"""Light beIN guide scraper: rolling Algeria-time EPG -> epg.xml + channels.csv."""

import csv
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

try:
    from zoneinfo import ZoneInfo
    ALGERIA = ZoneInfo("Africa/Algiers")
except Exception:  # fallback for Python installations without timezone data
    ALGERIA = timezone(timedelta(hours=1))

URL = "https://www.bein.com/en/tv-guide/?c=dz"
EPG_FILE = Path("epg.xml")
CSV_FILE = Path("channels.csv")
PAST_HOURS = 1
FUTURE_HOURS = 24
PAGE_TIMEOUT_MS = 60000
DAY_TIMEOUT_MS = 15000
ROW_TIMEOUT_MS = 1500
SETTLE_MS = 400

DATES_JS = r"""() => [...document.querySelectorAll('.day-cell')].map(e => e.dataset.date || '')"""

PROGRAM_SIGNATURE_JS = r"""
() => [...document.querySelectorAll('.channel-row')].map(row =>
  (row.dataset.channelIndex || '') + ':' + [...row.querySelectorAll('.prog-block')]
    .map(b => [b.dataset.startMs || '', b.dataset.endMs || '', b.dataset.fullTitle || ''].join(','))
    .join('~')
).join('|')
"""

WAIT_DAY_JS = r"""
({date, before}) => {
  const day = [...document.querySelectorAll('.day-cell')]
    .find(e => (e.dataset.date || '') === date);
  if (!day || !day.classList.contains('active')) return false;
  const signature = [...document.querySelectorAll('.channel-row')].map(row =>
    (row.dataset.channelIndex || '') + ':' + [...row.querySelectorAll('.prog-block')]
      .map(b => [b.dataset.startMs || '', b.dataset.endMs || '', b.dataset.fullTitle || ''].join(','))
      .join('~')
  ).join('|');
  return signature !== before;
}
"""

