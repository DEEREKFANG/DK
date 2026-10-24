#!/usr/bin/env python3
"""Best-effort cover photos from each *specific* sale listing, for this personal property dashboard.

Photos are sourced ONLY from the property's own original listing URL.  They are
not inferred from a community-name image search, neighbouring units, or stock photos.
Read only publisher-supplied image metadata; do not bypass robots/CAPTCHAs/login.
No image bytes are copied; remote publishers continue hosting their own images.
"""
import concurrent.futures
import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "sheet-data.json"
CACHE = ROOT / "listing-photos.json"
UTC = timezone.utc
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HomeComparisonLinkPreview/1.0)",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    "Accept-Language": "zh-TW,zh;q=0.8,en;q=0.5",
}
BLOCKED_WORDS = re.compile(
    r"(?:logo|favicon|avatar|profile|default|placeholder|loading|sprite|banner|advert|icon(?:[_./-]|$)|"
    r"wechat|line_qr|qrcode|share[_-]?img|fb[_-]?share|empty[_-]?image|og-default)",
    re.I,
)
SUPPORTED = (
    "www.rakuya.com.tw",
    "community.rakuya.com.tw",
    "www.hbhousing.com.tw",
    "sale.591.com.tw",
    "buy.housefun.com.tw",
    "www.great-home.com.tw",
    "buy.houseprice.tw",
    "buy.yungching.com.tw",
    "www.sinyi.com.tw",
)
MAX_FETCH = 70  # cover remaining exact-unit links in this follow-up run
def safe_url(url):
    try:
        p = urlparse(str(url).strip())
        if p.scheme != "https" or not p.hostname:
            return ""
        if p.username or p.password or p.port not in (None,443):
            return ""
        if p.hostname in ("localhost",) or p.hostname.endswith((".local",".internal")):
            return ""
        return p.geturl()
    except ValueError:
        return ""

class Metadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = []
        self.title = ""
        self.ld = []
        self.read_title = False
        self.read_ld = False
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            k = str(a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            v = a.get("content", "")
            if k in ("og:image", "og:image:url", "twitter:image", "twitter:image:src", "image"):
                self.meta.append((k,v))
        if tag == "title": self.read_title = True
        if tag == "script" and a.get("type","").lower() == "application/ld+json":
            self.read_ld = True
    def handle_endtag(self, tag):
        if tag == "title": self.read_title = False
        if tag == "script": self.read_ld = False
    def handle_data(self, data):
        if self.read_title: self.title += data
        if self.read_ld: self.ld.append(data)

def extract_image(html, listing_url):
    p = Metadata()
    p.feed(html[:1_500_000])
    candidates = [x for x in p.meta if x[0] in ("og:image", "og:image:url", "twitter:image", "twitter:image:src", "image")]
    def find_ld(obj):
        if isinstance(obj,dict):
            image = obj.get("image")
            if isinstance(image,str): candidates.append(("jsonld",image))
            elif isinstance(image,dict): candidates.append(("jsonld",image.get("url") or image.get("contentUrl") or ""))
            elif isinstance(image,list):
                for entry in image[:4]:
                    candidates.append(("jsonld",entry if isinstance(entry,str) else entry.get("url","") if isinstance(entry,dict) else ""))
            for k in ("@graph", "mainEntity"):
                if k in obj: find_ld(obj[k])
        elif isinstance(obj,list):
            for x in obj[:12]: find_ld(x)
    for fragment in p.ld[:8]:
        try: find_ld(json.loads(fragment))
        except (ValueError,TypeError): pass
    origin = urlparse(listing_url).hostname
    for label,raw in candidates:
        if not isinstance(raw,str) or not raw.strip():continue
        url=safe_url(urljoin(listing_url,raw.strip()))
        if not url: continue
        pu=urlparse(url)
        if pu.hostname in ("www.facebook.com","www.google.com","www.rakuya.com.tw") and pu.path in ("/","/favicon.ico"):
            continue
        if BLOCKED_WORDS.search(pu.path):continue
        # metadata needs to identify this advertised *individual listing*.
        # Extremely short/generic image paths often mean a publisher's corporate cover.
        if len(pu.path)<9: continue
        return url, label, p.title.strip()[:140]
    return "", "", p.title.strip()[:140]

def fetch_photo(record):
    """Get a safe primary cover; if blocked, use a cross-matched same-unit agent page."""
    seq, listing = record["seq"], record["url"]
    alternative = record.get("alternative", "")
    last = {"photoUrl":"","reason":"no_listing_image_metadata"}
    sources = [("listing",listing)]
    if alternative and safe_url(alternative):
        sources.append(("verified_matching_broker_listing",alternative))
    for origin_label, candidate in sources:
        try:
            response=requests.get(candidate,timeout=(4,8),headers=HEADERS,allow_redirects=True)
            if response.status_code!=200:
                last={"photoUrl":"","reason":"http_"+str(response.status_code)}
                continue
            page=urlparse(response.url)
            original=urlparse(candidate)
            if page.hostname != original.hostname:
                item_id = re.search(r"(?:[?&]ehid=)([a-zA-Z0-9]+)", candidate, re.I)
                same_vendor = (page.hostname or "").endswith(".rakuya.com.tw") and (
                    original.hostname or "").endswith(".rakuya.com.tw")
                same_item = bool(item_id and item_id.group(1) in response.url)
                if not (same_vendor and same_item):
                    last={"photoUrl":"","reason":"redirect_other_host", "redirect_to":response.url[:220]}
                    continue
            if response.url.rstrip("/") == ("https://"+original.hostname):
                last={"photoUrl":"","reason":"redirect_to_home"}
                continue
            ctype=response.headers.get("content-type","").lower()
            if "html" not in ctype and "text/" not in ctype:
                last={"photoUrl":"","reason":"not_html"}
                continue
            if origin_label != "listing":
                # Cross-match the independent ad against sale price, area, and name.
                body=response.text
                expected_name=record.get("community","")
                price=str(record.get("price",""))
                area=str(record.get("totalArea",""))
                if not expected_name or expected_name not in body or price not in body or area not in body:
                    last={"photoUrl":"","reason":"secondary_not_verified"}
                    continue
            image,kind,title=extract_image(response.text,response.url)
            if not image:
                last={"photoUrl":"","reason":"no_listing_image_metadata"}
                continue
            return seq,{"photoUrl":image,"source":origin_label+"_"+kind,
                        "reason":"ok","title":title}
        except requests.RequestException as exc:
            last={"photoUrl":"","reason":type(exc).__name__}
    return seq,last

def main():
    doc=json.loads(DATA.read_text("utf-8"))
    if doc.get("schemaVersion")!=1 or not isinstance(doc.get("items"),list):
        raise RuntimeError("Invalid sheet-data.json")
    data=doc["items"]
    if len(data)<60:raise RuntimeError("Unexpectedly small house inventory")
    prev=json.loads(CACHE.read_text("utf-8")) if CACHE.exists() else {"items":[]}
    old={int(x["seq"]):x for x in prev.get("items",[]) if "seq" in x}
    now=datetime.now(UTC)
    records=[]
    stored={}
    for house in data:
        seq=int(house["seq"])
        url=safe_url(house.get("url",""))
        # User's manually verified photo takes precedence.
        manual=safe_url(house.get("photoUrl",""))
        if manual: continue
        if not url or urlparse(url).hostname not in SUPPORTED:continue
        prior=old.get(seq,{})
        if prior.get("url")==url:
            try:checktime=datetime.fromisoformat(prior["checkedAt"])
            except (KeyError,ValueError):checktime=now-timedelta(days=20)
            ttl=(timedelta(days=7) if prior.get("photoUrl") else
                 timedelta(seconds=0) if prior.get("reason") in ("redirect_other_host","http_403") else timedelta(hours=12))
            if now-checktime<ttl:
                stored[seq]=prior
                continue
        alternative = ""
        related = house.get("related","")
        match = re.search(r"https://www\.mysweethome\.com\.tw/sell_item/[A-Za-z0-9_-]+", related)
        if match: alternative = match.group(0)
        records.append({"seq":seq,"url":url,"alternative":alternative,
                        "community":house.get("name","").split("・")[0].strip(),
                        "price":house.get("price"),"totalArea":house.get("totalArea")})
    # Avoid pounding listing websites; batches progressively fill blank images.
    records=records[:MAX_FETCH]
    results={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        for seq,photo in pool.map(fetch_photo,records):
            photo.update({"seq":seq,"url":next(x["url"] for x in records if x["seq"]==seq),
                          "checkedAt":now.isoformat(timespec="seconds")})
            results[seq]=photo
    stored.update(results)
    # Carry cached records that weren't checked in this invocation.
    for seq,entry in old.items():
        if seq not in stored:
            house=next((x for x in data if x.get("seq")==seq),None)
            if house and safe_url(house.get("url"))==entry.get("url"):
                stored[seq]=entry
    # Overlay only from the exact listing URL, preserving manual photos.
    photos=0
    for house in data:
        if house.get("photoUrl"):photos+=1;continue
        entry=stored.get(int(house["seq"]),{})
        if entry.get("url")==house.get("url") and entry.get("photoUrl"):
            house["photoUrl"]=entry["photoUrl"]
            house["photoSource"]=entry.get("source","listing_metadata")
            photos+=1
    newcache=json.dumps({"schemaVersion":1,"source":"individual_listing_metadata",
                         "items":sorted(stored.values(),key=lambda x:x["seq"])},ensure_ascii=False,indent=2)+"\n"
    if not CACHE.exists() or CACHE.read_text("utf-8")!=newcache:
        CACHE.write_text(newcache,encoding="utf-8")
    output=json.dumps(doc,ensure_ascii=False,indent=2)+"\n"
    if DATA.read_text("utf-8")!=output:
        DATA.write_text(output,encoding="utf-8")
    print("PHOTO_SYNC_OK: checked_this_run={}, known_image_urls={}, total_properties={}".format(
        len(results),photos,len(data)))
    for seq,entry in sorted(results.items()):
        print("photo #{:02d}: {}".format(seq, entry["reason"]+
                 (" "+entry["photoUrl"][:140] if entry.get("photoUrl") else "")))

if __name__=="__main__":
    main()
