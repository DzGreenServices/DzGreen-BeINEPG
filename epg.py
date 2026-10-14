"""Lightweight beIN TV Guide scraper: section/index -> XMLTV + channels.csv."""

import csv
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

URL = "https://www.bein.com/en/tv-guide/?c=dz"
EPG_FILE = Path("epg.xml")
CHANNELS_FILE = Path("channels.csv")
PAGE_TIMEOUT_MS = 45_000
VIEW_TIMEOUT_MS = 8_000
ROW_TIMEOUT_MS = 700
SETTLE_MS = 500

# Read one channel row. href/name are optional; programmes and logo are not.
ROW_INFO_JS = r"""row => {
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
}"""

ROW_INDEXES_JS = r"""() => [...new Set(
  [...document.querySelectorAll('.channel-row')]
    .map(r => r.getAttribute('data-channel-index'))
    .filter(v => v !== null && /^\d+$/.test(v))
)].map(Number).sort((a,b) => a-b)"""

SCROLL_ROW_JS = r"""i => {
  const row = document.querySelector('.channel-row[data-channel-index="' + i + '"]');
  if (!row) return false;
  row.scrollIntoView({block: 'center'});
  return true;
}"""

ROW_READY_JS = r"""i => {
  const row = document.querySelector('.channel-row[data-channel-index="' + i + '"]');
  return !!row && row.querySelectorAll('.prog-block').length > 0;
}"""

READ_ROW_JS = "i => { const row = document.querySelector('.channel-row[data-channel-index=\\\"' + i + '\\\"]'); return row ? (" + ROW_INFO_JS + ")(row) : null; }"

# Include EVERY block so a date transition cannot look unchanged merely because
# the first and last programme happen to have the same titles.
SCHEDULE_SIGNATURE_JS = r"""() => [...document.querySelectorAll('.channel-row')].map(row => {
  const i = row.getAttribute('data-channel-index') || '';
  const blocks = [...row.querySelectorAll('.prog-block')].map(b =>
    [b.dataset.startMs || '', b.dataset.endMs || '', b.dataset.fullTitle || ''].join('~')
  ).join('^');
  return i + ':' + blocks;
}).join('|')"""

CLICK_DAY_JS = r"""date => {
  const cell = document.querySelector('.day-cell[data-date="' + date + '"]');
  if (!cell) return false;
  cell.click();
  return true;
}"""

CLICK_TAB_JS = r"""i => {
  const tabs = [...document.querySelectorAll('.category-tab')];
  if (!tabs[i]) return false;
  tabs[i].click();
  return true;
}"""

ACTIVE_DAY_JS = r"""date => {
  const cell = document.querySelector('.day-cell.active');
  return !!cell && cell.getAttribute('data-date') === date;
}"""

ACTIVE_TAB_JS = r"""i => {
  const tabs = [...document.querySelectorAll('.category-tab')];
  if (!tabs[i]) return false;
  const el = tabs[i];
  const cls = typeof el.className === 'string' ? el.className : '';
  return el.getAttribute('aria-selected') === 'true' ||
    /(^|\s)(active|selected|current|is-active|is-selected)(\s|$)/i.test(cls);
}"""

DAY_LIST_JS = r"""() => [...document.querySelectorAll('.day-cell[data-date]')].map(el => ({
  date: el.getAttribute('data-date') || '',
  label: (el.innerText || '').trim().replace(/\s+/g, ' '),
  active: el.classList.contains('active')
})).filter(x => x.date)"""


def section_key(label: str, position: int) -> str:
    compact = re.sub(r"[^a-z0-9]", "", (label or "").lower())
    if "sport" in compact:
        return "sports"
    if "entertain" in compact:
        return "entertainment"
    # Keep deterministic IDs even if a tab label is unexpected/blank.
    clean = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")
    return clean or ("sports" if position == 0 else "entertainment" if position == 1 else f"section{position + 1}")


def channel_name(href: str, visible_name: str) -> str:
    name = (visible_name or "").strip()
    if name:
        return name
    path = urlparse((href or "").strip()).path.rstrip("/")
    # A link slug is useful as a name when the site exposes no display label.
    return path.rsplit("/", 1)[-1] if path else ""


def valid_program(program: dict) -> bool:
    try:
        return bool((program.get("title") or "").strip()) and int(program.get("start", "")) > 0 and int(program.get("end", "")) > int(program.get("start", ""))
    except (TypeError, ValueError):
        return False


