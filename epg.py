"""Lightweight beIN EPG scraper: separate sports and entertainment XMLTV files."""

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from playwright.sync_api import (
    sync_playwright,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

URL = "https://www.bein.com/en/tv-guide/?c=dz"
SPORTS_XML = Path("epg_sports.xml")
ENTERTAINMENT_XML = Path("epg_entertainment.xml")

# Algeria is UTC+1 year-round.
ALGERIA_TZ = timezone(timedelta(hours=1))

PAGE_TIMEOUT_MS = 60000
CATEGORY_TIMEOUT_MS = 10000
DAY_TIMEOUT_MS = 15000
GUIDE_TIMEOUT_MS = 12000
ROW_TIMEOUT_MS = 1800

WINDOW_BEFORE = timedelta(hours=1)
WINDOW_AFTER = timedelta(hours=24)


DAY_DATES_JS = r"""
() => [...document.querySelectorAll('.day-cell')]
    .map(e => e.dataset.date || '')
"""

PROGRAM_SIGNATURE_JS = r"""
() => [...document.querySelectorAll('.channel-row')].map(row =>
    [...row.querySelectorAll('.prog-block')].map(p => [
        p.dataset.startMs || '',
        p.dataset.endMs || '',
        p.dataset.fullTitle || ''
    ].join(':')).join('~')
).join('|')
"""

DAY_CHANGED_JS = r"""
({date, before}) => {
    const day = [...document.querySelectorAll('.day-cell')]
        .find(e => (e.dataset.date || '') === date);

    if (!day || !day.classList.contains('active')) return false;

    const current = [...document.querySelectorAll('.channel-row')].map(row =>
        [...row.querySelectorAll('.prog-block')].map(p => [
            p.dataset.startMs || '',
            p.dataset.endMs || '',
            p.dataset.fullTitle || ''
        ].join(':')).join('~')
    ).join('|');

    return current !== before;
}
"""

CATEGORY_SIGNATURE_JS = r"""
() => {
    const rows = document.querySelectorAll('.channel-row');
    const img = document.querySelector('.channel-row .channel-col img');

    return rows.length + '|' +
        (img ? (img.currentSrc || img.src || '') : '');
}
"""

CATEGORY_CHANGED_JS = r"""
({position, before}) => {
    const tabs = document.querySelectorAll('.category-tab');
    const tab = tabs[position];

    if (!tab || !tab.classList.contains('active')) return false;

    const rows = document.querySelectorAll('.channel-row');
    const img = document.querySelector('.channel-row .channel-col img');

    const current = rows.length + '|' +
        (img ? (img.currentSrc || img.src || '') : '');

    return rows.length > 0 && current !== before;
}
"""

ROW_JS = r"""
row => {
    const link = row.querySelector('.channel-col a[href]');
    const img = row.querySelector('.channel-col img');

    const label = row.querySelector(
        '[data-channel-name], .channel-name, .channel-title'
    );

    const alt = img ? (img.getAttribute('alt') || '').trim() : '';
    const usefulAlt = alt && !/^(channel\s+)?logo$/i.test(alt) ? alt : '';

    return {
        index: row.getAttribute('data-channel-index') || '',
        href: link ? link.href : '',
        name: (label ? (label.textContent || '').trim() : '') || usefulAlt,
        logo: img ? (img.currentSrc || img.src || '') : '',
        programs: [...row.querySelectorAll('.prog-block')].map(p => ({
            title: (p.dataset.fullTitle || '').trim(),
            category: (p.dataset.fullCategory || '').trim(),
            start: p.dataset.startMs || '',
            end: p.dataset.endMs || ''
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
    const row = document.querySelector(
        '.channel-row[data-channel-index="' + String(i) + '"]'
    );

    if (!row) return false;

    row.scrollIntoView({block: 'center'});
    return true;
}
"""

ROW_READY_JS = r"""
i => {
    const row = document.querySelector(
        '.channel-row[data-channel-index="' + String(i) + '"]'
    );

    return !!row && row.querySelectorAll('.prog-block').length > 0;
}
"""

READ_ROW_JS = (
    "i => { "
    "const row = document.querySelector("
    "'.channel-row[data-channel-index=\"' + String(i) + '\"]"
    "'); "
    "return row ? (" + ROW_JS.strip() + ")(row) : null; "
    "}"
)


def section_key(label, position):
    """Return a stable ID prefix for each guide section."""
    compact = re.sub(r"[^a-z0-9]+", "", (label or "").lower())

    if "sport" in compact:
        return "sports"

    if "entertain" in compact:
        return "entertainment"

    if position == 0:
        return "sports"

    if position == 1:
        return "entertainment"

    return ""


def valid_program(program):
    """Reject incomplete programme records."""
    try:
        title = (program.get("title") or "").strip()
        start = int(program.get("start", ""))
        end = int(program.get("end", ""))

        return bool(title) and start > 0 and end > start

    except (TypeError, ValueError):
        return False


def add_row(row, key, section_name, channels, window_start_ms, window_end_ms):
    """Store one channel and its programmes overlapping the time window."""
    raw_index = str(row.get("index", "")).strip()

    if not raw_index.isdigit():
        return 0

    index = str(int(raw_index))
    if key == "entertainment" and index == "0":
    tvg_id = "beINMOVIES1"
else:
    tvg_id = f"{key}_{index}"

    path = urlparse((row.get("href") or "").strip()).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1] if path else ""

    name = (row.get("name") or "").strip() or slug
    logo = (row.get("logo") or "").strip()

    channel = channels.setdefault(
        tvg_id,
        {
            "name": name,
            "slug": slug,
            "logo": logo,
            "section": section_name,
            "index": index,
            "programs": {},
        },
    )

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

        start = int(program["start"])
        end = int(program["end"])

        # Keep programmes overlapping [window_start, window_end).
        if end <= window_start_ms or start >= window_end_ms:
            continue

        start_key = str(start)

        channel["programs"][start_key] = {
            "title": program["title"].strip(),
            "category": (program.get("category") or "").strip(),
            "start": start_key,
            "end": str(end),
        }

        added += 1

    return added


