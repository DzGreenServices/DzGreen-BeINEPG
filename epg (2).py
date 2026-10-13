"""Scrape the official beIN TV Guide and generate epg.xml + channels.csv."""

import csv
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from playwright.sync_api import (
    sync_playwright,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

URL = "https://www.bein.com/en/tv-guide/?c=dz"

CONFIG_FILE = Path("channels_config.csv")
EPG_FILE = Path("epg.xml")
CHANNELS_FILE = Path("channels.csv")
MANUAL_DIR = Path("manual")

DEFAULT_ID_SUFFIX = ".dz"
PAGE_TIMEOUT = 60000
UPDATE_WAIT_MS = 400

# Scrape from "now" up to N days (today included), matching the site's day strip.
DAYS_TO_SCRAPE = 4
# Drop programmes that already ended before the script runs.
SKIP_FINISHED = True
# Rows load their programmes lazily: wait until the loaded-row count stops
# growing (quiet period) instead of using a fixed sleep.
ROWS_WAIT_MAX_MS = 5000
ROWS_QUIET_MS = 600
# Re-check once (per page) rows that are still empty: scroll to them, wait longer.
RETRY_EMPTY_ROWS = True
RETRY_WAIT_MAX_MS = 5000
RETRY_QUIET_MS = 1200

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

# Extract programmes from the standard markup and common alternatives.
ROW_JS = r"""
row => {
    const first = (...values) =>
        values.find(v => v !== undefined && v !== null &&
            String(v).trim() !== '') || '';

    const a = row.querySelector(
        '.channel-col a[href], a[href*="beinconnect.app"], a[href]'
    );
    const img = row.querySelector(
        '.channel-col img, img[src], img[data-src]'
    );

    const logo = img
        ? first(img.currentSrc, img.src,
                img.getAttribute('data-src'),
                img.getAttribute('src'))
        : '';

    const candidates = [...row.querySelectorAll(
        '.prog-block, .program-block, .programme-block, ' +
        '.program-item, .programme-item, [data-full-title], ' +
        '[data-title][data-start-ms], [data-start-ms][data-end-ms], ' +
        '[data-start][data-end]'
    )];

    const seen = new Set();
    const programs = [];

    for (const b of candidates) {
        const d = b.dataset || {};

        const title = first(
            d.fullTitle, d.programTitle, d.title,
            b.getAttribute('title'),
            b.getAttribute('aria-label')
        );

        const start = first(
            d.startMs, d.start, d.startTime, d.programStart,
            b.getAttribute('data-start-ms'),
            b.getAttribute('data-start')
        );

        const end = first(
            d.endMs, d.end, d.endTime, d.programEnd,
            b.getAttribute('data-end-ms'),
            b.getAttribute('data-end')
        );

        if (!title || !start || !end) continue;

        const key = `${title}|${start}|${end}`;
        if (seen.has(key)) continue;
        seen.add(key);

        programs.push({
            title: String(title).trim(),
            category: String(first(
                d.fullCategory, d.category, d.programCategory
            )).trim(),
            start: String(start).trim(),
            end: String(end).trim()
        });
    }

    return {
        index: row.getAttribute('data-channel-index') || '',
        href: a ? a.href : '',
        logo: String(logo || ''),
        channelName: first(
            img && img.getAttribute('alt'),
            img && img.getAttribute('title'),
            a && a.textContent,
            row.getAttribute('data-channel-name')
        ),
        programs
    };
}
"""

# First read the site's usual rows. Then discover other row wrappers by
# walking up from programme elements to a container containing its channel logo.
EXTRACT_JS = r"""
() => {
    const rows = new Set(
        document.querySelectorAll('.channel-row, [data-channel-index]')
    );

    const selector =
        '.prog-block, .program-block, .programme-block, ' +
        '.program-item, .programme-item, [data-full-title], ' +
        '[data-title][data-start-ms], [data-start-ms][data-end-ms], ' +
        '[data-start][data-end]';

    for (const p of document.querySelectorAll(selector)) {
        let n = p;

        for (let depth = 0; n && depth < 9; depth++, n = n.parentElement) {
            if (n.matches &&
                n.matches('.channel-row, [data-channel-index]')) {
                rows.add(n);
                break;
            }

            if (n.querySelector &&
                n.querySelector('img[src], img[data-src]')) {
                const logos =
                    n.querySelectorAll('img[src], img[data-src]').length;

                if (logos === 1) {
                    rows.add(n);
                    break;
                }
            }
        }
    }

    return [...rows]
        .map(row => (ROW_EXTRACT)(row))
        .filter(x => x && (x.logo || x.href || x.programs.length));
}
""".replace("ROW_EXTRACT", "(" + ROW_JS + ")")

# Used to verify that programme data has actually refreshed after date selection.
DATE_STATE_JS = r"""
() => {
    const active = document.querySelector('.day-cell.active[data-date]');
    const selector =
        '.prog-block, .program-block, .programme-block, ' +
        '.program-item, .programme-item, [data-full-title][data-start-ms], ' +
        '[data-title][data-start-ms]';

    const programs = [...document.querySelectorAll(selector)];

    const signature = programs.slice(0, 12).map(p => {
        const d = p.dataset || {};
        return [
            d.startMs || d.start || '',
            d.fullTitle || d.title || '',
            d.endMs || d.end || ''
        ].join('|');
    }).join('~');

    return {
        date: active
            ? (active.getAttribute('data-date') ||
               active.textContent.trim())
            : '',
        signature,
        count: programs.length
    };
}
"""


def normalize(value):
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def load_config():
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


def identify_by_logo(logo):
    """Known channels (Alkass, 4K...) are recognised from their logo file name."""
    if not logo:
        return None

    filename = Path(urlparse(logo).path).stem
    key = normalize(filename)

    if key in KNOWN_LOGOS:
        return KNOWN_LOGOS[key]

    match = re.search(r"alkass[_\- ]*([1-8])", filename, re.I)
    if match:
        number = match.group(1)
        return f"Alkass{number}", f"Alkass {number}"

    if "4k" in key and "digital" in key:
        return "beINSPORTS4K", "beIN SPORTS 4K"

    return None


def identify_channel(row):
    href = (row.get("href") or "").strip()
    logo = (row.get("logo") or "").strip()

    known = identify_by_logo(logo)
    if known:
        return known

    if href:
        path = urlparse(href).path.rstrip("/")
        slug = path.split("/")[-1] if path else ""

        if slug:
            return slug, slug

    if logo:
        filename = Path(urlparse(logo).path).stem
        key = normalize(filename)
        slug = "logo_" + key if key else ""

        if slug:
            readable = re.sub(r"[_-]+", " ", filename).strip()
            return slug, readable or slug

    name = (row.get("channelName") or "").strip()
    if name:
        slug = re.sub(r"[^A-Za-z0-9]+", "", name) or "unknown_channel"
        return slug, name

    return None, None


def timestamp_ms(value):
    """Accept Unix seconds, milliseconds, or ISO-8601 date strings."""
    value = str(value or "").strip()

    if not value:
        raise ValueError("empty timestamp")

    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        number = float(value)

        if number < 100_000_000_000:
            number *= 1000

        return int(number)

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return int(parsed.timestamp() * 1000)


def is_finished(program):
    """True if the programme already ended before the current moment."""
    try:
        end = timestamp_ms(program.get("end", ""))
    except (TypeError, ValueError, OverflowError):
        return False

    return end <= int(datetime.now(timezone.utc).timestamp() * 1000)


def pick_dates(dates):
    """Keep only the days from TODAY up to DAYS_TO_SCRAPE days (as in the site's strip)."""
    if not dates:
        return dates

    start_index = 0

    for i, item in enumerate(dates):
        if "today" in (item.get("text") or "").lower():
            start_index = i
            break

    picked = dates[start_index:start_index + DAYS_TO_SCRAPE]

    if len(picked) < DAYS_TO_SCRAPE:
        print(
            f"  Warning: the site shows only {len(picked)} day(s), "
            f"expected {DAYS_TO_SCRAPE}."
        )

    return picked


def valid_program(program):
    try:
        start = timestamp_ms(program.get("start", ""))
        end = timestamp_ms(program.get("end", ""))

        return (
            bool((program.get("title") or "").strip())
            and start > 0
            and end > start
        )

    except (TypeError, ValueError, OverflowError):
        return False


# Read every channel row in a single browser call (fast).
BULK_ROWS_JS = (
    "() => [...document.querySelectorAll("
    "'.channel-row, [data-channel-index]')].map(" + ROW_JS + ")"
)

# Scroll through the rows quickly so lazily loaded programmes get rendered.
AUTOSCROLL_JS = r"""
async () => {
    const rows = [
        ...document.querySelectorAll('.channel-row, [data-channel-index]')
    ];
    const pause = ms => new Promise(r => setTimeout(r, ms));

    for (let i = 0; i < rows.length; i += 3) {
        rows[i].scrollIntoView({block: 'center'});
        await pause(25);
    }

    if (rows.length) {
        rows[rows.length - 1].scrollIntoView({block: 'center'});
        await pause(60);
    }
}
"""

# Scroll specifically to rows that still have no programme (retry pass).
SCROLL_EMPTY_JS = r"""
async () => {
    const sel = '.prog-block, .program-block, .programme-block, ' +
        '.program-item, .programme-item, [data-full-title], ' +
        '[data-title][data-start-ms], [data-start-ms][data-end-ms], ' +
        '[data-start][data-end]';
    const rows = [
        ...document.querySelectorAll('.channel-row, [data-channel-index]')
    ].filter(r => !r.querySelector(sel));

    for (const row of rows) {
        row.scrollIntoView({block: 'center'});
        await new Promise(r => setTimeout(r, 120));
    }
}
"""

# Wait until every row has programmes, or the loaded count stops growing.
WAIT_ROWS_JS = r"""
async ([maxMs, quietMs]) => {
    const sel = '.prog-block, .program-block, .programme-block, ' +
        '.program-item, .programme-item, [data-full-title], ' +
        '[data-title][data-start-ms], [data-start-ms][data-end-ms], ' +
        '[data-start][data-end]';
    const pause = ms => new Promise(r => setTimeout(r, ms));
    const state = () => {
        const rows = [
            ...document.querySelectorAll('.channel-row, [data-channel-index]')
        ];
        return [rows.length, rows.filter(r => r.querySelector(sel)).length];
    };

    const start = Date.now();
    let [total, loaded] = state();
    let lastChange = Date.now();

    while (loaded < total && Date.now() - start < maxMs) {
        await pause(100);
        const [t, l] = state();
        total = t;

        if (l !== loaded) {
            loaded = l;
            lastChange = Date.now();
        }

        if (Date.now() - lastChange > quietMs) break;
    }

    return [total, loaded];
}
"""


def add_rows(rows, channels):
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

        if not ch["logo"] and row.get("logo"):
            ch["logo"] = row["logo"]

        if not ch["name"] and fallback_name:
            ch["name"] = fallback_name

        for prog in row.get("programs", []):
            if not valid_program(prog):
                continue

            start_key = str(timestamp_ms(prog["start"]))

            ch["programs"][start_key] = {
                "title": prog["title"].strip(),
                "category": (prog.get("category") or "").strip(),
                "start": start_key,
                "end": str(timestamp_ms(prog["end"])),
            }

            added_programmes += 1

    return len(rows), added_programmes


def read_page_rows(page, channels, retry=False):
    """Load rows (scroll + adaptive wait), read all at once.

    Returns (total, empty_slugs)."""
    if retry:
        page.evaluate(SCROLL_EMPTY_JS)
        wait_args = [RETRY_WAIT_MAX_MS, RETRY_QUIET_MS]
    else:
        page.evaluate(AUTOSCROLL_JS)
        wait_args = [ROWS_WAIT_MAX_MS, ROWS_QUIET_MS]

    page.evaluate(WAIT_ROWS_JS, wait_args)
    rows = page.evaluate(BULK_ROWS_JS)
    empty = []

    for row in rows:
        _, added = add_rows([row], channels)

        if added == 0:
            slug, _name = identify_channel(row)
            empty.append(slug or f"row{row.get('index', '?')}")

    return len(rows), empty


def collect_all_rows(page, channels):
    """Read all rows, retry the empty ones once, then scan alternate wrappers."""
    try:
        total, empty = read_page_rows(page, channels)

        if RETRY_EMPTY_ROWS and empty:
            before = len(empty)
            total, empty = read_page_rows(page, channels, retry=True)

            if len(empty) < before:
                print(f"  retry recovered {before - len(empty)} row(s)")

        if empty:
            shown = ", ".join(empty[:8])
            more = f" +{len(empty) - 8}" if len(empty) > 8 else ""
            print(f"  without programmes: {shown}{more}")

    except PlaywrightError as exc:
        print(f"  rows: skipped ({str(exc).splitlines()[0]})")
        total, empty = 0, []

    try:
        add_rows(page.evaluate(EXTRACT_JS), channels)

    except PlaywrightError as exc:
        print(f"  automatic row scan: skipped ({str(exc).splitlines()[0]})")

    return total, len(empty)


def wait_for_guide(page):
    """Wait for the guide container after changing category."""
    try:
        page.wait_for_selector(
            ".channel-row, [data-channel-index]",
            state="attached",
            timeout=20000,
        )
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(UPDATE_WAIT_MS)


def select_day_and_wait(page, day):
    """Wait for the selected date's programme data to refresh."""
    date_value = (day.get_attribute("data-date") or "").strip()
    before = page.evaluate(DATE_STATE_JS)

    if date_value:
        target = page.locator(
            f'.day-cell[data-date="{date_value}"]'
        ).first
    else:
        target = day

    try:
        target.click()
    except PlaywrightError:
        if date_value:
            page.locator(
                f'.day-cell[data-date="{date_value}"]'
            ).first.click()
        else:
            raise

    if date_value:
        page.wait_for_function(
            """date => {
                const active = document.querySelector('.day-cell.active');
                return !!active &&
                    (active.getAttribute('data-date') || '') === date;
            }""",
            arg=date_value,
            timeout=10000,
        )

    # Wait until programme timestamps change, not merely until an old
    # .prog-block remains in the DOM.
    if not date_value or date_value != before["date"]:
        try:
            page.wait_for_function(
                "before => { const s = (" + DATE_STATE_JS +
                ")(); return s.count > 0 && s.signature !== before.signature; }",
                arg=before,
                timeout=8000,
            )
        except PlaywrightTimeoutError:
            print(
                f"  Warning: programme data did not visibly refresh "
                f"for {date_value or 'selected date'}."
            )

    page.wait_for_timeout(UPDATE_WAIT_MS)


class _ProgParser(HTMLParser):
    """Read programmes from saved channel HTML with varied class/attribute names."""

    PROGRAM_CLASSES = {
        "prog-block",
        "program-block",
        "programme-block",
        "program-item",
        "programme-item",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.programs = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set((a.get("class") or "").split())

        has_times = (
            any(k in a for k in (
                "data-start-ms", "data-start", "data-start-time"
            ))
            and any(k in a for k in (
                "data-end-ms", "data-end", "data-end-time"
            ))
        )

        if not (classes & self.PROGRAM_CLASSES or has_times):
            return

        title = (
            a.get("data-full-title")
            or a.get("data-program-title")
            or a.get("data-title")
            or a.get("title")
            or a.get("aria-label")
            or ""
        ).strip()

        start = (
            a.get("data-start-ms")
            or a.get("data-start")
            or a.get("data-start-time")
            or ""
        ).strip()

        end = (
            a.get("data-end-ms")
            or a.get("data-end")
            or a.get("data-end-time")
            or ""
        ).strip()

        if not title or not start or not end:
            return

        self.programs.append({
            "title": title,
            "category": (
                a.get("data-full-category")
                or a.get("data-category")
                or ""
            ).strip(),
            "start": start,
            "end": end,
        })


def default_channel_name(slug):
    for known_slug, known_name in KNOWN_LOGOS.values():
        if known_slug == slug:
            return known_name

    return slug


def load_manual(channels):
    """Read optional HTML snippets stored in manual/<channel-slug>.html."""
    if not MANUAL_DIR.is_dir():
        return 0

    total = 0

    for path in sorted(MANUAL_DIR.glob("*.htm*")):
        slug = path.stem.split("__")[0].strip()

        if not slug:
            continue

        parser = _ProgParser()
        parser.feed(
            path.read_text(encoding="utf-8-sig", errors="replace")
        )

        programs = [
            prog for prog in parser.programs if valid_program(prog)
        ]

        if not programs:
            print(f"Manual: {path.name}: no valid programmes found")
            continue

        # Manual files are only a fallback: skip them when the channel
        # was already scraped automatically from the site.
        existing = channels.get(slug)

        if existing and existing["programs"]:
            print(
                f"Manual: {path.name}: skipped "
                f"(scraped automatically: {len(existing['programs'])} programmes)"
            )
            continue

        ch = channels.setdefault(
            slug,
            {
                "name": default_channel_name(slug),
                "logo": "",
                "programs": {},
            },
        )

        for prog in programs:
            start_key = str(timestamp_ms(prog["start"]))

            ch["programs"][start_key] = {
                "title": prog["title"],
                "category": prog["category"],
                "start": start_key,
                "end": str(timestamp_ms(prog["end"])),
            }

        total += len(programs)
        print(
            f"Manual: {path.name}: {len(programs)} programmes -> {slug}"
        )

    return total


def scrape():
    """Visit every category and date; wait for the page to update on each date."""
    channels = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        try:
            page = browser.new_page()
            page.set_default_timeout(PAGE_TIMEOUT)

            # Images/fonts/media are not needed: we read attributes only.
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"image", "media", "font"}
                else route.continue_(),
            )
            page.goto(URL, wait_until="domcontentloaded")

            page.wait_for_selector(".category-tab", timeout=PAGE_TIMEOUT)
            category_count = page.locator(".category-tab").count()

            if category_count == 0:
                raise RuntimeError("لم يتم العثور على تصنيفات القنوات.")

            for category_index in range(category_count):
                tabs = page.locator(".category-tab")

                if category_index >= tabs.count():
                    break

                tabs.nth(category_index).click()
                wait_for_guide(page)

                dates = page.locator(".day-cell").evaluate_all(
                    """els => els.map((e, i) => ({
                        date: e.getAttribute('data-date') || '',
                        text: e.textContent.trim(),
                        index: i
                    }))"""
                )

                dates = pick_dates(dates)

                if not dates:
                    row_count, empty_rows = collect_all_rows(page, channels)
                    print(
                        f"Category {category_index + 1}/{category_count}: "
                        f"{row_count} rows ({empty_rows} without programmes)"
                    )
                    continue

                for day_info in dates:
                    date_value = day_info["date"]

                    try:
                        if date_value:
                            locator = page.locator(
                                f'.day-cell[data-date="{date_value}"]'
                            ).first
                        else:
                            locator = page.locator(".day-cell").nth(
                                day_info["index"]
                            )

                        select_day_and_wait(page, locator)

                    except (PlaywrightError, PlaywrightTimeoutError) as exc:
                        print(
                            f"  date {date_value or day_info['text']}: "
                            f"selection warning "
                            f"({str(exc).splitlines()[0]})"
                        )
                        continue

                    row_count, empty_rows = collect_all_rows(page, channels)
                    label = date_value or day_info["text"]

                    print(
                        f"Category {category_index + 1}/{category_count}, "
                        f"date {label}: {row_count} rows "
                        f"({empty_rows} without programmes), "
                        f"{len(channels)} unique channels"
                    )

        finally:
            browser.close()

    return channels


