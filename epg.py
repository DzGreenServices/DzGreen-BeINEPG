"""Scrape beIN TV Guide into epg.xml and channels.csv using section + channel index."""

import csv
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
EPG_FILE = Path("epg.xml")
CHANNELS_FILE = Path("channels.csv")

PAGE_TIMEOUT = 60000
UPDATE_WAIT_MS = 1000
GUIDE_WAIT_MS = 12000
ROW_WAIT_MS = 900
MAX_DAYS = 4
CATEGORY_CHANGE_WAIT_MS = 8000
DAY_CHANGE_WAIT_MS = 15000

# Algeria uses UTC+1 year-round, even when the script runs on GitHub.
ALGERIA_TZ = timezone(timedelta(hours=1))


DAY_DATES_JS = r"""
() => [...document.querySelectorAll('.day-cell')].map(e => e.dataset.date || '')
"""

PROGRAM_SIGNATURE_JS = r"""
() => [...document.querySelectorAll('.channel-row')].map(row => {
    const blocks = [...row.querySelectorAll('.prog-block')];
    const signature = blocks.map(b => [
        b.dataset.startMs || '',
        b.dataset.endMs || '',
        b.dataset.fullTitle || ''
    ].join(':')).join('~');
    return (row.dataset.channelIndex || '') + '[' + blocks.length + ']' + signature;
}).join('|')
"""

DAY_CHANGED_JS = r"""
({date, before}) => {
    const target = [...document.querySelectorAll('.day-cell')]
        .find(e => (e.dataset.date || '') === date);

    if (!target || !target.classList.contains('active')) return false;

    const signature = [...document.querySelectorAll('.channel-row')].map(row => {
        const blocks = [...row.querySelectorAll('.prog-block')];
        const rowSignature = blocks.map(b => [
            b.dataset.startMs || '',
            b.dataset.endMs || '',
            b.dataset.fullTitle || ''
        ].join(':')).join('~');

        return (row.dataset.channelIndex || '') +
            '[' + blocks.length + ']' + rowSignature;
    }).join('|');

    return signature !== before;
}
"""

CATEGORY_SIGNATURE_JS = r"""
() => {
    const rows = document.querySelectorAll('.channel-row');
    const img = document.querySelector('.channel-row .channel-col img');
    return rows.length + '|' + (img ? (img.currentSrc || img.src || '') : '');
}
"""

CATEGORY_CHANGED_JS = r"""
before => {
    const rows = document.querySelectorAll('.channel-row');
    const img = document.querySelector('.channel-row .channel-col img');
    const now = rows.length + '|' + (img ? (img.currentSrc || img.src || '') : '');
    return rows.length > 0 && now !== before;
}
"""


ROW_JS = r"""
row => {
    const a = row.querySelector('.channel-col a[href]');
    const img = row.querySelector('.channel-col img');
    const label = row.querySelector(
        '[data-channel-name], .channel-name, .channel-title'
    );
    const alt = img ? (img.getAttribute('alt') || '').trim() : '';
    const usefulAlt = alt && !/^(channel\s+)?logo$/i.test(alt) ? alt : '';
    const labelText = label ? (label.textContent || '').trim() : '';

    return {
        index: row.getAttribute('data-channel-index') || '',
        href: a ? a.href : '',
        name: labelText || usefulAlt,
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
    return !row || row.querySelectorAll('.prog-block').length > 0;
}
"""

READ_ROW_JS = r"""
i => {
    const row = document.querySelector(
        '.channel-row[data-channel-index="' + String(i) + '"]'
    );
    if (!row) return null;
    const read = ROW_READER_PLACEHOLDER;
    return read(row);
}
""".replace("ROW_READER_PLACEHOLDER", ROW_JS.strip())


def category_key(label, position):
    """Build a compact section key used in stable TVG IDs."""
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
    """Return the final URL path segment, if a channel link exists."""
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
    """Merge one row under section_index, even if href/name are absent."""
    raw_index = str(row.get("index", "")).strip()

    if not raw_index.isdigit():
        return 0

    index = str(int(raw_index))
    tvg_id = f"{section_key}_{index}"
    slug = slug_from_href(row.get("href", ""))
    row_name = (row.get("name") or "").strip()
    name = row_name or slug
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

        # Deduplicate programmes repeated across overlapping guide days.
        channel["programs"][start_key] = {
            "title": program["title"].strip(),
            "category": (program.get("category") or "").strip(),
            "start": start_key,
            "end": str(int(program["end"])),
        }

        added += 1

    return added


def collect_all_rows(page, channels, section, section_key):
    """Scroll through the current section and collect each channel index."""
    indexes = page.evaluate(ROW_INDEXES_JS)
    indexes = sorted({int(index) for index in indexes})
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
                    timeout=ROW_WAIT_MS,
                )
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


def wait_for_guide(page):
    """Wait for programme elements after selecting a category or date."""
    try:
        page.wait_for_selector(
            ".channel-row .prog-block",
            state="attached",
            timeout=GUIDE_WAIT_MS,
        )
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(UPDATE_WAIT_MS)


def has_class(locator, name):
    """True if the element has the requested CSS class."""
    return name in (locator.get_attribute("class") or "").split()


def wait_until_changed(page, script, before, timeout_ms):
    """Return True when the supplied page condition becomes true."""
    try:
        page.wait_for_function(
            script,
            arg=before,
            timeout=timeout_ms,
        )
        return True
    except PlaywrightTimeoutError:
        return False