def wait_for_guide(page):
    """Wait for the guide to render after a section or date change."""
    try:
        page.wait_for_selector(
            ".channel-row",
            state="attached",
            timeout=GUIDE_TIMEOUT_MS,
        )

        page.wait_for_selector(
            ".channel-row .prog-block",
            state="attached",
            timeout=GUIDE_TIMEOUT_MS,
        )

    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(500)


def wait_for_condition(page, expression, argument, timeout_ms):
    """Wait for a JavaScript condition, returning False on timeout."""
    try:
        page.wait_for_function(
            expression,
            arg=argument,
            timeout=timeout_ms,
        )
        return True

    except PlaywrightTimeoutError:
        return False


def choose_section(page, position):
    """Select a category and confirm that its content changed."""
    tabs = page.locator(".category-tab")
    tab = tabs.nth(position)

    classes = (tab.get_attribute("class") or "").split()

    if "active" not in classes:
        before = page.evaluate(CATEGORY_SIGNATURE_JS)
        tab.click()

        changed = wait_for_condition(
            page,
            CATEGORY_CHANGED_JS,
            {"position": position, "before": before},
            CATEGORY_TIMEOUT_MS,
        )

        if not changed:
            raise RuntimeError(
                f"Could not confirm category change for tab {position + 1}."
            )

    wait_for_guide(page)

    return tab.inner_text().strip()