def schedule_signature(page) -> str:
    return page.evaluate(SCHEDULE_SIGNATURE_JS)


def wait_for_view_change(page, previous: str | None, description: str) -> bool:
    """Wait until the new schedule differs and stays stable, not just until a block exists."""
    deadline = time.monotonic() + VIEW_TIMEOUT_MS / 1000
    last_signature = None
    stable_since = None

    while time.monotonic() < deadline:
        current = schedule_signature(page)
        has_programmes = page.locator(".channel-row .prog-block").count() > 0
        changed = previous is None or current != previous
        now = time.monotonic()

        if has_programmes and changed:
            if current == last_signature:
                if stable_since is not None and (now - stable_since) * 1000 >= SETTLE_MS:
                    return True
            else:
                last_signature = current
                stable_since = now
        else:
            last_signature = None
            stable_since = None

        page.wait_for_timeout(150)

    print(f"  WARNING: programme data did not change/stabilize after selecting {description}; skipped to avoid duplicates.")
    return False


def add_row(row: dict, section_name: str, section_id: str, channels: dict) -> int:
    raw_index = str(row.get("index", "")).strip()
    if not raw_index.isdigit():
        return 0

    index = str(int(raw_index))
    tvg_id = f"{section_id}_{index}"
    name = channel_name(row.get("href", ""), row.get("name", ""))
    logo = (row.get("logo") or "").strip()

    channel = channels.setdefault(tvg_id, {
        "tvg_id": tvg_id,
        "name": name,
        "logo": logo,
        "section": section_name,
        "index": index,
        "programs": {},
    })
    if not channel["name"] and name:
        channel["name"] = name
    if not channel["logo"] and logo:
        channel["logo"] = logo

    added = 0
    for p in row.get("programs", []):
        if not valid_program(p):
            continue
        start = str(int(p["start"]))
        channel["programs"][start] = {
            "title": p["title"].strip(),
            "category": (p.get("category") or "").strip(),
            "start": start,
            "end": str(int(p["end"])),
        }
        added += 1
    return added


