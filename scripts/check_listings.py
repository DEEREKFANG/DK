#!/usr/bin/env python3
"""Daily, evidence-conservative checker for user-curated home listings.

The checker NEVER infers a completed sale from a missing webpage, HTTP status,
bot protection, search-page redirect, or absence of a listing.
Status 'reachable' ONLY means the individual listing webpage is publicly readable,
NOT that the home remains available for purchase.
"""
from __future__ import annotations
import csv
import io
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
WEBSITE = ROOT / "index.html"
OUTPUT = ROOT / "listing-status.json"
SHEET_ID = "1XP_8GXN9QRKbEGqYzBudjOYazADMJqYFf8gqMiADgS8"
SHEET_GID = "762574749"
TW = timezone(timedelta(hours=8))
ALLOWED_DOMAINS = (
    "rakuya.com.tw", "591.com.tw", "housefun.com.tw", "yungching.com.tw",
    "houseprice.tw", "hbhousing.com.tw", "great-home.com.tw"
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PersonalPropertyListingChecker/1.0; +https://github.com/DEEREKFANG/DK)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5"
}

def allowed_url(url):
    try:
        u = urlparse(url)
        h = (u.hostname or "").lower()
        return u.scheme == "https" and any(h == d or h.endswith("." + d) for d in ALLOWED_DOMAINS)
    except Exception:
        return False

def load_embedded():
    text = WEBSITE.read_text("utf-8")
    found = re.search(r"const SEED_PROPERTIES=(\[.*?\]);\s*const ", text, re.S)
    if not found:
        raise RuntimeError("Cannot locate website's embedded property list")
    rows = json.loads(found.group(1))
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Embedded property list empty")
    return rows

def load_public_sheet():
    """Optional expansion: when public read permission is granted, new rows join checking."""
    url = "https://docs.google.com/spreadsheets/d/{}/export?format=csv&gid={}".format(SHEET_ID, SHEET_GID)
    try:
        rsp = requests.get(url, headers=HEADERS, timeout=13)
        if rsp.status_code != 200 or "csv" not in rsp.headers.get("content-type", "").lower():
            print("Google Sheet not publicly readable; using embedded properties only")
            return []
        content = rsp.content.decode("utf-8-sig", "replace")
        if not content.startswith("編號,") or "原始網址" not in content.splitlines()[0]:
            print("Google Sheet format invalid; using embedded properties only")
            return []
        rows = []
        for x in csv.DictReader(io.StringIO(content)):
            try:
                seq = int(float(x.get("編號") or ""))
            except ValueError:
                continue
            url = (x.get("原始網址") or "").strip()
            rows.append({
                "id": "H{:03d}".format(seq),
                "seq": seq,
                "name": x.get("房屋／社區", "").strip(),
                "url": url,
                "district": x.get("區域", "").strip(),
            })
        return rows
    except requests.RequestException as exc:
        print("Public Sheet unavailable:", type(exc).__name__)
        return []

def properties():
    included = {int(p["seq"]): p for p in load_embedded() if p.get("seq") is not None}
    for s in load_public_sheet():
        n = s["seq"]
        if n in included:
            old = included[n]
            for key in ("name", "url"):
                if s.get(key):
                    old[key] = s[key]
        else:
            included[n] = s
    return [included[n] for n in sorted(included)]

def result(p, status, reason, http_code=None, page_title=""):
    return {
        "id": str(p.get("id") or "H{:03d}".format(int(p["seq"]))),
        "seq": int(p.get("seq") or 0),
        "name": str(p.get("name") or "")[:160],
        "sourceUrl": str(p.get("url") or "")[:1600],
        "status": status,
        "reason": reason,
        "httpCode": http_code,
        "pageTitle": page_title[:150],
        "checkedAt": datetime.now(TW).isoformat(timespec="seconds"),
    }

