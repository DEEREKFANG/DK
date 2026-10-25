#!/usr/bin/env python3
"""Export the public *property-only* Google Sheet as same-origin JSON for GitHub Pages.

No login credentials are sent to the public website. The Sheet is read-only
for anonymous visitors. Fail closed if Google returns an HTML/login page or
if a truncated/misformatted spreadsheet would delete users' housing records.
"""
import csv
import hashlib
import io
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parents[1]
OUT = BASE / "sheet-data.json"
SHEET_ID = "1XP_8GXN9QRKbEGqYzBudjOYazADMJqYFf8gqMiADgS8"
GID = "762574749"
URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}"
REQUIRED = ("編號", "房屋／社區", "區域", "開價（萬元）", "原始網址")
COLS = {
    "編號":"seq", "房屋／社區":"name", "區域":"district",
    "開價（萬元）":"price", "權狀（坪）":"totalArea", "主建物（坪）":"mainArea",
    "管理費（元／月）":"management", "房型":"rooms", "挑高類型":"heightType",
    "高度(m)":"height", "本戶法定用途":"purpose", "原始網址":"url",
    "捷運":"metro", "floor":"floor", "刊登狀態":"listingStatus",
    "評估結論":"assessment", "優點":"pros", "缺點與風險":"cons",
    "重複／關聯":"related", "屋齡（年）":"ageYears", "屋齡來源／查核":"ageNote",
    "屋況照片網址（本戶已核實）":"photoUrl"
}
NUMERIC = {"seq", "price", "totalArea", "mainArea", "management", "height", "floor", "ageYears"}

def main():
    response = requests.get(URL, timeout=35, headers={
        "User-Agent": "Mozilla/5.0 (compatible; PersonalHomeSheetSync/1.0)",
        "Accept": "text/csv,text/plain,*/*",
    }, allow_redirects=True)
    response.raise_for_status()
    raw = response.content.decode("utf-8-sig", errors="replace")
    if not raw.strip() or "<html" in raw[:1200].lower():
        raise RuntimeError("Google returned HTML/login page instead of the public Sheet CSV")
    reader = csv.DictReader(io.StringIO(raw))
    columns = set(reader.fieldnames or [])
    if not all(c in columns for c in REQUIRED):
        raise RuntimeError("Unexpected Google Sheet CSV header: "+str(reader.fieldnames))
    items, seen = [], set()
    for row in reader:
        try:
            seq = int(float((row.get("編號") or "").strip()))
        except ValueError:
            continue
        if seq < 1 or seq in seen:
            raise RuntimeError("Duplicate or invalid row number: "+str(seq))
        name = (row.get("房屋／社區") or "").strip()
        if not name:
            continue
        seen.add(seq)
        p={}
        for source, field in COLS.items():
            val = (row.get(source) or "").strip()
            if field in NUMERIC:
                if val:
                    try:
                        n = float(val.replace(",", ""))
                        p[field] = int(n) if n.is_integer() else n
                    except ValueError:
                        p[field] = None
                else:
                    p[field] = None
            else:
                p[field] = val[:3000]
        if p.get("ageYears") is not None and not (0 <= p["ageYears"] <= 120):
            p["ageYears"] = None
        if p.get("url") and not re.match(r"^https://", p["url"], re.I):
            p["url"] = ""
        # Photo links must be verified by the owner as the exact advertised unit.
        # Never infer an interior photo from the community or neighbouring unit.
        if p.get("photoUrl") and not re.match(r"^https://", p["photoUrl"], re.I):
            p["photoUrl"] = ""
        items.append(p)
    # Preserve individually researched items entered directly in the website
    # when Google Sheets connector writing was unavailable. Google Sheet wins
    # on a later matching sequence ID or source listing URL.
    if OUT.exists():
        try:
            old_public = json.loads(OUT.read_text("utf-8"))
            ids = {p["seq"] for p in items}
            urls = {p.get("url") for p in items if p.get("url")}
            for p in old_public.get("items", []):
                if (p.get("manualEntry") is True and
                    isinstance(p.get("seq"), int) and
                    p["seq"] not in ids and
                    str(p.get("url", "")).startswith("https://") and
                    p["url"] not in urls):
                    items.append(p)
        except (ValueError, TypeError, KeyError):
            pass
    items.sort(key=lambda x:x["seq"])
    if len(items) < 60:
        raise RuntimeError("Truncated Sheet? Refusing to publish only "+str(len(items))+" rows")
    if len(items) > 2000:
        raise RuntimeError("Unexpectedly large Sheet")
    # A publication is material only if rows changed. Avoid redeploying
    # every 15 minutes solely to update the timestamp.
    payload={"schemaVersion":1,"source":"google_sheet_public_csv","sheetId":SHEET_ID,
             "sheetGid":GID,"count":len(items),"items":items}
    content=json.dumps(payload,ensure_ascii=False,indent=2)+"\n"
    previous=OUT.read_text("utf-8") if OUT.exists() else ""
    if previous == content:
        print("SHEET_SYNC_OK: {} properties, data unchanged".format(len(items)))
    else:
        OUT.write_text(content,encoding="utf-8")
        print("SHEET_SYNC_OK: {} properties, generated {}".format(len(items),OUT))
    print("SHEET_SOURCE_URL="+URL)

