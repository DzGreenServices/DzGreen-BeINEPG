"""Scrape the official beIN TV Guide and generate epg.xml + channels.csv."""

import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


URL = "https://www.bein.com/en/tv-guide/?c=dz"

CONFIG_FILE = Path("channels_config.csv")
EPG_FILE = Path("epg.xml")
CHANNELS_FILE = Path("channels.csv")

DEFAULT_ID_SUFFIX = ".dz"
PAGE_TIMEOUT = 60000
UPDATE_WAIT_MS = 1200
ROW_WAIT_MS = 1500  # max wait for one row to render its programme blocks


# Known channels that may have no <a href>.
# The matching is based on the logo filename, not channel-index.
KNOWN_LOGOS = {
    "4kdigitalmono": ("beINSPORTS4K", "beIN SPORTS 4K"),
    "2023alkass1": ("Alkass1", "Alkass 1"),
    "2023alkass2": ("Alkass2", "Alkass 2"),
    "2023alkass3": ("Alkass3", "Alkass 3"),
    "2023alkass4": ("Alkass4", "Alkass 4"),
    "2023alkass5": ("Alkass5", "Alkass 5"),
    "2023alkass6": ("Alkass6", "Alkass 6"),
    "2023alkass7": ("Alkass7", "Alkass 7"),
    "2023alkass8": ("Alkass8", "Alkass 8"),
    "staraction2024200x2001": ("StarAction", "Star Action"),
}


# Extract one channel row. The guide renders programme blocks lazily
# (rows far from the viewport are empty), so rows are read one by one
# after being scrolled into view.
ROW_JS = r"""
row => {
    const a = row.querySelector('.channel-col a[href]');
    const img = row.querySelector('.channel-col img');

    return {
        index: row.getAttribute('data-channel-index') || '',
        href: a ? a.href : '',
        logo: img ? (img.currentSrc || img.src || '') : '',
        programs: [...row.querySelectorAll('.prog-block')].map(b => ({
            title: (b.dataset.fullTitle || '').trim(),
            category: (b.dataset.fullCategory || '').trim(),
            start: b.dataset.startMs || '',
            end: b.dataset.endMs || ''
        }))
    };
}
"""

# Same extraction for every row currently in the DOM.
EXTRACT_JS = (
    "() => [...document.querySelectorAll('.channel-row')].map("
    + ROW_JS
    + ")"
)


