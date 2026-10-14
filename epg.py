"""Scrape beIN TV Guide into epg.xml and channels.csv using section + channel index."""

import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from playwright.sync_api import (
    sync_playwright,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)


URL = "https://www.bein.com/en/tv-guide/?c=dz"
EPG_FILE = Path("epg.xml")
CHANNELS_FILE = Path("channels.csv")
PAGE_TIMEOUT = 60000
UPDATE_WAIT_MS = 250
GUIDE_WAIT_MS = 12000
ROW_WAIT_MS = 900


# Read the row's index, optional link/name, logo, and all programme data.
# A channel link is optional: programme collection does not depend on it.
ROW_JS = r"""
row => {
    const a = row.querySelector('.channel-col a[href]');
    const img = row.querySelector('.channel-col img');
    const label = row.querySelector('[data-channel-name], .channel-name, .channel-title');
    const alt = img ? (img.getAttribute('alt') || '').trim() : '';
    const usefulAlt = alt && !/^(channel\s+)?logo$/i.test(alt) ? alt : '';

    return {
        index: row.getAttribute('data-channel-index') || '',
        href: a ? a.href : '',
        name: label ? (label.textContent || '').trim() : usefulAlt,
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

ROW_INDEXES_JS = r"""
() => [...new Set(
    [...document.querySelectorAll('.channel-row')]
        .map(row => row.getAttribute('data-channel-index'))
        .filter(value => value !== null && /^\d+$/.test(value))
)]
"""

SCROLL_ROW_JS = r"""
i => {
    const row = document.querySelector('.channel-row[data-channel-index="' + String(i) + '"]');
    if (!row) return false;
    row.scrollIntoView({block: 'center'});
    return true;
}
"""

ROW_READY_JS = r"""
i => {
    const row = document.querySelector('.channel-row[data-channel-index="' + String(i) + '"]');
    return !row || row.querySelectorAll('.prog-block').length > 0;
}
"""

READ_ROW_JS = r"""
i => {
    const row = document.querySelector('.channel-row[data-channel-index="' + String(i) + '"]');
    if (!row) return null;
    const read = ROW_READER_PLACEHOLDER;
    return read(row);
}
""".replace("ROW_READER_PLACEHOLDER", ROW_JS.strip())


def category_key(label, position):
    """Use stable names for the two known sections; support other tabs too."""
    text = (label or "").strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", text)

    if "sport" in compact:
        return "sports"
    if "entertain" in compact:
        return "entertainment"

    if not compact:
        if position == 0:
            return "sports"
        if position == 1:
            return "entertainment"

    key = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return key or f"category{position + 1}"


def slug_from_href(href):
    """Return the final URL path segment, if the row has a channel link."""
    path = urlparse((href or "").strip()).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def valid_program(program):
    """Reject incomplete programme records and invalid timestamps."""
    try:
        title = (program.get("title") or "").strip()
        start = int(program.get("start", ""))
        end = int(program.get("end", ""))
        return bool(title) and start > 0 and end > start
    except (TypeError, ValueError):
        return False


def add_row(row, section, section_key, channels):
    """Merge one row under section_index, even when href/name are absent."""
    raw_index = str(row.get("index", "")).strip()
    if not raw_index.isdigit():
        return 0

    index = str(int(raw_index))
    tvg_id = f"{section_key}_{index}"
    slug = slug_from_href(row.get("href", ""))
    row_name = (row.get("name") or "").strip()
    name = row_name or slug  # blank if neither a usable label nor link exists
    logo = (row.get("logo") or "").strip()

    channel = channels.setdefault(
        tvg_id,
        {
            "tvg_id": tvg_id,
            "name": name,
            "slug": slug,
            "logo": logo,
            "category": section,
            "index": index,
            "programs": {},
        },
    )

    # Fill metadata when another displayed day provides it.
    if not channel["name"] and name:
        channel["name"] = name
    if not channel["slug"] and slug:
        channel["slug"] = slug
    if not channel["logo"] and logo:
        channel["logo"] = logo

    added = 0
    for program in row.get("programs", []):
        if not valid_program(program):
            continue

        start_key = str(int(program["start"]))
        # The guide can repeat the same programme across overlapping days.
        channel["programs"][start_key] = {
            "title": program["title"].strip(),
            "category": (program.get("category") or "").strip(),
            "start": start_key,
            "end": str(int(program["end"])),
        }
        added += 1

    return added


def collect_all_rows(page, channels, section, section_key):
    """Visit each data-channel-index in the current section and read its programmes."""
    indexes = page.evaluate(ROW_INDEXES_JS)
    indexes = sorted({int(index) for index in indexes})
    empty_rows = 0

    for index in indexes:
        try:
            if not page.evaluate(SCROLL_ROW_JS, index):
                empty_rows += 1
                continue

            try:
                page.wait_for_function(ROW_READY_JS, arg=index, timeout=ROW_WAIT_MS)
            except PlaywrightTimeoutError:
                pass

            row = page.evaluate(READ_ROW_JS, index)
            if not row:
                empty_rows += 1
                continue

            added = add_row(row, section, section_key, channels)
            if added == 0:
                empty_rows += 1

        except PlaywrightError as exc:
            print(f"  index {index}: skipped ({str(exc).splitlines()[0]})")
            empty_rows += 1

    return len(indexes), empty_rows


SCHEDULE_SIGNATURE_JS = r"""
() => [...document.querySelectorAll('.channel-row')].map(row => {
    const index = row.getAttribute('data-channel-index') || '';
    const blocks = [...row.querySelectorAll('.prog-block')];
    if (!blocks.length) return `${index}:0`;
    const first = blocks[0];
    const last = blocks[blocks.length - 1];
    return [
        index,
        blocks.length,
        first.dataset.startMs || '',
        first.dataset.fullTitle || '',
        last.dataset.endMs || '',
        last.dataset.fullTitle || ''
    ].join('~');
}).join('|')
"""

WAIT_FOR_CHANGED_SCHEDULE_JS = r"""
previous => {
    const blocks = document.querySelectorAll('.channel-row .prog-block');
    if (!blocks.length) return false;
    const signature = [...document.querySelectorAll('.channel-row')].map(row => {
        const index = row.getAttribute('data-channel-index') || '';
        const items = [...row.querySelectorAll('.prog-block')];
        if (!items.length) return `${index}:0`;
        const first = items[0];
        const last = items[items.length - 1];
        return [index, items.length, first.dataset.startMs || '',
                first.dataset.fullTitle || '', last.dataset.endMs || '',
                last.dataset.fullTitle || ''].join('~');
    }).join('|');
    return signature !== previous;
}
"""

IS_SELECTED_CELL_JS = r"""
el => {
    const cls = typeof el.className === 'string' ? el.className : '';
    return el.getAttribute('aria-selected') === 'true' ||
           el.getAttribute('aria-current') === 'date' ||
           /(^|\s)(active|selected|current|is-active|is-selected)(\s|$)/i.test(cls);
}
"""

DAY_LABEL_JS = r"""
el => (el.innerText || el.getAttribute('data-date') || el.title || '').trim()
"""


def schedule_signature(page):
    """Summarize the loaded programme timestamps to detect real day changes."""
    return page.evaluate(SCHEDULE_SIGNATURE_JS)


def element_is_selected(locator):
    """Detect the currently selected tab/day when the site exposes its state."""
    try:
        return bool(locator.evaluate(IS_SELECTED_CELL_JS))
    except PlaywrightError:
        return False


def wait_for_schedule_change(page, previous_signature):
    """Wait for programme data to change; an already-present block is not enough."""
    try:
        page.wait_for_function(
            WAIT_FOR_CHANGED_SCHEDULE_JS,
            arg=previous_signature,
            timeout=GUIDE_WAIT_MS,
        )
    except PlaywrightTimeoutError:
        return False

    # Let the site's row re-render finish before we begin scrolling through it.
    page.wait_for_timeout(UPDATE_WAIT_MS)
    return schedule_signature(page) != previous_signature


def wait_for_guide(page):
    """Give the selected category/day a short chance to render."""
    try:
        page.wait_for_selector(
            ".channel-row .prog-block",
            state="attached",
            timeout=GUIDE_WAIT_MS,
        )
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(UPDATE_WAIT_MS)


def scrape():
    """Visit each section and displayed day, confirming schedule changes before scraping."""
    channels = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(PAGE_TIMEOUT)
            page.goto(URL, wait_until="domcontentloaded")
            page.wait_for_selector(".category-tab", timeout=PAGE_TIMEOUT)
            page.wait_for_selector(".day-cell", timeout=PAGE_TIMEOUT)
            wait_for_guide(page)

            category_count = page.locator(".category-tab").count()
            if not category_count:
                raise RuntimeError("لم يتم العثور على تصنيفات القنوات.")

            for category_index in range(category_count):
                tabs = page.locator(".category-tab")
                if category_index >= tabs.count():
                    break

                tab = tabs.nth(category_index)
                section = tab.inner_text().strip()
                section_key = category_key(section, category_index)

                tab_is_selected = element_is_selected(tab)
                # The guide opens on its first tab (Sports). Avoid clicking it
                # again when the site does not expose an active/selected class.
                if not tab_is_selected and category_index != 0:
                    before_category = schedule_signature(page)
                    tab.click()
                    if before_category and not wait_for_schedule_change(page, before_category):
                        print(
                            f"WARNING: {section or section_key}: programme data did not change "
                            "after selecting this category; skipping it to avoid duplicates."
                        )
                        continue
                wait_for_guide(page)

                # Save the view already shown by the page. This also protects the
                # currently selected date if the site does not mark it with a CSS class.
                active_label = ""
                day_count = page.locator(".day-cell").count()
                available_day_labels = []
                for date_index in range(day_count):
                    date_cell = page.locator(".day-cell").nth(date_index)
                    available_day_labels.append(date_cell.evaluate(DAY_LABEL_JS))
                print(
                    f"{section or section_key}: {day_count} day cells found "
                    f"[{', '.join(label for label in available_day_labels if label)}]"
                )

                for candidate_index in range(day_count):
                    candidate = page.locator(".day-cell").nth(candidate_index)
                    if element_is_selected(candidate):
                        active_label = candidate.evaluate(DAY_LABEL_JS)
                        break

                row_count, empty = collect_all_rows(
                    page, channels, section, section_key
                )
                print(
                    f"{section or section_key}, current view"
                    f"{(' [' + active_label + ']') if active_label else ''}: "
                    f"{row_count} indexes ({empty} without programmes), "
                    f"{len(channels)} channel IDs"
                )

                # Re-read the day controls on each iteration because the site may
                # rebuild them after a click.
                for day_index in range(day_count):
                    days = page.locator(".day-cell")
                    if day_index >= days.count():
                        break

                    day = days.nth(day_index)
                    day_label = day.evaluate(DAY_LABEL_JS) or f"day index {day_index}"

                    # The selected day was collected just above; don't trigger a
                    # needless wait or risk reading stale data for it.
                    if element_is_selected(day):
                        continue

                    before_day = schedule_signature(page)
                    try:
                        day.click()
                    except PlaywrightError as exc:
                        print(f"WARNING: {section_key} {day_label}: click failed ({str(exc).splitlines()[0]})")
                        continue

                    if not wait_for_schedule_change(page, before_day):
                        print(
                            f"WARNING: {section_key} {day_label}: page programme data did not change; "
                            "day skipped to avoid saving the previous day's programmes again."
                        )
                        continue

                    row_count, empty = collect_all_rows(
                        page, channels, section, section_key
                    )
                    print(
                        f"{section or section_key}, day {day_label} "
                        f"({day_index + 1}/{day_count}): {row_count} indexes "
                        f"({empty} without programmes), {len(channels)} channel IDs"
                    )
        finally:
            browser.close()

    return channels


def xmltv_time(ms):
    """Convert Unix milliseconds to an XMLTV UTC timestamp."""
    dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    return dt.strftime("%Y%m%d%H%M%S +0000")


def build_outputs(channels):
    """Write XMLTV and CSV with the same section_index tvg-id."""
    root = ET.Element("tv", {"generator-info-name": "bein-epg-index-scraper"})
    rows = []

    # Declare each channel before writing its programmes.
    for tvg_id, channel in sorted(
        channels.items(),
        key=lambda pair: (pair[1]["category"].lower(), int(pair[1]["index"])),
    ):
        element = ET.SubElement(root, "channel", id=tvg_id)
        ET.SubElement(element, "display-name").text = channel.get("name", "")

        if channel.get("logo"):
            ET.SubElement(element, "icon", src=channel["logo"])

        rows.append({
            "tvg_id": tvg_id,
            "name": channel.get("name", ""),
            "slug": channel.get("slug", ""),
            "logo": channel.get("logo", ""),
            "category": channel.get("category", ""),
            "index": channel.get("index", ""),
        })

    total_programmes = 0
    for tvg_id, channel in channels.items():
        for programme_data in sorted(
            channel["programs"].values(), key=lambda item: int(item["start"])
        ):
            if not valid_program(programme_data):
                continue

            programme = ET.SubElement(
                root,
                "programme",
                start=xmltv_time(programme_data["start"]),
                stop=xmltv_time(programme_data["end"]),
                channel=tvg_id,
            )
            ET.SubElement(programme, "title", lang="en").text = programme_data["title"]
            if programme_data.get("category"):
                ET.SubElement(programme, "category", lang="en").text = programme_data["category"]
            total_programmes += 1

    # Replace output files only after both temporary files have been written.
    temp_epg = EPG_FILE.with_suffix(EPG_FILE.suffix + ".tmp")
    temp_channels = CHANNELS_FILE.with_suffix(CHANNELS_FILE.suffix + ".tmp")

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(temp_epg, encoding="utf-8", xml_declaration=True)

    with temp_channels.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["tvg_id", "name", "slug", "logo", "category", "index"],
        )
        writer.writeheader()
        writer.writerows(rows)

    temp_epg.replace(EPG_FILE)
    temp_channels.replace(CHANNELS_FILE)
    return total_programmes


def main():
    try:
        channels = scrape()
        total = sum(len(channel["programs"]) for channel in channels.values())

        # Never replace a previously good EPG with an empty scrape.
        if not channels or total == 0:
            print(
                "لم يتم استخراج أي برنامج صالح؛ تم الاحتفاظ بالملفات السابقة.",
                file=sys.stderr,
            )
            sys.exit(1)

        written = build_outputs(channels)
        print("\nتم الاستخراج بنجاح.")
        print(f"معرّفات القنوات: {len(channels)}")
        print(f"البرامج: {written}")
        print(f"XMLTV: {EPG_FILE.resolve()}")
        print(f"CSV:   {CHANNELS_FILE.resolve()}")

    except Exception as exc:
        print(f"فشل الاستخراج: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
