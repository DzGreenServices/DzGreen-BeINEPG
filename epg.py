"""Scrape beIN TV Guide into separate sports and entertainment XMLTV files."""

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

try:
    from zoneinfo import ZoneInfo
    ALGERIA_TZ = ZoneInfo("Africa/Algiers")
except Exception:
    ALGERIA_TZ = timezone(timedelta(hours=1))


URL = "https://www.bein.com/en/tv-guide/?c=dz"

SPORTS_XML = Path("epg_sports.xml")
ENTERTAINMENT_XML = Path("epg_entertainment.xml")

PAST_HOURS = 1
FUTURE_HOURS = 24

PAGE_TIMEOUT_MS = 60000
CATEGORY_TIMEOUT_MS = 10000
DAY_TIMEOUT_MS = 15000
GUIDE_TIMEOUT_MS = 12000
ROW_TIMEOUT_MS = 1800
SETTLE_MS = 500


DATES_JS = r"""
() => [...document.querySelectorAll('.day-cell')]
    .map(e => e.dataset.date || '')
"""


PROGRAM_SIGNATURE_JS = r"""
() => [...document.querySelectorAll('.channel-row')].map(row =>
    (row.dataset.channelIndex || '') + ':' +
    [...row.querySelectorAll('.prog-block')].map(p => [
        p.dataset.startMs || '',
        p.dataset.endMs || '',
        p.dataset.fullTitle || ''
    ].join(',')).join('~')
).join('|')
"""