def open_category(page, tab):
    """Select a section and wait for its channel rows to change."""
    already_active = has_class(tab, "active")

    if not already_active:
        before = page.evaluate(CATEGORY_SIGNATURE_JS)
        tab.click()

        if not wait_until_changed(
            page,
            CATEGORY_CHANGED_JS,
            before,
            CATEGORY_CHANGE_WAIT_MS,
        ):
            raise RuntimeError(
                "لم يتغير قسم القنوات بعد النقر على تبويبه."
            )

    wait_for_guide(page)


def open_day(page, date):
    """Click the exact YYYY-MM-DD day and wait for its programmes to change."""
    selector = f'.day-cell[data-date="{date}"]'
    day = page.locator(selector)

    if day.count() == 0:
        available = page.evaluate(DAY_DATES_JS)
        raise RuntimeError(
            f"تاريخ {date} غير موجود في شريط الأيام. "
            f"التواريخ الظاهرة: {available}"
        )

    day = day.first

    # If the requested date is already active, do not click it again.
    if has_class(day, "active"):
        wait_for_guide(page)
        return

    before = page.evaluate(PROGRAM_SIGNATURE_JS)
    day.click()

    changed = wait_until_changed(
        page,
        DAY_CHANGED_JS,
        {"date": date, "before": before},
        DAY_CHANGE_WAIT_MS,
    )

    if not changed:
        raise RuntimeError(
            f"تم النقر على تاريخ {date} لكن لم يتأكد تحديث البرامج؛ "
            "أوقفت الاستخراج لحماية ملفات EPG السابقة."
        )

    wait_for_guide(page)


def scrape():
    """Collect today's guide plus the next three days."""
    channels = {}

    today = datetime.now(ALGERIA_TZ).date()

    target_dates = [
        (today + timedelta(days=offset)).isoformat()
        for offset in range(MAX_DAYS)
    ]

    print(f"تاريخ اليوم بتوقيت الجزائر: {target_dates[0]}")
    print("تواريخ الاستخراج: " + ", ".join(target_dates))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        try:
            page = browser.new_page()
            page.set_default_timeout(PAGE_TIMEOUT)
            page.goto(URL, wait_until="domcontentloaded")
            page.wait_for_selector(".category-tab", timeout=PAGE_TIMEOUT)

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

                open_category(page, tab)

                # The day strip is read again for every category because
                # switching tabs may re-render its date cells.
                available_dates = page.evaluate(DAY_DATES_JS)
                missing_dates = [
                    date for date in target_dates
                    if date not in available_dates
                ]

                if missing_dates:
                    raise RuntimeError(
                        f"قسم {section or section_key} لا يعرض كل التواريخ المطلوبة. "
                        f"المفقود: {missing_dates}; الظاهر: {available_dates}. "
                        "لم تُستبدل ملفات EPG السابقة."
                    )

                for day_number, date in enumerate(target_dates, start=1):
                    open_day(page, date)

                    row_count, empty = collect_all_rows(
                        page,
                        channels,
                        section,
                        section_key,
                    )

                    print(
                        f"{section or section_key}, {date} "
                        f"({day_number}/{len(target_dates)}): "
                        f"{row_count} indexes "
                        f"({empty} without programmes), "
                        f"{len(channels)} channel IDs"
                    )

        finally:
            browser.close()

    return channels


def xmltv_time(ms):
    """Convert Unix milliseconds to an XMLTV UTC timestamp."""
    dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    return dt.strftime("%Y%m%d%H%M%S +0000")


def build_outputs(channels):
    """Write XMLTV and CSV using matching section_index TVG IDs."""
    root = ET.Element(
        "tv",
        {"generator-info-name": "bein-epg-index-scraper"},
    )

    rows = []

    # XMLTV channel declarations must precede programme entries.
    for tvg_id, channel in sorted(
        channels.items(),
        key=lambda pair: (
            pair[1]["category"].lower(),
            int(pair[1]["index"]),
        ),
    ):
        element = ET.SubElement(root, "channel", id=tvg_id)

        ET.SubElement(
            element,
            "display-name",
        ).text = channel.get("name", "")

        if channel.get("logo"):
            ET.SubElement(
                element,
                "icon",
                src=channel["logo"],
            )

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
        programs = sorted(
            channel["programs"].values(),
            key=lambda item: int(item["start"]),
        )

        for programme_data in programs:
            if not valid_program(programme_data):
                continue

            programme = ET.SubElement(
                root,
                "programme",
                start=xmltv_time(programme_data["start"]),
                stop=xmltv_time(programme_data["end"]),
                channel=tvg_id,
            )

            ET.SubElement(
                programme,
                "title",
                lang="en",
            ).text = programme_data["title"]

            if programme_data.get("category"):
                ET.SubElement(
                    programme,
                    "category",
                    lang="en",
                ).text = programme_data["category"]

            total_programmes += 1

    # Write temporary files first to preserve the old files if scraping fails.
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
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "tvg_id",
                "name",
                "slug",
                "logo",
                "category",
                "index",
            ],
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
            len(channel["programs"])
            for channel in channels.values()
        )

        # Never replace older files if the scrape contains no valid programmes.
        if not channels or total == 0:
            print(
                "لم يتم استخراج أي برنامج صالح؛ "
                "تم الاحتفاظ بالملفات السابقة.",
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