PENDING_GID = "762574754"
PENDING_OUT = BASE / "pending-listings.json"
PENDING_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={PENDING_GID}"
PENDING_FIELDS = {
    "收到日期": "received",
    "房源平台": "platform",
    "原始連結": "url",
    "社區／房名": "name",
    "開價（萬）": "price",
    "核查狀態": "status",
    "說明／待確認": "note",
}

def sync_pending():
    """Publish ONLY public property links from the dedicated unresolved-listings tab."""
    try:
        res = requests.get(PENDING_URL, timeout=35, headers={
            "User-Agent": "Mozilla/5.0 (compatible; PersonalHomeSheetSync/1.0)",
            "Accept": "text/csv,text/plain,*/*",
        })
        res.raise_for_status()
        data = res.content.decode("utf-8-sig", errors="replace")
        if not data.strip() or "<html" in data[:1200].lower():
            raise RuntimeError("Expected CSV, got blank or HTML")
        reader = csv.DictReader(io.StringIO(data))
        if not all(k in set(reader.fieldnames or []) for k in PENDING_FIELDS):
            raise RuntimeError("Missing pending-listing CSV columns")
        items = []
        seen = set()
        for r in reader:
            url = (r.get("原始連結") or "").strip()
            if not re.match(r"^https://[^/\s]+", url, flags=re.I):
                continue
            if url in seen: continue
            seen.add(url)
            item = {dest: (r.get(src) or "").strip()[:1800]
                    for src, dest in PENDING_FIELDS.items()}
            items.append(item)
        if len(items) > 500:
            raise RuntimeError("Unexpected pending-listing record count")
        output = {
            "schemaVersion": 1, "source": "google_sheet_pending_listing_links",
            "sheetId": SHEET_ID, "sheetGid": PENDING_GID,
            "count": len(items), "items": items,
        }
        content = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
        if not PENDING_OUT.exists() or PENDING_OUT.read_text("utf-8") != content:
            PENDING_OUT.write_text(content, encoding="utf-8")
            print(f"PENDING_SYNC_OK: {len(items)} links updated")
        else:
            print(f"PENDING_SYNC_OK: {len(items)} links unchanged")
    except (requests.RequestException, ValueError, RuntimeError) as e:
        # Keep previously published pending records if Google is temporarily unavailable.
        print(f"PENDING_SYNC_WARNING: previous data preserved: {e}", file=sys.stderr)

if __name__=="__main__":
    main()
    sync_pending()