ROW_JS = r"""
row => {
  const a = row.querySelector('.channel-col a[href]');
  const img = row.querySelector('.channel-col img');
  const label = row.querySelector('[data-channel-name], .channel-name, .channel-title');
  const alt = img ? (img.getAttribute('alt') || '').trim() : '';
  const usefulAlt = alt && !/^(channel\s+)?logo$/i.test(alt) ? alt : '';
  const href = a ? a.href : '';
  const path = href ? new URL(href).pathname.replace(/\/+$/, '') : '';
  const slug = path ? path.split('/').pop() : '';
  return {
    index: row.getAttribute('data-channel-index') || '',
    name: (label ? (label.textContent || '').trim() : '') || usefulAlt || slug,
    slug,
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

INDEXES_JS = r"""() => [...new Set([...document.querySelectorAll('.channel-row')]
  .map(r => r.getAttribute('data-channel-index'))
  .filter(v => v !== null && /^\d+$/.test(v)))].map(Number).sort((a,b) => a-b)"""

SCROLL_ROW_JS = r"""
i => {
  const row = document.querySelector('.channel-row[data-channel-index="' + i + '"]');
  if (!row) return false;
  row.scrollIntoView({block: 'center'});
  return true;
}
"""

ROW_READY_JS = r"""i => {
  const row = document.querySelector('.channel-row[data-channel-index="' + i + '"]');
  return !row || row.querySelector('.prog-block') !== null;
}"""

READ_ROW_JS = "i => { const row = document.querySelector('.channel-row[data-channel-index=\"' + i + '\"]'); return row ? (" + ROW_JS.strip() + ")(row) : null; }"


def section_key(label, position):
    text = (label or '').strip().lower()
    compact = re.sub(r'[^a-z0-9]+', '', text)
    if 'sport' in compact:
        return 'sports'
    if 'entertain' in compact:
        return 'entertainment'
    key = re.sub(r'[^a-z0-9]+', '_', text).strip('_')
    return key or ('sports' if position == 0 else 'entertainment' if position == 1 else f'category{position + 1}')


def valid_program(program):
    try:
        return bool((program.get('title') or '').strip()) and int(program['start']) > 0 and int(program['end']) > int(program['start'])
    except (KeyError, TypeError, ValueError):
        return False


def add_row(row, section, key, channels):
    raw_index = str(row.get('index', '')).strip()
    if not raw_index.isdigit():
        return 0

    index = str(int(raw_index))
    tvg_id = f'{key}_{index}'
    channel = channels.setdefault(tvg_id, {
        'tvg_id': tvg_id,
        'name': (row.get('name') or '').strip(),
        'slug': (row.get('slug') or '').strip(),
        'logo': (row.get('logo') or '').strip(),
        'section': section,
        'index': index,
        'programs': {},
    })

    for field in ('name', 'slug', 'logo'):
        value = (row.get(field) or '').strip()
        if not channel[field] and value:
            channel[field] = value

    count = 0
    for program in row.get('programs', []):
        if not valid_program(program):
            continue
        start = str(int(program['start']))
        channel['programs'][start] = {
            'title': program['title'].strip(),
            'category': (program.get('category') or '').strip(),
            'start': start,
            'end': str(int(program['end'])),
        }
        count += 1
    return count


def collect_rows(page, channels, section, key):
    indexes = page.evaluate(INDEXES_JS)
    empty = 0
    for index in indexes:
        try:
            if not page.evaluate(SCROLL_ROW_JS, index):
                empty += 1
                continue
            try:
                page.wait_for_function(ROW_READY_JS, arg=index, timeout=ROW_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass
            row = page.evaluate(READ_ROW_JS, index)
            if not row or add_row(row, section, key, channels) == 0:
                empty += 1
        except Exception as exc:
            print(f'  index {index}: skipped ({str(exc).splitlines()[0]})')
            empty += 1
    return len(indexes), empty


def wait_for_guide(page):
    page.wait_for_selector('.channel-row', state='attached', timeout=PAGE_TIMEOUT_MS)
    try:
        page.wait_for_selector('.channel-row .prog-block', state='attached', timeout=12000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(SETTLE_MS)


def choose_category(page, index):
    tabs = page.locator('.category-tab')
    tab = tabs.nth(index)
    classes = (tab.get_attribute('class') or '').split()
    if 'active' not in classes:
        tab.click()
        try:
            page.wait_for_function(
                "i => { const t = document.querySelectorAll('.category-tab')[i]; return !!t && t.classList.contains('active'); }",
                arg=index,
                timeout=8000,
            )
        except PlaywrightTimeoutError:
            raise RuntimeError(f'لم يتأكد تفعيل القسم رقم {index + 1}.')
    wait_for_guide(page)
    return tab.inner_text().strip()


def choose_day(page, date):
    day = page.locator(f'.day-cell[data-date="{date}"]').first
    if day.count() == 0:
        raise RuntimeError(f'التاريخ {date} غير موجود في شريط الأيام.')
    if 'active' in (day.get_attribute('class') or '').split():
        wait_for_guide(page)
        return

    before = page.evaluate(PROGRAM_SIGNATURE_JS)
    day.click()
    try:
        page.wait_for_function(WAIT_DAY_JS, arg={'date': date, 'before': before}, timeout=DAY_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        raise RuntimeError(f'لم يتأكد تحديث برامج التاريخ {date} بعد النقر؛ لم أكتب الملفات.')
    wait_for_guide(page)


def filter_window(channels, start_ms, end_ms):
    removed = 0
    for channel in channels.values():
        channel['programs'] = {
            start: programme
            for start, programme in channel['programs'].items()
            if int(programme['end']) > start_ms and int(programme['start']) < end_ms
        }
        removed += 0  # kept simple; counts are reported below
    return sum(len(c['programs']) for c in channels.values())


def scrape():
    now = datetime.now(ALGERIA)
    start = now - timedelta(hours=PAST_HOURS)
    end = now + timedelta(hours=FUTURE_HOURS)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    wanted = []
    day = start.date()
    while day <= end.date():
        wanted.append(day.isoformat())
        day += timedelta(days=1)

    print(f'وقت الجزائر: {now:%Y-%m-%d %H:%M:%S}')
    print(f'نافذة EPG: {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M}')

    channels = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            page.goto(URL, wait_until='domcontentloaded')
            page.wait_for_selector('.category-tab', timeout=PAGE_TIMEOUT_MS)
            category_count = page.locator('.category-tab').count()
            if not category_count:
                raise RuntimeError('لم يتم العثور على أقسام القنوات.')

            for category_index in range(category_count):
                section = choose_category(page, category_index)
                key = section_key(section, category_index)
                available = page.evaluate(DATES_JS)
                missing = [d for d in wanted if d not in available]

                # Just after midnight, the site may no longer offer yesterday.
                # Warn and continue, but never silently skip today or a future date.
                unavailable_past = [d for d in missing if d < now.date().isoformat()]
                unavailable_now_or_future = [d for d in missing if d >= now.date().isoformat()]
                if unavailable_past:
                    print(f'  تنبيه {key}: الموقع لا يعرض التاريخ السابق {unavailable_past}; سيتم استخدام التواريخ المتاحة.')
                if unavailable_now_or_future:
                    raise RuntimeError(
                        f'القسم {section} لا يعرض التواريخ المطلوبة {unavailable_now_or_future}. '
                        f'المتاح: {available}. لم أكتب الملفات.'
                    )

                dates_to_read = [d for d in wanted if d in available]
                if not dates_to_read:
                    raise RuntimeError(f'لا توجد تواريخ صالحة في القسم {section}.')

                for date in dates_to_read:
                    choose_day(page, date)
                    rows, empty = collect_rows(page, channels, section, key)
                    print(f'  {section} {date}: {rows} صفوف، {empty} بلا برامج، {len(channels)} معرّفاً')
        finally:
            browser.close()

    kept = filter_window(channels, start_ms, end_ms)
    print(f'البرامج داخل النافذة الزمنية: {kept}')
    return channels


def xmltv_time(milliseconds):
    return datetime.fromtimestamp(int(milliseconds) / 1000, timezone.utc).strftime('%Y%m%d%H%M%S +0000')


def write_outputs(channels):
    root = ET.Element('tv', {'generator-info-name': 'bein-epg-light'})
    csv_rows = []

    for tvg_id, channel in sorted(channels.items(), key=lambda item: (item[1]['section'].lower(), int(item[1]['index']))):
        element = ET.SubElement(root, 'channel', {'id': tvg_id})
        # XMLTV requires a display-name. Keep CSV's name blank when unknown,
        # but use the stable ID as a technical fallback in the XML document.
        ET.SubElement(element, 'display-name').text = channel['name'] or tvg_id
        if channel['logo']:
            ET.SubElement(element, 'icon', {'src': channel['logo']})
        csv_rows.append({
            'tvg_id': tvg_id,
            'name': channel['name'],
            'slug': channel['slug'],
            'logo': channel['logo'],
            'category': channel['section'],
            'index': channel['index'],
        })

    total = 0
    for tvg_id, channel in channels.items():
        for program in sorted(channel['programs'].values(), key=lambda item: int(item['start'])):
            if not valid_program(program):
                continue
            item = ET.SubElement(root, 'programme', {
                'start': xmltv_time(program['start']),
                'stop': xmltv_time(program['end']),
                'channel': tvg_id,
            })
            ET.SubElement(item, 'title', {'lang': 'en'}).text = program['title']
            if program['category']:
                ET.SubElement(item, 'category', {'lang': 'en'}).text = program['category']
            total += 1

    temp_xml = EPG_FILE.with_suffix('.xml.tmp')
    temp_csv = CSV_FILE.with_suffix('.csv.tmp')
    tree = ET.ElementTree(root)
    ET.indent(tree, space='  ')
    tree.write(temp_xml, encoding='utf-8', xml_declaration=True)

    with temp_csv.open('w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=['tvg_id', 'name', 'slug', 'logo', 'category', 'index'])
        writer.writeheader()
        writer.writerows(csv_rows)

    temp_xml.replace(EPG_FILE)
    temp_csv.replace(CSV_FILE)
    return total


def main():
    try:
        channels = scrape()
        total = sum(len(c['programs']) for c in channels.values())
        if not channels or total == 0:
            raise RuntimeError('لم يتم استخراج أي برنامج داخل النافذة؛ احتفظت بالملفات السابقة.')
        written = write_outputs(channels)
        print(f'\nنجح التحديث: {len(channels)} قنوات، {written} برنامجاً.')
        print(f'XMLTV: {EPG_FILE.resolve()}')
        print(f'CSV:   {CSV_FILE.resolve()}')
    except Exception as exc:
        print(f'فشل التحديث: {exc}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
