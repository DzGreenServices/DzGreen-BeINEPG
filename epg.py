"""Lightweight beIN TV Guide scraper: separate sports and entertainment XMLTV."""

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

ALGERIA_TZ = timezone(timedelta(hours=1))
PAGE_TIMEOUT = 60000
CATEGORY_WAIT = 10000
DAY_WAIT = 15000
GUIDE_WAIT = 12000
ROW_WAIT = 2000

DAY_DATES_JS = r"""
() => [...document.querySelectorAll('.day-cell')].map(day => day.dataset.date || '')
"""

PROGRAM_SIGNATURE_JS = r"""
() => [...document.querySelectorAll('.channel-row')].map(row =>
    [...row.querySelectorAll('.prog-block')].map(p => [
        p.dataset.startMs || '', p.dataset.endMs || '', p.dataset.fullTitle || ''
    ].join(':')).join('~')
).join('|')
"""

DAY_CHANGED_JS = r"""
({date, before}) => {
    const day = [...document.querySelectorAll('.day-cell')]
        .find(item => (item.dataset.date || '') === date);
    if (!day || !day.classList.contains('active')) return false;

    const signature = [...document.querySelectorAll('.channel-row')].map(row =>
        [...row.querySelectorAll('.prog-block')].map(p => [
            p.dataset.startMs || '', p.dataset.endMs || '', p.dataset.fullTitle || ''
        ].join(':')).join('~')
    ).join('|');

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
    const current = rows.length + '|' + (img ? (img.currentSrc || img.src || '') : '');
    return rows.length > 0 && current !== before;
}
"""

