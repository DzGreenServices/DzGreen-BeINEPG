"""Lightweight beIN EPG scraper: separate sports and entertainment XMLTV."""

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

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

PAGE_TIMEOUT = 60000
DAY_WAIT = 12000
ROW_WAIT = 1200
SETTLE_MS = 700


DATES_JS = r"""
() => [...new Set(
  [...document.querySelectorAll('.day-cell')]
    .map(e => e.dataset.date || '')
    .filter(d => /^\d{4}-\d{2}-\d{2}$/.test(d))
)]
"""


SIGNATURE_JS = r"""
() => [...document.querySelectorAll('.channel-row')].map(r =>
  [...r.querySelectorAll('.prog-block')].map(p => [
    p.dataset.startMs || '',
    p.dataset.endMs || '',
    p.dataset.fullTitle || ''
  ].join(':')).join('~')
).join('|')
"""


WAIT_DAY_JS = r"""
({date, before}) => {
  const d = [...document.querySelectorAll('.day-cell')]
    .find(e => e.dataset.date === date);

  if (!d || !d.classList.contains('active')) return false;

  const now = [...document.querySelectorAll('.channel-row')].map(r =>
    [...r.querySelectorAll('.prog-block')].map(p => [
      p.dataset.startMs || '',
      p.dataset.endMs || '',
      p.dataset.fullTitle || ''
    ].join(':')).join('~')
  ).join('|');

  return now !== before;
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
  const href = a ? a.href : '';
  const path = href ? new URL(href).pathname.replace(/\/+$/, '') : '';

  return {
    index: row.getAttribute('data-channel-index') || '',
    href: href,
    slug: path ? path.split('/').pop() : '',
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


INDEXES_JS = r"""
() => [...new Set(
  [...document.querySelectorAll('.channel-row')]
    .map(r => r.getAttribute('data-channel-index'))
    .filter(v => v !== null && /^\d+$/.test(v))
)].map(Number).sort((a, b) => a - b)
"""


SCROLL_JS = r"""
i => {
  const r = document.querySelector(
    '.channel-row[data-channel-index="' + String(i) + '"]'
  );

  if (!r) return false;

  r.scrollIntoView({block: 'center'});
  return true;
}
"""


ROW_READY_JS = r"""
i => {
  const r = document.querySelector(
    '.channel-row[data-channel-index="' + String(i) + '"]'
  );

  return !!r && r.querySelectorAll('.prog-block').length > 0;
}
"""


READ_ROW_JS = (
    "i => {"
    "const r = document.querySelector("
    "'.channel-row[data-channel-index=\"' + String(i) + '\"]');"
    "return r ? (" + ROW_JS.strip() + ")(r) : null;"
    "}"
)


def section_key(label, position):
    text = re.sub(r"[^a-z0-9]+", "", (label or "").lower())

    if "sport" in text:
        return "sports"

    if "entertain" in text:
        return "entertainment"

    if position == 0:
        return "sports"

    if position == 1:
        return "entertainment"

    return None


def tvg_id_for(key, index):
    # Keep this ID aligned with the M3U entry for BeIN Movies 1.
    if key == "entertainment" and index == "0":
        return "beINMOVIES1"

    return f"{key}_{index}"


def valid_program(program):
    try:
        title = (program.get("title") or "").strip()
        start = int(program["start"])
        end = int(program["end"])

        return bool(title) and start > 0 and end > start

    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def add_row(row, key, section, channels, start_limit, end_limit):
    raw_index = str(row.get("index", "")).strip()

    if not raw_index.isdigit():
        return 0

    index = str(int(raw_index))
    tvg_id = tvg_id_for(key, index)

    href = (row.get("href") or "").strip()
    path = urlparse(href).path.rstrip("/")
    slug = (row.get("slug") or "").strip()

    if not slug and path:
        slug = path.rsplit("/", 1)[-1]

    name = (row.get("name") or "").strip() or slug
    logo = (row.get("logo") or "").strip()

    channel = channels.setdefault(
        tvg_id,
        {
            "name": name,
            "slug": slug,
            "logo": logo,
            "section": section,
            "index": index,
            "programs": {},
        },
    )

    for field, value in (
        ("name", name),
        ("slug", slug),
        ("logo", logo),
    ):
        if not channel[field] and value:
            channel[field] = value

    added = 0

    for program in row.get("programs", []):
        if not valid_program(program):
            continue

        start = int(program["start"])
        end = int(program["end"])

        # Keep any full programme that overlaps the requested time window.
        if end <= start_limit or start >= end_limit:
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


def wait_guide(page):
    try:
        page.wait_for_selector(
            ".channel-row",
            state="attached",
            timeout=10000,
        )
    except PlaywrightTimeoutError:
        pass

    try:
        page.wait_for_selector(
            ".channel-row .prog-block",
            state="attached",
            timeout=7000,
        )
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(SETTLE_MS)


def select_section(page, position):
    tab = page.locator(".category-tab").nth(position)
    name = tab.inner_text().strip()

    if "active" not in (tab.get_attribute("class") or "").split():
        tab.click()

        try:
            page.wait_for_function(
                "i => document.querySelectorAll('.category-tab')[i]"
                "?.classList.contains('active')",
                arg=position,
                timeout=8000,
            )
        except PlaywrightTimeoutError:
            print(f"Warning: category change not confirmed: {name}")
            page.wait_for_timeout(1500)

    wait_guide(page)
    return name


def select_day(page, date):
    day = page.locator(f'.day-cell[data-date="{date}"]').first

    if day.count() == 0:
        print(f"Warning: date {date} is no longer available; skipping.")
        return False

    if "active" in (day.get_attribute("class") or "").split():
        wait_guide(page)
        return True

    before = page.evaluate(SIGNATURE_JS)
    day.click()

    try:
        page.wait_for_function(
            WAIT_DAY_JS,
            arg={"date": date, "before": before},
            timeout=DAY_WAIT,
        )
    except PlaywrightTimeoutError:
        # Do not abort the full extraction because the signature timed out.
        print(
            f"Warning: programme change not confirmed for {date}; "
            "waiting briefly and continuing."
        )
        page.wait_for_timeout(1800)

    wait_guide(page)
    return True


def collect_rows(page, key, section, channels, start_limit, end_limit):
    indexes = page.evaluate(INDEXES_JS)
    empty = 0

    for index in indexes:
        try:
            if not page.evaluate(SCROLL_JS, index):
                empty += 1
                continue

            try:
                page.wait_for_function(
                    ROW_READY_JS,
                    arg=index,
                    timeout=ROW_WAIT,
                )
            except PlaywrightTimeoutError:
                pass

            row = page.evaluate(READ_ROW_JS, index)

            if not row:
                empty += 1
                continue

            added = add_row(
                row, key, section, channels, start_limit, end_limit
            )

            # One short retry for rows whose programmes lazy-load on scroll.
            if added == 0:
                page.wait_for_timeout(350)
                row = page.evaluate(READ_ROW_JS, index)

                if row:
                    added = add_row(
                        row, key, section, channels,
                        start_limit, end_limit
                    )

            if added == 0:
                empty += 1

        except PlaywrightError as exc:
            print(
                f"  index {index}: skipped "
                f"({str(exc).splitlines()[0]})"
            )
            empty += 1

    return len(indexes), empty


def xmltv_time(milliseconds):
    return datetime.fromtimestamp(
        int(milliseconds) / 1000,
        timezone.utc,
    ).strftime("%Y%m%d%H%M%S +0000")


def make_tree(channels, filename):
    total = sum(len(c["programs"]) for c in channels.values())

    if not channels or total == 0:
        raise RuntimeError(
            f"{filename}: no valid programmes; previous file was preserved."
        )

    root = ET.Element(
        "tv",
        {"generator-info-name": "bein-epg-split"},
    )

    ordered = sorted(
        channels.items(),
        key=lambda item: int(item[1]["index"]),
    )

    for tvg_id, channel in ordered:
        element = ET.SubElement(root, "channel", {"id": tvg_id})

        # Use the ID as a valid display name if the real name is unknown.
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
                {"lang": "en"},
            ).text = program["title"]

            if program["category"]:
                ET.SubElement(
                    element,
                    "category",
                    {"lang": "en"},
                ).text = program["category"]

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    return tree, len(channels), total


def write_outputs(sports, entertainment):
    # Build and validate both temporary files before replacing existing ones.
    prepared = []

    try:
        for path, channels in (
            (SPORTS_XML, sports),
            (ENTERTAINMENT_XML, entertainment),
        ):
            tree, channel_count, program_count = make_tree(
                channels, path.name
            )

            temp = path.with_suffix(path.suffix + ".tmp")

            tree.write(
                temp,
                encoding="utf-8",
                xml_declaration=True,
            )

            root = ET.parse(temp).getroot()
            declared = {
                channel.get("id")
                for channel in root.findall("channel")
            }

            missing = sorted({
                program.get("channel")
                for program in root.findall("programme")
                if program.get("channel") not in declared
            })

            if missing:
                raise RuntimeError(
                    f"{path.name}: undefined channel IDs: {missing}"
                )

            prepared.append(
                (temp, path, channel_count, program_count)
            )

        for temp, path, _channels, _programs in prepared:
            temp.replace(path)

    finally:
        for temp, _path, _channels, _programs in prepared:
            if temp.exists():
                temp.unlink()

    for _temp, path, channel_count, program_count in prepared:
        print(
            f"{path}: {channel_count} channels, "
            f"{program_count} programmes"
        )


def scrape():
    now = datetime.now(ALGERIA_TZ)

    window_start = now - timedelta(hours=PAST_HOURS)
    window_end = now + timedelta(hours=FUTURE_HOURS)

    start_ms = int(window_start.timestamp() * 1000)
    end_ms = int(window_end.timestamp() * 1000)

    print(f"Algeria time: {now:%Y-%m-%d %H:%M:%S}")
    print(
        f"Programme window: {window_start:%Y-%m-%d %H:%M} to "
        f"{window_end:%Y-%m-%d %H:%M}"
    )

    data = {
        "sports": {},
        "entertainment": {},
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        try:
            page = browser.new_page()
            page.set_default_timeout(PAGE_TIMEOUT)

            page.goto(URL, wait_until="domcontentloaded")
            page.wait_for_selector(
                ".category-tab",
                timeout=PAGE_TIMEOUT,
            )

            tab_count = page.locator(".category-tab").count()

            if tab_count == 0:
                raise RuntimeError("No category tabs found on the guide.")

            found = set()

            for position in range(tab_count):
                section = select_section(page, position)
                key = section_key(section, position)

                if key not in data or key in found:
                    print(f"Skipping unknown or repeated section: {section}")
                    continue

                found.add(key)

                # The site's calendar may be ahead of Algeria's local date.
                # Read and use the dates actually offered by the guide.
                dates = page.evaluate(DATES_JS)

                if not dates:
                    raise RuntimeError(
                        f"No date cells found in section {section}."
                    )

                print(f"{section} dates available: {', '.join(dates)}")

                for date in dates:
                    if not select_day(page, date):
                        continue

                    row_count, empty_count = collect_rows(
                        page,
                        key,
                        section,
                        data[key],
                        start_ms,
                        end_ms,
                    )

                    accumulated = sum(
                        len(channel["programs"])
                        for channel in data[key].values()
                    )

                    print(
                        f"{section} | {date}: {row_count} rows, "
                        f"{empty_count} without matching programmes; "
                        f"{accumulated} unique programmes accumulated"
                    )

            if found != {"sports", "entertainment"}:
                raise RuntimeError(
                    f"Could not identify both sections: {sorted(found)}"
                )

        finally:
            browser.close()

    return data


def main():
    try:
        results = scrape()

        write_outputs(
            results["sports"],
            results["entertainment"],
        )

        print("Extraction completed successfully.")
        print(f"Sports XML: {SPORTS_XML.resolve()}")
        print(f"Entertainment XML: {ENTERTAINMENT_XML.resolve()}")

    except Exception as exc:
        print(f"Extraction failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