def xmltv_time(value):
    dt = datetime.fromtimestamp(
        timestamp_ms(value) / 1000,
        tz=timezone.utc,
    )
    return dt.strftime("%Y%m%d%H%M%S +0000")


def build_outputs(channels, config):
    root = ET.Element(
        "tv",
        {"generator-info-name": "bein-epg-scraper"},
    )

    rows = []
    resolved = {}

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

            if SKIP_FINISHED and is_finished(prog):
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

    # Write temporary files first so a failed run won't truncate good outputs.
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
        "w",
        newline="",
        encoding="utf-8",
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


def print_diagnostics(channels):
    """List channels with no upcoming programmes, and unrecognised logo-only channels."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    problems = []

    for slug, ch in sorted(channels.items()):
        ends = [int(p["end"]) for p in ch["programs"].values()]
        upcoming = [e for e in ends if e > now_ms]

        if not upcoming:
            last = (
                datetime.fromtimestamp(max(ends) / 1000, tz=timezone.utc)
                .strftime("%Y-%m-%d")
                if ends else "none"
            )
            problems.append(
                f"  {slug}: no upcoming programmes (last data: {last})"
            )

        elif slug.startswith("logo_"):
            problems.append(
                f"  {slug}: identified only from logo -> {ch.get('logo', '')}"
            )

    if problems:
        print()
        print("Diagnostics - channels to check:")
        print("\n".join(problems))


def main():
    try:
        channels = scrape()
        load_manual(channels)

        total = sum(
            len(ch["programs"]) for ch in channels.values()
        )

        if not channels or total == 0:
            print(
                "لم يتم استخراج أي برنامج صالح؛ "
                "تم الاحتفاظ بالملفات السابقة.",
                file=sys.stderr,
            )
            sys.exit(1)

        print_diagnostics(channels)

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