ROW_JS = r"""
row => {
    const link = row.querySelector('.channel-col a[href]');
    const img = row.querySelector('.channel-col img');
    const label = row.querySelector('[data-channel-name], .channel-name, .channel-title');
    const alt = img ? (img.getAttribute('alt') || '').trim() : '';

    return {
        index: row.getAttribute('data-channel-index') || '',
        href: link ? link.href : '',
        name: (label ? (label.textContent || '').trim() : '') ||
              (alt && !/^(channel\s+)?logo$/i.test(alt) ? alt : ''),
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

ROW_COUNT_JS = "() => document.querySelectorAll('.channel-row').length"

SCROLL_ROW_JS = r"""
i => {
    const row = document.querySelectorAll('.channel-row')[i];
    if (!row) return false;
    row.scrollIntoView({block: 'center'});
    return true;
}
"""

ROW_READY_JS = r"""
i => {
    const row = document.querySelectorAll('.channel-row')[i];
    return !!row && row.querySelectorAll('.prog-block').length > 0;
}
"""

READ_ROW_JS = (
    "i => { const row = document.querySelectorAll('.channel-row')[i]; "
    "return row ? (" + ROW_JS + ")(row) : null; }"
)


def section_key(label, position):
    text = (label or '').strip().lower()
    compact = re.sub(r'[^a-z0-9]+', '', text)

    if 'sport' in compact:
        return 'sports'
    if 'entertain' in compact:
        return 'entertainment'

    return 'sports' if position == 0 else 'entertainment' if position == 1 else ''


def valid_program(program):
    try:
        return (
            bool((program.get('title') or '').strip())
            and int(program.get('start', '')) > 0
            and int(program.get('end', '')) > int(program.get('start', ''))
        )
    except (TypeError, ValueError):
        return False


def add_row(row, key, section_name, channels, window_start_ms, window_end_ms):
    raw_index = str(row.get('index', '')).strip()
    if not raw_index.isdigit():
        return 0

    index = str(int(raw_index))
    tvg_id = f'{key}_{index}'

    path = urlparse((row.get('href') or '').strip()).path.rstrip('/')
    slug = path.rsplit('/', 1)[-1] if path else ''

    name = (row.get('name') or '').strip() or slug
    logo = (row.get('logo') or '').strip()

    channel = channels.setdefault(tvg_id, {
        'name': name,
        'slug': slug,
        'logo': logo,
        'section': section_name,
        'index': index,
        'programs': {},
    })

    if not channel['name'] and name:
        channel['name'] = name
    if not channel['slug'] and slug:
        channel['slug'] = slug
    if not channel['logo'] and logo:
        channel['logo'] = logo

    added = 0

    for program in row.get('programs', []):
        if not valid_program(program):
            continue

        start = int(program['start'])
        end = int(program['end'])

        # Keep programmes overlapping the rolling time window.
        if end <= window_start_ms or start >= window_end_ms:
            continue

        start_key = str(start)
        channel['programs'][start_key] = {
            'title': program['title'].strip(),
            'category': (program.get('category') or '').strip(),
            'start': start_key,
            'end': str(end),
        }
        added += 1

    return added


def wait_for_guide(page):
    try:
        page.wait_for_selector(
            '.channel-row',
            state='attached',
            timeout=GUIDE_WAIT,
        )
        page.wait_for_selector(
            '.channel-row .prog-block',
            state='attached',
            timeout=GUIDE_WAIT,
        )
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(500)


def wait_until_changed(page, expression, argument, timeout):
    try:
        page.wait_for_function(
            expression,
            arg=argument,
            timeout=timeout,
        )
        return True
    except PlaywrightTimeoutError:
        return False


def choose_section(page, tab):
    if 'active' in (tab.get_attribute('class') or '').split():
        wait_for_guide(page)
        return

    before = page.evaluate(CATEGORY_SIGNATURE_JS)
    tab.click()

    if not wait_until_changed(
        page, CATEGORY_CHANGED_JS, before, CATEGORY_WAIT
    ):
        raise RuntimeError('لم يتغير محتوى القنوات بعد اختيار القسم.')

    wait_for_guide(page)


def choose_day(page, date):
    day = page.locator(f'.day-cell[data-date="{date}"]').first

    if day.count() == 0:
        raise RuntimeError(f'الموقع لا يعرض التاريخ المطلوب: {date}')

    if 'active' in (day.get_attribute('class') or '').split():
        wait_for_guide(page)
        return

    before = page.evaluate(PROGRAM_SIGNATURE_JS)
    day.click()

    if not wait_until_changed(
        page,
        DAY_CHANGED_JS,
        {'date': date, 'before': before},
        DAY_WAIT,
    ):
        raise RuntimeError(
            f'تم النقر على {date} لكن لم يتأكد تحديث البرامج؛ '
            'لم أكتب ملفات جديدة.'
        )

    wait_for_guide(page)


def collect_rows(page, key, section_name, channels, start_ms, end_ms):
    total = page.evaluate(ROW_COUNT_JS)
    empty = 0

    for row_position in range(total):
        try:
            if not page.evaluate(SCROLL_ROW_JS, row_position):
                empty += 1
                continue

            try:
                page.wait_for_function(
                    ROW_READY_JS,
                    arg=row_position,
                    timeout=ROW_WAIT,
                )
            except PlaywrightTimeoutError:
                pass

            row = page.evaluate(READ_ROW_JS, row_position)

            if not row or add_row(
                row, key, section_name, channels, start_ms, end_ms
            ) == 0:
                empty += 1

        except PlaywrightError as exc:
            print(
                f'  صف {row_position}: تم تخطيه '
                f'({str(exc).splitlines()[0]})'
            )
            empty += 1

    return total, empty


def xml_time(ms):
    return datetime.fromtimestamp(
        int(ms) / 1000,
        timezone.utc,
    ).strftime('%Y%m%d%H%M%S +0000')


def write_xml(path, channels):
    program_count = sum(
        len(channel['programs'])
        for channel in channels.values()
    )

    if program_count == 0:
        print(
            f'تحذير: لا توجد برامج صالحة؛ احتفظت بالملف السابق '
            f'إن وُجد: {path}'
        )
        return False

    root = ET.Element(
        'tv',
        {'generator-info-name': 'bein-epg-split'},
    )

    ordered = sorted(
        channels.items(),
        key=lambda item: int(item[1]['index']),
    )

    for tvg_id, channel in ordered:
        element = ET.SubElement(root, 'channel', id=tvg_id)

        # Use the TVG ID as a fallback when the channel name is unknown.
        ET.SubElement(element, 'display-name').text = (
            channel['name'] or tvg_id
        )

        if channel['logo']:
            ET.SubElement(element, 'icon', src=channel['logo'])

    for tvg_id, channel in ordered:
        programs = sorted(
            channel['programs'].values(),
            key=lambda item: int(item['start']),
        )

        for program in programs:
            element = ET.SubElement(root, 'programme', {
                'start': xml_time(program['start']),
                'stop': xml_time(program['end']),
                'channel': tvg_id,
            })

            ET.SubElement(
                element, 'title', lang='en'
            ).text = program['title']

            if program['category']:
                ET.SubElement(
                    element, 'category', lang='en'
                ).text = program['category']

    temp = path.with_suffix(path.suffix + '.tmp')
    tree = ET.ElementTree(root)
    ET.indent(tree, space='  ')
    tree.write(temp, encoding='utf-8', xml_declaration=True)
    temp.replace(path)

    print(f'{path}: {len(channels)} قناة، {program_count} برنامج')
    return True


def scrape():
    now = datetime.now(ALGERIA_TZ)
    window_start = now - timedelta(hours=1)
    window_end = now + timedelta(hours=24)

    start_ms = int(window_start.timestamp() * 1000)
    end_ms = int(window_end.timestamp() * 1000)

    # Required dates: today through the end of the next 24 hours.
    required_dates = []
    day = now.date()
    while day <= window_end.date():
        required_dates.append(day.isoformat())
        day += timedelta(days=1)

    # If the previous date is available, scan it too to catch programmes
    # that started during the hour before this run.
    candidate_dates = []
    day = window_start.date()
    while day <= window_end.date():
        candidate_dates.append(day.isoformat())
        day += timedelta(days=1)

    print(f'توقيت الجزائر: {now:%Y-%m-%d %H:%M:%S}')
    print(
        f'نافذة الاستخراج: {window_start:%Y-%m-%d %H:%M} '
        f'إلى {window_end:%Y-%m-%d %H:%M}'
    )

    output = {
        'sports': {},
        'entertainment': {},
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        try:
            page = browser.new_page()
            page.set_default_timeout(PAGE_TIMEOUT)
            page.goto(URL, wait_until='domcontentloaded')
            page.wait_for_selector(
                '.category-tab',
                timeout=PAGE_TIMEOUT,
            )

            count = page.locator('.category-tab').count()
            if not count:
                raise RuntimeError('لم يتم العثور على أقسام القنوات.')

            for position in range(count):
                tab = page.locator('.category-tab').nth(position)
                name = tab.inner_text().strip()
                key = section_key(name, position)

                if key not in output:
                    print(f'تم تجاهل القسم غير المعروف: {name}')
                    continue

                choose_section(page, tab)

                available = page.evaluate(DAY_DATES_JS)
                missing = [
                    date for date in required_dates
                    if date not in available
                ]

                if missing:
                    raise RuntimeError(
                        f'قسم {name}: التواريخ المطلوبة غير موجودة '
                        f'{missing}. المتاح: {available}. '
                        'لم أكتب ملفات جديدة.'
                    )

                dates = [
                    date for date in candidate_dates
                    if date in available
                ]

                if (
                    window_start.date() < now.date()
                    and window_start.date().isoformat() not in available
                ):
                    print(
                        'ملاحظة: الموقع لا يعرض تاريخ الساعة السابقة؛ '
                        'سأستخرج البرامج المتاحة وأحتفظ بالبرنامج الجاري '
                        'إذا ظهر في البيانات.'
                    )

                for date
