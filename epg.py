"""Scrape the official beIN TV guide and build epg.xml (XMLTV) + channels.csv."""
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright

URL = "https://www.bein.com/en/tv-guide/?c=dz"
CONFIG_FILE = Path("channels_config.csv")  # optional: slug,tvg_id,name
EPG_FILE = Path("epg.xml")
CHANNELS_FILE = Path("channels.csv")
DEFAULT_ID_SUFFIX = ".dz"  # used when a channel has no tvg_id in the config

# Runs inside the page: returns every channel row with its programme blocks.
EXTRACT_JS = """
() => [...document.querySelectorAll('.channel-row')].map(row => {
  const a = row.querySelector('.channel-col a');
  const img = row.querySelector('.channel-col img');
  return {
    href: a ? a.href : '',
    logo: img ? img.src : '',
    programs: [...row.querySelectorAll('.prog-block')].map(b => ({
      title: b.dataset.fullTitle || '',
      category: b.dataset.fullCategory || '',
      start: b.dataset.startMs,
      end: b.dataset.endMs,
    })),
  };
})
"""


def load_config():
    """Read optional overrides: slug -> {tvg_id, name}."""
    config = {}
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                slug = (row.get("slug") or "").strip()
                if slug:
                    config[slug] = {
                        "tvg_id": (row.get("tvg_id") or "").strip(),
                        "name": (row.get("name") or "").strip(),
                    }
    return config


def scrape():
    """Open the guide, click each day, and collect all programmes per channel."""
    channels = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle")
        page.wait_for_selector(".channel-row .prog-block", timeout=60000)

        tabs = page.locator(".category-tab")  # Sports / Entertainment
        for t in range(tabs.count()):
            tabs.nth(t).click()
            page.wait_for_timeout(2500)
            days = page.locator(".day-cell")
            for i in range(days.count()):
                days.nth(i).click()
                page.wait_for_timeout(2500)
                try:
                    page.wait_for_selector(".channel-row .prog-block", timeout=30000)
                except Exception:
                    continue  # nothing listed for this tab/day
                for row in page.evaluate(EXTRACT_JS):
                    slug = row["href"].rstrip("/").split("/")[-1]
                    if not slug:
                        continue
                    ch = channels.setdefault(slug, {"logo": row["logo"], "programs": {}})
                    for prog in row["programs"]:
                        # de-duplicate days that overlap: key = start timestamp
                        ch["programs"][prog["start"]] = prog
        browser.close()
    return channels


def xmltv_time(ms):
    dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    return dt.strftime("%Y%m%d%H%M%S +0000")


def build_outputs(channels, config):
    root = ET.Element("tv", {"generator-info-name": "bein-epg-scraper"})
    rows = []

    # XMLTV requires all <channel> elements before any <programme>.
    resolved = {}
    for slug, ch in channels.items():
        cfg = config.get(slug, {})
        tvg_id = cfg.get("tvg_id") or f"{slug}{DEFAULT_ID_SUFFIX}"
        name = cfg.get("name") or slug
        resolved[slug] = tvg_id
        c = ET.SubElement(root, "channel", id=tvg_id)
        ET.SubElement(c, "display-name").text = name
        if ch["logo"]:
            ET.SubElement(c, "icon", src=ch["logo"])
        rows.append({"tvg_id": tvg_id, "name": name, "slug": slug, "logo": ch["logo"]})

    for slug, ch in channels.items():
        for prog in sorted(ch["programs"].values(), key=lambda x: int(x["start"])):
            p = ET.SubElement(
                root,
                "programme",
                start=xmltv_time(prog["start"]),
                stop=xmltv_time(prog["end"]),
                channel=resolved[slug],
            )
            ET.SubElement(p, "title", lang="en").text = prog["title"]
            if prog["category"]:
                ET.SubElement(p, "category", lang="en").text = prog["category"]

    tree = ET.ElementTree(root)
    ET.indent(tree)
    tree.write(EPG_FILE, encoding="utf-8", xml_declaration=True)

    with CHANNELS_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["tvg_id", "name", "slug", "logo"])
        writer.writeheader()
        writer.writerows(rows)


def main():
    channels = scrape()
    total = sum(len(c["programs"]) for c in channels.values())
    if not channels or total == 0:
        # never overwrite good files with an empty result
        print("No programmes found, keeping previous files.", file=sys.stderr)
        sys.exit(1)
    build_outputs(channels, load_config())
    print(f"Done: {len(channels)} channels, {total} programmes.")


if __name__ == "__main__":
    main()