WAIT_DAY_JS = r"""
({date, before}) => {
    const day = [...document.querySelectorAll('.day-cell')]
        .find(e => (e.dataset.date || '') === date);

    if (!day || !day.classList.contains('active')) return false;

    const current = [...document.querySelectorAll('.channel-row')].map(row =>
        (row.dataset.channelIndex || '') + ':' +
        [...row.querySelectorAll('.prog-block')].map(p => [
            p.dataset.startMs || '',
            p.dataset.endMs || '',
            p.dataset.fullTitle || ''
        ].join(',')).join('~')
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
before => {
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

    const href = link ? link.href : '';
    const path = href ? new URL(href).pathname.replace(/\/+$/, '') : '';
    const slug = path ? path.split('/').pop() : '';

    return {
        index: row.getAttribute('data-channel-index') || '',
        href,
        name: (label ? (label.textContent || '').trim() : '') || usefulAlt || slug,
        slug,
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


INDEXES_JS = r"""
() => [...new Set(
    [...document.querySelectorAll('.channel-row')]
        .map(row => row.getAttribute('data-channel-index'))
        .filter(value => value !== null && /^\d+$/.test(value))
)].map(Number).sort((a, b) => a - b)
"""


SCROLL_ROW_JS = r"""
i => {
    const row = document.querySelector(
        '.channel-row[data-channel-index="' + i + '"]'
    );

    if (!row) return false;

    row.scrollIntoView({block: 'center'});
    return true;
}
"""


ROW_READY_JS = r"""
i => {
    const row = document.querySelector(
        '.channel-row[data-channel-index="' + i + '"]'
    );

    return !!row && row.querySelector('.prog-block') !== null;
}
"""


READ_ROW_JS = (
    "i => { const row = document.querySelector("
    "'.channel-row[data-channel-index=\"' + i + '\"]'); "
    "return row ? (" + ROW_JS.strip() + ")(row) : null; }"
)


def section_key(label, position):
    """Identify the sports and entertainment tabs."""
    compact = re.sub(
        r"[^a-z0-9]+",
        "",
        (label or "").strip().lower(),
    )

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
    """Reject incomplete or invalid programme records."""
    try:
        title = (program.get("title") or "").strip()
        start = int(program.get("start", ""))
        end = int(program.get("end", ""))

        return bool(title) and start > 0 and end > start

    except (AttributeError, TypeError, ValueError):
        return False


def add_row(
    row,
    key,
    section_name,
    channels,
    window_start_ms,
    window_end_ms,
):
    """Add a row under its section/index ID and keep programmes in the window."""
    raw_index = str(row.get("index", "")).strip()

    if not raw_index.isdigit():
        return 0

    index = str(int(raw_index))

    # Special test: BeIN Movies 1 gets the ID used in the M3U.
    # The M3U tvg-id must also be changed to exactly "beINMOVIES1".
    if key == "entertainment" and index == "0":
        tvg_id = "beINMOVIES1"
    else:
        tvg_id = f"{key}_{index}"

    href = (row.get("href") or "").strip()
    path = urlparse(href).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1] if path else (
        row.get("slug") or ""
    ).strip()

    name = (row.get("name") or "").strip() or slug
    logo = (row.get("logo") or "").strip()

    channel = channels.setdefault(
        tvg_id,
        {
            "tvg_id": tvg_id,
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
    """Wait for the guide and its programme blocks to appear."""
    try:
        page.wait_for_selector(
            ".channel-row",
            state="attached",
            timeout=GUIDE_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        pass

    try:
        page.wait_for_selector(
            ".channel-row .prog-block",
            state="attached",
            timeout=GUIDE_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(SETTLE_MS)


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


def choose_category(page, position):
    """Select a category and wait for its channel rows to change."""
    tab = page.locator(".category-tab").nth(position)
    classes = (tab.get_attribute("class") or "").split()

    if "active" not in classes:
        before = page.evaluate(CATEGORY_SIGNATURE_JS)
        tab.click()

        changed = wait_for_condition(
            page,
            CATEGORY_CHANGED_JS,
            before,
            CATEGORY_TIMEOUT_MS,
        )

        if not changed:
            raise RuntimeError(
                f"لم يتأكد تغيير القسم إلى الخيار رقم {position + 1}."
            )

    wait_for_guide(page)
    return tab.inner_text().strip()


def choose_day(page, date):
    """Click an exact date and confirm the programme data changes."""
    day = page.locator(f'.day-cell[data-date="{date}"]').first

    if day.count() == 0:
        available = page.evaluate(DATES_JS)

        raise RuntimeError(
            f"التاريخ {date} غير موجود. التواريخ المتاحة: {available}"
        )

    if "active" in (day.get_attribute("class") or "").split():
        wait_for_guide(page)
        return

    before = page.evaluate(PROGRAM_SIGNATURE_JS)
    day.click()

    changed = wait_for_condition(
        page,
        WAIT_DAY_JS,
        {"date": date, "before": before},
        DAY_TIMEOUT_MS,
    )

    if not changed:
        raise RuntimeError(
            f"لم يتأكد تحديث برامج التاريخ {date}. "
            "أوقفت العملية لحماية الملفات السابقة."
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
    """Scroll through all channel indexes and collect their programmes."""
    indexes = page.evaluate(INDEXES_JS)
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

        except Exception as exc:
            print(
                f"  index {index}: تم تخطي الصف "
                f"({str(exc).splitlines()[0]})"
            )
            empty_rows += 1

    return len(indexes), empty_rows


def xmltv_time(milliseconds):
    """Convert Unix milliseconds to an XMLTV UTC timestamp."""
    return datetime.fromtimestamp(
        int(milliseconds) / 1000,
        tz=timezone.utc,
    ).strftime("%Y%m%d%H%M%S +0000")


def build_xml(channels):
    """Build XMLTV data in memory before touching output files."""
    programme_count = sum(
        len(channel["programs"])
        for channel in channels.values()
    )

    if programme_count == 0:
        raise RuntimeError(
            "لا توجد برامج صالحة لهذه الفئة؛ لم يتم استبدال الملف السابق."
        )

    root = ET.Element(
        "tv",
        {"generator-info-name": "bein-epg-split"},
    )

    ordered = sorted(
        channels.items(),
        key=lambda item: int(item[1]["index"]),
    )

    # Write channel declarations before programme entries.
    for tvg_id, channel in ordered:
        element = ET.SubElement(
            root,
            "channel",
            {"id": tvg_id},
        )

        # Use the ID as a display-name fallback if the site has no name.
        ET.SubElement(element, "display-name").text = (
            channel["name"] or tvg_id
        )

        if channel["logo"]:
            ET.SubElement(
                element,
                "icon",
                {"src": channel["logo"]},
            )

    for tvg_id, channel in ordered:
        programs = sorted(
            channel["programs"].values(),
            key=lambda item: int(item["start"]),
        )

        for program in programs:
            if not valid_program(program):
                continue

            item = ET.SubElement(
                root,
                "programme",
                {
                    "start": xmltv_time(program["start"]),
                    "stop": xmltv_time(program["end"]),
                    "channel": tvg_id,
                },
            )

            ET.SubElement(
                item,
                "title",
                {"lang": "en"},
            ).text = program["title"]

            if program["category"]:
                ET.SubElement(
                    item,
                    "category",
                    {"lang": "en"},
                ).text = program["category"]

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    return tree


def write_both_xml(sports, entertainment):
    """Validate both outputs before replacing either previous file."""
    outputs = [
        (SPORTS_XML, sports),
        (ENTERTAINMENT_XML, entertainment),
    ]

    temporary_paths = []

    try:
        for destination, channels in outputs:
            tree = build_xml(channels)

            temp_path = destination.with_suffix(
                destination.suffix + ".tmp"
            )

            tree.write(
                temp_path,
                encoding="utf-8",
                xml_declaration=True,
            )

            # Parse the temporary file before accepting it.
            ET.parse(temp_path)
            temporary_paths.append((temp_path, destination))

        for temp_path, destination in temporary_paths:
            temp_path.replace(destination)

    finally:
        for temp_path, _destination in temporary_paths:
            if temp_path.exists():
                temp_path.unlink()

    for destination, channels in outputs:
        count = sum(
            len(channel["programs"])
            for channel in channels.values()
        )

        print(
            f"{destination}: {len(channels)} channels, "
            f"{count} programmes"
        )


def dates_between(start_date, end_date):
    """Return ISO dates between two dates, inclusive."""
    dates = []
    day = start_date

    while day <= end_date:
        dates.append(day.isoformat())
        day += timedelta(days=1)

    return dates


def scrape():
    """Collect sports and entertainment programmes for the rolling time window."""
    now = datetime.now(ALGERIA_TZ)

    window_start = now - timedelta(hours=PAST_HOURS)
    window_end = now + timedelta(hours=FUTURE_HOURS)

    start_ms = int(window_start.timestamp() * 1000)
    end_ms = int(window_end.timestamp() * 1000)

    candidate_dates = dates_between(
        window_start.date(),
        window_end.date(),
    )

    required_dates = dates_between(
        now.date(),
        window_end.date(),
    )

    print(f"توقيت الجزائر: {now:%Y-%m-%d %H:%M:%S}")
    print(
        f"نافذة البرامج: {window_start:%Y-%m-%d %H:%M} "
        f"إلى {window_end:%Y-%m-%d %H:%M}"
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
                    "لم يتم العثور على أقسام القنوات."
                )

            found_sections = set()

            for position in range(tab_count):
                section_name = choose_category(page, position)
                key = section_key(section_name, position)

                if key not in results or key in found_sections:
                    print(
                        f"تم تجاهل القسم غير المعروف أو المكرر: "
                        f"{section_name}"
                    )
                    continue

                found_sections.add(key)

                # Switching category can recreate the date strip.
                available_dates = page.evaluate(DATES_JS)

                missing_required = [
                    date for date in required_dates
                    if date not in available_dates
                ]

                if missing_required:
                    raise RuntimeError(
                        f"قسم {section_name} لا يعرض التواريخ المطلوبة "
                        f"{missing_required}. المتاح: {available_dates}. "
                        "لم تُكتب ملفات جديدة."
                    )

                dates_to_read = [
                    date for date in candidate_dates
                    if date in available_dates
                ]

                if window_start.date().isoformat() not in available_dates:
                    print(
                        f"تنبيه: قسم {section_name} لا يعرض تاريخ الساعة "
                        "السابقة؛ سأقرأ التواريخ المتاحة."
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
                        f"{section_name} | {date}: {row_count} صفوف، "
                        f"{empty_count} صفوف بلا برامج ضمن النافذة"
                    )

            missing_sections = set(results) - found_sections

            if missing_sections:
                raise RuntimeError(
                    f"لم يتم التعرف على الأقسام: "
                    f"{sorted(missing_sections)}"
                )

        finally:
            browser.close()

    return results


def main():
    try:
        results = scrape()

        write_both_xml(
            results["sports"],
            results["entertainment"],
        )

        print("تم استخراج ملفي الرياضة والترفيه بنجاح.")
        print(f"الرياضة: {SPORTS_XML.resolve()}")
        print(f"الترفيه: {ENTERTAINMENT_XML.resolve()}")

    except Exception as exc:
        print(f"فشل الاستخراج: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