def normalize(value):
    """Normalize text for reliable logo matching."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def load_config():
    """Read optional overrides: slug,tvg_id,name."""
    config = {}

    if not CONFIG_FILE.exists():
        return config

    with CONFIG_FILE.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            slug = (row.get("slug") or "").strip()

            if slug:
                config[slug] = {
                    "tvg_id": (row.get("tvg_id") or "").strip(),
                    "name": (row.get("name") or "").strip(),
                }

    return config


def identify_channel(row):
    """
    Use the channel URL when available.
    If there is no URL, identify the channel from its logo filename.
    Never use channel-index as a unique channel ID because indexes can
    repeat between categories.
    """
    href = (row.get("href") or "").strip()
    logo = (row.get("logo") or "").strip()

    if href:
        path = urlparse(href).path.rstrip("/")
        slug = path.split("/")[-1] if path else ""

        if slug:
            return slug, slug

    if not logo:
        return None, None

    filename = Path(urlparse(logo).path).stem
    key = normalize(filename)

    # Exact known logo matches.
    if key in KNOWN_LOGOS:
        return KNOWN_LOGOS[key]

    # Handle Alkass logos with minor filename variations.
    match = re.search(r"alkass[_-]?([1-8])", filename, re.IGNORECASE)
    if match:
        number = match.group(1)
        return f"Alkass{number}", f"Alkass {number}"

    # Handle common 4K logo filename variations.
    if "4k" in key and "digital" in key:
        return "beINSPORTS4K", "beIN SPORTS 4K"

    # Stable fallback for other channels without links.
    # The filename is stable across different category indexes.
    slug = "logo_" + key if key else ""
    if not slug:
        return None, None

    readable_name = re.sub(r"[_-]+", " ", filename).strip()
    readable_name = readable_name or slug

    return slug, readable_name


def valid_program(program):
    """Reject incomplete or invalid programme records."""
    try:
        start = int(program.get("start", ""))
        end = int(program.get("end", ""))

        return (
            bool(program.get("title", "").strip())
            and start > 0
            and end > start
        )
    except (TypeError, ValueError):
        return False


def collect_visible_rows(page, channels):
    """Collect programmes from all channel rows currently in the DOM."""
    return add_rows(page.evaluate(EXTRACT_JS), channels)


def collect_all_rows(page, channels):
    """
    Scroll every channel row into view, wait for its programme blocks,
    then read it. Returns (row_count, empty_row_count).
    """
    row_locator = page.locator(".channel-row")
    total = row_locator.count()
    empty = 0

    for i in range(total):
        # The DOM can change while scrolling; stop if the row is gone.
        if i >= row_locator.count():
            break

        row = row_locator.nth(i)

        try:
            row.scroll_into_view_if_needed(timeout=5000)
            row.locator(".prog-block").first.wait_for(
                state="attached", timeout=ROW_WAIT_MS
            )
        except PlaywrightTimeoutError:
            pass

        try:
            data = row.evaluate(ROW_JS)
        except PlaywrightTimeoutError:
            continue

        _, added = add_rows([data], channels)

        if added == 0:
            empty += 1

    return total, empty


def add_rows(rows, channels):
    """Merge extracted rows into the channels dict."""
    added_programmes = 0

    for row in rows:
        slug, fallback_name = identify_channel(row)

        if not slug:
            continue

        ch = channels.setdefault(
            slug,
            {
                "name": fallback_name,
                "logo": row.get("logo", ""),
                "programs": {},
            },
        )

        # Fill missing metadata without overwriting existing good values.
        if not ch["logo"] and row.get("logo"):
            ch["logo"] = row["logo"]

        if not ch["name"] and fallback_name:
            ch["name"] = fallback_name

        for prog in row.get("programs", []):
            if not valid_program(prog):
                continue

            # Overlapping guide days can repeat the same programme.
            # Keep one entry per start timestamp for each channel.
            start_key = str(int(prog["start"]))

            ch["programs"][start_key] = {
                "title": prog["title"].strip(),
                "category": prog.get("category", "").strip(),
                "start": start_key,
                "end": str(int(prog["end"])),
            }
            added_programmes += 1

    return len(rows), added_programmes


def wait_for_guide(page):
    """Wait until programme blocks are present after a category/day change."""
    try:
        page.wait_for_selector(
            ".channel-row .prog-block",
            state="attached",
            timeout=20000,
        )
    except PlaywrightTimeoutError:
        # Some dates may have no available programmes.
        pass

    page.wait_for_timeout(UPDATE_WAIT_MS)


def scrape():
    """Visit each category and each displayed day, collecting all channels."""
    channels = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        try:
            page = browser.new_page()
            page.set_default_timeout(PAGE_TIMEOUT)
            page.goto(URL, wait_until="domcontentloaded")
            page.wait_for_selector(".category-tab", timeout=PAGE_TIMEOUT)

            # Read the number of categories before starting the loop.
            category_count = page.locator(".category-tab").count()

            if category_count == 0:
                raise RuntimeError("لم يتم العثور على تصنيفات القنوات.")

            for category_index in range(category_count):
                tabs = page.locator(".category-tab")

                if category_index >= tabs.count():
                    break

                tabs.nth(category_index).click()
                wait_for_guide(page)

                day_count = page.locator(".day-cell").count()

                # If there are no day selectors, still collect the current view.
                if day_count == 0:
                    collect_all_rows(page, channels)
                    continue

                for day_index in range(day_count):
                    days = page.locator(".day-cell")

                    if day_index >= days.count():
                        break

                    days.nth(day_index).click()
                    wait_for_guide(page)

                    row_count, empty_rows = collect_all_rows(page, channels)

                    print(
                        f"Category {category_index + 1}/{category_count}, "
                        f"day {day_index + 1}/{day_count}: "
                        f"{row_count} rows ({empty_rows} without programmes), "
                        f"{len(channels)} unique channels"
                    )

        finally:
            browser.close()

    return channels


def xmltv_time(ms):
    """Convert Unix milliseconds to an XMLTV UTC timestamp."""
    dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    return dt.strftime("%Y%m%d%H%M%S +0000")


def build_outputs(channels, config):
    """Write XMLTV and CSV, preserving the original output structure."""
    root = ET.Element("tv", {"generator-info-name": "bein-epg-scraper"})
    rows = []
    resolved = {}

    # XMLTV requires all channel declarations before programme entries.
    for slug, ch in channels.items():
        cfg = config.get(slug, {})

        tvg_id = cfg.get("tvg_id") or f"{slug}{DEFAULT_ID_SUFFIX}"
        name = cfg.get("name") or ch.get("name") or slug

        resolved[slug] = tvg_id

        channel_element = ET.SubElement(root, "channel", id=tvg_id)
        ET.SubElement(channel_element, "display-name").text = name

        if ch.get("logo"):
            ET.SubElement(channel_element, "icon", src=ch["logo"])

        rows.append({
            "tvg_id": tvg_id,
            "name": name,
            "slug": slug,
            "logo": ch.get("logo", ""),
        })

    total_programmes = 0

    for slug, ch in channels.items():
        programs = sorted(
            ch["programs"].values(),
            key=lambda item: int(item["start"]),
        )

        for prog in programs:
            if not valid_program(prog):
                continue

            programme = ET.SubElement(
                root,
                "programme",
                start=xmltv_time(prog["start"]),
                stop=xmltv_time(prog["end"]),
                channel=resolved[slug],
            )

            ET.SubElement(
                programme, "title", lang="en"
            ).text = prog["title"]

            if prog.get("category"):
                ET.SubElement(
                    programme, "category", lang="en"
                ).text = prog["category"]

            total_programmes += 1

    # Write temporary files first so a failed write does not truncate
    # the existing output files.
    temp_epg = EPG_FILE.with_suffix(EPG_FILE.suffix + ".tmp")
    temp_channels = CHANNELS_FILE.with_suffix(
        CHANNELS_FILE.suffix + ".tmp"
    )

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(
        temp_epg,
        encoding="utf-8",
        xml_declaration=True,
    )

    with temp_channels.open(
        "w", newline="", encoding="utf-8"
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["tvg_id", "name", "slug", "logo"],
        )
        writer.writeheader()
        writer.writerows(rows)

    temp_epg.replace(EPG_FILE)
    temp_channels.replace(CHANNELS_FILE)

    return total_programmes


def main():
    try:
        channels = scrape()
        total = sum(
            len(ch["programs"]) for ch in channels.values()
        )

        # Never replace previous good files with an empty result.
        if not channels or total == 0:
            print(
                "لم يتم استخراج أي برنامج صالح؛ "
                "تم الاحتفاظ بالملفات السابقة.",
                file=sys.stderr,
            )
            sys.exit(1)

        written = build_outputs(channels, load_config())

        print()
        print("تم الاستخراج بنجاح.")
        print(f"القنوات: {len(channels)}")
        print(f"البرامج: {written}")
        print(f"XMLTV: {EPG_FILE.resolve()}")
        print(f"CSV:   {CHANNELS_FILE.resolve()}")

    except Exception as exc:
        print(f"فشل الاستخراج: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