def collect_current_day(page, channels: dict, section_name: str, section_id: str) -> tuple[int, int]:
    indexes = page.evaluate(ROW_INDEXES_JS)
    empty = 0
    for index in indexes:
        try:
            if not page.evaluate(SCROLL_ROW_JS, index):
                empty += 1
                continue
            try:
                page.wait_for_function(ROW_READY_JS, arg=index, timeout=ROW_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                # Some rows may legitimately have no programmes; still read logo/index.
                pass
            row = page.evaluate(READ_ROW_JS, index)
            if not row:
                empty += 1
                continue
            if add_row(row, section_name, section_id, channels) == 0:
                empty += 1
        except PlaywrightError as exc:
            print(f"  index {index}: skipped ({str(exc).splitlines()[0]})")
            empty += 1
    return len(indexes), empty


def click_and_confirm_day(page, date: str) -> bool:
    active = page.evaluate(ACTIVE_DAY_JS, date)
    if active:
        return True
    before = schedule_signature(page)
    if not page.evaluate(CLICK_DAY_JS, date):
        print(f"  WARNING: day button {date} not found.")
        return False
    try:
        page.wait_for_function(ACTIVE_DAY_JS, arg=date, timeout=4_000)
    except PlaywrightTimeoutError:
        print(f"  WARNING: site did not activate day {date}.")
        return False
    return wait_for_view_change(page, before, f"day {date}")


def scrape() -> dict:
    channels = {}
    failures = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            page.goto(URL, wait_until="domcontentloaded")
            page.wait_for_selector(".category-tab", timeout=PAGE_TIMEOUT_MS)
            page.wait_for_selector(".day-cell[data-date]", timeout=PAGE_TIMEOUT_MS)
            page.wait_for_selector(".channel-row", timeout=PAGE_TIMEOUT_MS)
            if not wait_for_view_change(page, None, "initial page load"):
                failures.append("initial schedule did not stabilize")

            category_count = page.locator(".category-tab").count()
            if not category_count:
                raise RuntimeError("لم يتم العثور على أقسام القنوات.")

            for ci in range(category_count):
                tabs = page.locator(".category-tab")
                tab = tabs.nth(ci)
                label = tab.inner_text().strip()
                key = section_key(label, ci)

                # The first tab is normally already open. Select every later tab
                # and verify that its programme data actually changed.
                if ci > 0 and not page.evaluate(ACTIVE_TAB_JS, ci):
                    before = schedule_signature(page)
                    if not page.evaluate(CLICK_TAB_JS, ci):
                        print(f"WARNING: could not select section {label or key}; skipped.")
                        failures.append(f"section {label or key} could not be selected")
                        continue
                    try:
                        page.wait_for_function(ACTIVE_TAB_JS, arg=ci, timeout=4_000)
                    except PlaywrightTimeoutError:
                        pass  # Some versions of the page do not mark the active tab.
                    if not wait_for_view_change(page, before, f"section {label or key}"):
                        failures.append(f"section {label or key} did not load")
                        continue

                days = page.evaluate(DAY_LIST_JS)
                dates = [d for d in days if d["date"]]
                if not dates:
                    print(f"WARNING: no dated day cells in section {label or key}.")
                    failures.append(f"section {label or key} had no dated days")
                    continue
                if len(dates) != 4:
                    failures.append(f"section {label or key} showed {len(dates)} days instead of the expected 4")

                print(f"\n{label or key}: {len(dates)} displayed days: " + ", ".join(f"{d['date']} ({d['label']})" for d in dates))
                for d in dates:
                    date = d["date"]
                    if not click_and_confirm_day(page, date):
                        failures.append(f"section {label or key}, date {date} did not load")
                        continue
                    rows, empty = collect_current_day(page, channels, label or key, key)
                    print(f"  {date}: {rows} channel indexes; {empty} rows without programme data; {len(channels)} channel IDs total")
        finally:
            browser.close()

    if failures:
        details = "; ".join(failures)
        raise RuntimeError(
            "الاستخراج غير مكتمل، لذلك لم يتم استبدال ملفات XMLTV/CSV السابقة. " + details
        )
    return channels


def xmltv_time(ms: str) -> str:
    dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    return dt.strftime("%Y%m%d%H%M%S +0000")


def write_outputs(channels: dict) -> int:
    root = ET.Element("tv", {"generator-info-name": "bein-epg-index-scraper"})
    csv_rows = []
    for tvg_id, ch in sorted(channels.items(), key=lambda item: (item[1]["section"].lower(), int(item[1]["index"]))):
        element = ET.SubElement(root, "channel", id=tvg_id)
        ET.SubElement(element, "display-name").text = ch.get("name", "")
        if ch.get("logo"):
            ET.SubElement(element, "icon", src=ch["logo"])
        csv_rows.append({"tvg_id": tvg_id, "name": ch.get("name", ""), "logo": ch.get("logo", ""), "section": ch.get("section", ""), "index": ch.get("index", "")})

    programme_count = 0
    for tvg_id, ch in channels.items():
        for pr in sorted(ch["programs"].values(), key=lambda item: int(item["start"])):
            if not valid_program(pr):
                continue
            node = ET.SubElement(root, "programme", {
                "start": xmltv_time(pr["start"]),
                "stop": xmltv_time(pr["end"]),
                "channel": tvg_id,
            })
            ET.SubElement(node, "title", lang="en").text = pr["title"]
            if pr.get("category"):
                ET.SubElement(node, "category", lang="en").text = pr["category"]
            programme_count += 1

    temp_epg = EPG_FILE.with_suffix(".xml.tmp")
    temp_csv = CHANNELS_FILE.with_suffix(".csv.tmp")
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(temp_epg, encoding="utf-8", xml_declaration=True)
    with temp_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["tvg_id", "name", "logo", "section", "index"])
        writer.writeheader()
        writer.writerows(csv_rows)
    temp_epg.replace(EPG_FILE)
    temp_csv.replace(CHANNELS_FILE)
    return programme_count


def main() -> None:
    try:
        channels = scrape()
        total = sum(len(ch["programs"]) for ch in channels.values())
        if not channels or not total:
            print("لم يتم استخراج أي برنامج صالح؛ تم الاحتفاظ بالملفات السابقة.", file=sys.stderr)
            raise SystemExit(1)
        written = write_outputs(channels)
        print("\nاكتمل الاستخراج.")
        print(f"القنوات ذات المعرّفات: {len(channels)}")
        print(f"البرامج: {written}")
        print(f"XMLTV: {EPG_FILE.resolve()}")
        print(f"CSV:   {CHANNELS_FILE.resolve()}")
    except Exception as exc:
        print(f"فشل الاستخراج: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