def choose_day(page, date):
    """Select an exact date and verify that programme data changed."""
    day = page.locator(f'.day-cell[data-date="{date}"]').first

    if day.count() == 0:
        available = page.evaluate(DAY_DATES_JS)

        raise RuntimeError(
            f"Date {date} is not present in the guide. "
            f"Available dates: {available}"
        )

    if "active" in (day.get_attribute("class") or "").split():
        wait_for_guide(page)
        return

    before = page.evaluate(PROGRAM_SIGNATURE_JS)
    day.click()

    changed = wait_for_condition(
        page,
        DAY_CHANGED_JS,
        {"date": date, "before": before},
        DAY_TIMEOUT_MS,
    )

    if not changed:
        raise RuntimeError(
            f"Clicked {date}, but programme data did not change; "
            "stopping to avoid writing stale guide data."
        )

    wait_for_guide(page)


def collect_rows(
    page,
    key,
    section_name,
    channels,
    window_start_ms,
    window_end_ms,
):
    """Scroll through the channel indexes and collect their programmes."""
    indexes = sorted(
        {int(value) for value in page.evaluate(ROW_INDEXES_JS)}
    )

    empty_rows = 0

    for index in indexes:
        try:
            if not page.evaluate(SCROLL_ROW_JS, index):
                empty_rows += 1
                continue

            try:
                page.wait_for_function(
                    ROW_READY_JS,
                    arg=index,
                    timeout=ROW_TIMEOUT_MS,
                )

            except PlaywrightTimeoutError:
                # Still read the row; it may contain metadata without programmes.
                pass

            row = page.evaluate(READ_ROW_JS, index)

            if not row:
                empty_rows += 1
                continue

            added = add_row(
                row,
                key,
                section_name,
                channels,
                window_start_ms,
                window_end_ms,
            )

            if added == 0:
                empty_rows += 1

        except PlaywrightError as exc:
            print(
                f"  index {index}: skipped row "
                f"({str(exc).splitlines()[0]})"
            )
            empty_rows += 1

    return len(indexes), empty_rows


def xmltv_time(timestamp_ms):
    """Convert Unix milliseconds to an XMLTV UTC timestamp."""
    return datetime.fromtimestamp(
        int(timestamp_ms) / 1000,
        tz=timezone.utc,
    ).strftime("%Y%m%d%H%M%S +0000")


def write_xml(path, channels):
    """Write an XMLTV file atomically, retaining the old file if empty."""
    programme_count = sum(
        len(channel["programs"])
        for channel in channels.values()
    )

    if programme_count == 0:
        print(
            f"Warning: no valid programmes for this section; "
            f"kept existing file: {path}."
        )
        return False

    root = ET.Element(
        "tv",
        {"generator-info-name": "bein-epg-split"},
    )

    ordered = sorted(
        channels.items(),
        key=lambda item: int(item[1]["index"]),
    )

    # Declare channels before their programme entries.
    for tvg_id, channel in ordered:
        element = ET.SubElement(root, "channel", id=tvg_id)

        # Use the ID as a display-name fallback when the channel name is unknown.
        ET.SubElement(element, "display-name").text = (
            channel["name"] or tvg_id
        )

        if channel["logo"]:
            ET.SubElement(
                element,
                "icon",
                src=channel["logo"],
            )

    for tvg_id, channel in ordered:
        programs = sorted(
            channel["programs"].values(),
            key=lambda item: int(item["start"]),
        )

        for program in programs:
            element = ET.SubElement(
                root,
                "programme",
                {
                    "start": xmltv_time(program["start"]),
                    "stop": xmltv_time(program["end"]),
                    "channel": tvg_id,
                },
            )

            ET.SubElement(
                element,
                "title",
                lang="en",
            ).text = program["title"]

            if program["category"]:
                ET.SubElement(
                    element,
                    "category",
                    lang="en",
                ).text = program["category"]

    temp_path = path.with_suffix(path.suffix + ".tmp")

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    tree.write(
        temp_path,
        encoding="utf-8",
        xml_declaration=True,
    )

    # Validate the written XML before replacing the last good copy.
    ET.parse(temp_path)
    temp_path.replace(path)

    print(
        f"{path}: {len(channels)} channels, "
        f"{programme_count} programmes"
    )

    return True