def check_one(p):
    raw = str(p.get("url") or "").strip()
    if not raw:
        return result(p, "no_url", "原始單戶刊登網址缺漏；無法自動確認")
    u = urlparse(raw)
    host = (u.hostname or "").lower()
    if host == "community.rakuya.com.tw" or "/community/" in u.path:
        return result(p, "community_page", "來源為社區或彙整頁，不能驗證指定單戶")
    if not allowed_url(raw):
        return result(p, "unknown", "網址不是受支援的原始房仲 HTTPS 網域")

    try:
        response = requests.get(raw, headers=HEADERS, timeout=16, allow_redirects=True,
                                stream=True)
        code = response.status_code
        final = response.url
        if code in (404, 410):
            return result(p, "missing_page", "房源頁回傳 {}；可能下架，不代表已成交".format(code), code)
        if code in (401, 403, 407, 429, 503):
            return result(p, "blocked", "網站拒絕或限制自動讀取（HTTP {}）".format(code), code)
        if code >= 400:
            return result(p, "unknown", "網站回傳 HTTP {}，無法判斷是否仍在售".format(code), code)
        if not allowed_url(final):
            return result(p, "blocked", "房仲導向其他網域，可能需要登入或驗證", code)
        if "/login" in urlparse(final).path.lower() or "accounts.google" in final:
            return result(p, "blocked", "需要登入，無法確認", code)
        chunks = []
        length = 0
        for chunk in response.iter_content(chunk_size=65536):
            chunks.append(chunk)
            length += len(chunk)
            if length >= 1400000:
                break
        response.close()
        content = b"".join(chunks)
        ctype = response.headers.get("content-type", "")
        if "html" not in ctype.lower() and b"<html" not in content[:2000].lower():
            return result(p, "unknown", "非 HTML 刊登頁面，無法判斷", code)
        # requests may mis-detect Chinese encodings on some broker websites.
        encoding = response.apparent_encoding or "utf-8"
        soup = BeautifulSoup(content.decode(encoding, errors="replace"), "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        meta = []
        for tag in soup.find_all("meta"):
            name = str(tag.get("property") or tag.get("name") or "").lower()
            if name in ("og:title", "og:description", "description", "twitter:title"):
                meta.append(str(tag.get("content") or "")[:500])
        heading = " ".join(h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"], limit=5))
        focus = " ".join([title, heading, *meta])
        for tag in soup(["script", "style", "nav", "footer", "noscript"]):
            tag.decompose()
        readable = soup.get_text(" ", strip=True)
        first = unescape(readable[:4500])
        lower = (title + " " + first).lower()

        if any(token in lower for token in ("captcha", "cloudflare", "access denied", "verify you are human",
                                            "請完成驗證", "機器人驗證", "異常流量", "請先登入")):
            return result(p, "blocked", "房仲網站出現驗證或機器人限制", code, title)
        # Sales must be explicitly attributed to *this listing*, not a list of suggested other houses.
        sold_strong = re.search(r"(本[戶屋案]|此[屋戶筆間]|這[個筆間])(?:房[屋子])?.{0,6}(?:已成交|已售出|已出售|已售罄)", focus)
        sold_title = re.search(r"^[（(\[\s]*(?:已成交|已售出|已出售)[）)\]\s]*", title)
        title_has_sold = re.search(r"[（(\[\s](?:已成交|已售出|已出售)[）)\]\s]", title)
        if sold_strong or (title_has_sold and any(x in focus for x in ("售屋", "房屋", "住宅", "不動產"))):
            return result(p, "sold", "本戶刊登頁明確標示已成交或已售出，建議人工複核", code, title)
        removed_strong = re.search(r"(?:本物件|此物件|此房源|該房源|本案|該物件).{0,7}(?:已下架|已撤銷|已撤售|已結案|不存在|已刪除)", focus + " " + first[:1200])
        if removed_strong:
            return result(p, "removed", "房仲頁面明示此物件已下架／撤售（不等於成交）", code, title)

        # A successful response alone isn't evidence that the seller will still transact.
        has_home_terms = any(t in first[:4500] for t in ("坪", "房", "售價", "總價", "萬", "地址", "樓層"))
        looks_listing = has_home_terms and (
            len(title) >= 6 and
            (("house/" in urlparse(final).path) or
             ("sell_item" in urlparse(final).path) or
             ("sale" in urlparse(final).path) or
             ("sell/info" in urlparse(final).path) or
             ("sell" in urlparse(final).path))
        )
        if looks_listing:
            return result(p, "reachable", "個別刊登頁可讀取；不代表尚未收訂或確定在售", code, title)
        return result(p, "unknown", "頁面可開啟，但未確認是該戶的有效刊登", code, title)
    except (requests.RequestException, UnicodeError, ValueError) as exc:
        return result(p, "unknown", "自動查核無法取得有效頁面：{}".format(type(exc).__name__))

def main():
    props = properties()
    if not props:
        raise RuntimeError("Refusing to overwrite results with empty property list")
    prev = {}
    if OUTPUT.exists():
        try:
            prev = {str(x["id"]): x for x in json.loads(OUTPUT.read_text("utf-8")).get("results", [])}
        except (ValueError, KeyError):
            pass
    print("Checking {} property source records".format(len(props)))
    current = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(check_one, p): p for p in props}
        for done in as_completed(jobs):
            p = jobs[done]
            try:
                x = done.result()
            except Exception as exc:
                x = result(p, "unknown", "檢查程序失敗：{}".format(type(exc).__name__))
            prior = prev.get(x["id"])
            # During connectivity outages keep a previous definitive signal as contextual evidence,
            # without reporting it as the current day's verified status.
            x["previousStatus"] = prior.get("status") if prior else None
            x["changedAt"] = (prior.get("changedAt") if prior and prior.get("status") == x["status"]
                               else x["checkedAt"])
            current[x["id"]] = x
            print("#{:<3} {:<16} {}".format(x["seq"], x["status"], x["reason"][:64]))

    values = [current[k] for k in sorted(current, key=lambda k: current[k]["seq"])]
    counts = dict(Counter(x["status"] for x in values))
    result_json = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(TW).isoformat(timespec="seconds"),
        "timezone": "Asia/Taipei",
        "notice": "可讀取刊登頁不代表真正可購買；僅明確出現本戶已成交才標已售，404不算成交。",
        "total": len(values),
        "counts": counts,
        "results": values,
    }
    OUTPUT.write_text(json.dumps(result_json, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print("Summary:", json.dumps(counts, ensure_ascii=False))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## 房源每日在售狀態查核\n\n")
            f.write("共 {} 筆，結果：{}\n\n".format(len(values), json.dumps(counts, ensure_ascii=False)))
            f.write("來源阻擋、無效連結、社區彙整頁不應判斷為已成交。詳細資料在 listing-status.json。\n")

if __name__ == "__main__":
    main()