def dates_between(start_date, end_date):
    """Return every ISO date between two dates, inclusive."""
    dates = []
    current = start_date

    while current <= end_date:
        dates.append(current.isoformat())
        current += timedelta(days=1)

    return dates


def scrape():
    """Collect both sections for the rolling time window."""
    now = datetime.now(ALGERIA_TZ)

    window_start = now - WINDOW_BEFORE
    window_end = now + WINDOW_AFTER

    start_ms = int(window_start.timestamp() * 1000)
    end_ms = int(window_end.timestamp() * 1000)

    # The previous date is optional, needed only when the window crosses midnight.
    candidate_dates = dates_between(
        window_start.date(),
        window_end.date(),
    )

    required_dates = dates_between(
        now.date(),
        window_end.date(),
    )

    print(f"Algeria local time: {now:%Y-%m-%d %H:%M:%S}")
    print(
        f"Programme window: {window_start:%Y-%m-%d %H:%M} to "
        f"{window_end:%Y-%m-%d %H:%M} Algeria time"
    )

    results = {
        "sports": {},
        "entertainment": {},
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        try:
            page = browser.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)

            page.goto(
                URL,
                wait_until="domcontentloaded",
            )

            page.wait_for_selector(
                ".category-tab",
                timeout=PAGE_TIMEOUT_MS,
            )

            tab_count = page.locator(".category-tab").count()

            if tab_count == 0:
                raise RuntimeError(
                    "No channel categories were found on the site."
                )

            found_keys = set()

            for position in range(tab_count):
                section_name = choose_section(page, position)
                key = section_key(section_name, position)

                if key not in results or key in found_keys:
                    print(
                        f"Skipped unknown or duplicate section: {section_name}"
                    )
                    continue

                found_keys.add(key)

                # Read the date strip again after switching sections.
                available_dates = page.evaluate(DAY_DATES_JS)

                missing_required = [
                    date for date in required_dates
                    if date not in available_dates
                ]

                if missing_required:
                    raise RuntimeError(
                        f"Section {section_name}: the site does not show "
                        f"all required dates {missing_required}. "
                        f"Available: {available_dates}. "
                        "No new files were written."
                    )

                dates_to_read = [
                    date for date in candidate_dates
                    if date in available_dates
                ]

                if window_start.date().isoformat() not in available_dates:
                    print(
                        f"Notice: {section_name} does not show the "
                        "previous-hour date; reading available dates only."
                    )

                for date in dates_to_read:
                    choose_day(page, date)

                    row_count, empty_count = collect_rows(
                        page,
                        key,
                        section_name,
                        results[key],
                        start_ms,
                        end_ms,
                    )

                    print(
                        f"{section_name} | {date}: {row_count} rows, "
                        f"{empty_count} rows without programmes in window"
                    )

            missing_sections = set(results) - found_keys

            if missing_sections:
                raise RuntimeError(
                    f"Could not identify these section tabs: "
                    f"{sorted(missing_sections)}"
                )

        finally:
            browser.close()

    return results


def main():
    try:
        results = scrape()

        sports_ok = write_xml(
            SPORTS_XML,
            results["sports"],
        )

        entertainment_ok = write_xml(
            ENTERTAINMENT_XML,
            results["entertainment"],
        )

        if not sports_ok or not entertainment_ok:
            raise RuntimeError(
                "One XML file was not updated because no valid programmes "
                "were found; the previous file was preserved. "
                "Check the log above."
            )

        print("Extraction completed successfully.")
        print(f"Sports XML: {SPORTS_XML.resolve()}")
        print(
            f"Entertainment XML: {ENTERTAINMENT_XML.resolve()}"
        )

    except Exception as exc:
        print(f"Scrape failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
