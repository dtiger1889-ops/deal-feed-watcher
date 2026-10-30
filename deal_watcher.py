#!/usr/bin/env python3
"""Deal feed watcher -- a zero-AI deal poller.

Fetches deal feeds (RSS/Atom, Shopify JSON, the Woot API, one HTML tile page),
dedupes into SQLite, scores new items against watchlist.json (plain regex, no
AI), and regenerates a static HTML dashboard plus digest_feed.json, a small
file any downstream notifier can read.

Run:  python deal_watcher.py                (on a schedule, a few times a day)
      python deal_watcher.py --dashboard    (re-render the page from the DB, no fetch)
      python deal_watcher.py --rescore      (apply a watchlist edit to stored deals)
      python deal_watcher.py --rebuild      (re-extract prices + rebuild baselines)
      python deal_watcher.py --consolidate  (monthly: fold old price-archive
                                             rows into price_history.json)
      python deal_watcher.py --tuning-stats (write tuning_stats.json for a
                                             periodic watchlist review)
Stdlib only -- no pip installs needed. Python 3.10+.

Config: sources.json (which feeds to poll) and watchlist.json (what you want);
copy the .example.json files to start. The dashboard is one self-contained
HTML file; set DEAL_WATCHER_DASHBOARD to write it somewhere a web server
already serves. Keep this folder itself off any web server: it holds API keys.
"""

import json
import os
import re
import sqlite3
import statistics
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html import escape, unescape
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_FILE = BASE / "deals.db"
WATCHLIST_FILE = BASE / "watchlist.json"
SOURCES_FILE = BASE / "sources.json"
# To publish the page, WRITE it into a folder a web server already serves; never serve this
# folder, because it holds woot_api_key.txt and ccc_feed_url.txt.
DASHBOARD_FILE = Path(os.environ.get("DEAL_WATCHER_DASHBOARD") or (BASE / "dashboard.html"))
DIGEST_FILE = BASE / "digest_feed.json"
PRICE_HISTORY_FILE = BASE / "price_history.json"
LOG_FILE = BASE / "watcher.log"

USER_AGENT = "deal-feed-watcher/1.0 (personal RSS reader)"
FETCH_TIMEOUT = 30
DIGEST_WINDOW_HOURS = 48
DASHBOARD_RECENT_LIMIT = 250
# A repeat offer counts as a price-DROP event only when its new price undercuts
# the lowest price this row has ever carried by at least this much. Measured
# against the all-time low (not yesterday's price) so a source's daily +/- churn
# cannot re-report the same offer.
PRICE_DROP_MIN_PCT = 10
# Freshness: the main page shows a deal only while its source still lists it
# (last_seen) or Woot's API last confirmed it live (last_verified) within this
# many days; older rows move to a separate "older listings" view. Feed absence
# is not proof of expiry (RSS rotates), so a source in sources.json may set its
# own "fresh_days" where its behaviour is known (a one-deal-a-day site: 2).
FRESH_DAYS_DEFAULT = 7


def load_sources():
    """sources.json: {"sources": [{name, type, url | url_file, enabled?, fresh_days?}]}.
    type is one of rss (RSS 2.0 or Atom), shopify (a store's /products.json),
    woot (the Woot developer API; key in woot_api_key.txt) or bradsdeals_html."""
    if not SOURCES_FILE.exists():
        raise SystemExit(f"{SOURCES_FILE.name} not found: copy sources.example.json to start")
    return json.loads(SOURCES_FILE.read_text(encoding="utf-8")).get("sources", [])


SOURCES = load_sources() if SOURCES_FILE.exists() else []
SOURCE_FRESH_DAYS = {s["name"]: s["fresh_days"] for s in SOURCES if s.get("fresh_days")}

WOOT_KEY_FILE = BASE / "woot_api_key.txt"  # the Woot API key; keep it out of git

# CamelCamelCamel's per-account alert feed URL is itself a secret, so its source
# entry names a url_file instead of a url. With no alerts pending it emits one
# placeholder item pointing at the tracker page -- not a deal, never stored.
CCC_PLACEHOLDER_URL = "camelcamelcamel.com/tracker"


def woot_key(source):
    if WOOT_KEY_FILE.exists():
        return WOOT_KEY_FILE.read_text(encoding="utf-8").strip()
    return ""

# Mechanical category rules, first match wins. Dashboards group by these;
# categories listed in the watchlist's "matched_only_categories" only display
# rows that matched a watchlist entry.
CATEGORY_RULES = [
    ("computers", r"NVMe|M\.2|\bSSD\b|\bHDD\b|hard drive|\bRAM\b|DDR[45]|\bCPU\b|\bGPU\b|"
                  r"RTX|Radeon|Ryzen|motherboard|\bPSU\b|power supply|laptop|desktop|monitor|"
                  r"\bPC\b|chromebook|MacBook"),
    ("electronics", r"Raspberry\s?Pi|\bTV\b|headphone|earbud|soundbar|speaker|\btablet\b|iPad|"
                    r"\bphone\b|camera|Amazon Echo|Echo (Dot|Show|Studio|Pop|Buds|Plus|Spot|\()|"
                    r"Alexa|Zigbee|smart (home|plug|bulb)|Home Assistant|"
                    r"console|Nintendo|PlayStation|Xbox|\b(mouse|keyboard)\b"),
    ("camping-outdoors", r"\btent\b|sleeping bag|camping|\bcooler\b|hiking|air mattress"),
    ("home", r"vacuum|furniture|mattress|kitchen|appliance|air fryer|\bsofa\b|\brug\b|ironing board|"
             r"sectional|\bcouch\b|loveseat|recliner|"
             r"air (filter|purifier)|MERV|LED strip|light bulb"),
    ("clothing", r"jacket|\bcoat\b|shirt|\btee\b|t-shirt|undershirt|pants|jeans|shoes|sneaker|"
                 r"boots|apparel|clothing|sweater|hoodie|\bsocks\b|underwear|shorts"),
]

# Computer-part price archive: mechanically archive priced deals on parts you
# may want in the FUTURE, so an eventual purchase can compare price movement
# over time. Only rows with a clearly captured price are kept.
PART_TYPES = [
    ("nvme", r"NVMe|M\.2"),
    ("ssd", r"\bSSD\b|solid state"),
    ("hdd", r"\bHDD\b|hard drive"),
    ("ram", r"\bRAM\b|DDR[45]"),
    ("gpu", r"\bGPU\b|RTX\s?\d+|Radeon|graphics card"),
    ("cpu", r"\bCPU\b|Ryzen|Core i[3579]|processor"),
    ("psu", r"\bPSU\b|power supply"),
    ("motherboard", r"motherboard"),
]
SPEC_RE = re.compile(r"(\d+(?:\.\d+)?)\s?(TB|GB)\b", re.IGNORECASE)


def log(msg):
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z {msg}"
    if sys.stdout:  # absent under pythonw.exe (the scheduled-task host)
        print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(url, extra_headers=None):
    headers = {"User-Agent": USER_AGENT,
               "Accept": "application/rss+xml, application/atom+xml, application/json, text/xml, */*"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read()


def strip_tags(html_text):
    return unescape(re.sub(r"<[^>]+>", " ", html_text or "")).strip()


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(raw, source_name):
    """Parse RSS 2.0 or Atom bytes into item dicts. Namespace-agnostic."""
    root = ET.fromstring(raw)
    items = []
    root_tag = _local(root.tag)
    if root_tag == "rss":
        for item in root.iter():
            if _local(item.tag) != "item":
                continue
            fields = {}
            for child in item:
                fields.setdefault(_local(child.tag), (child.text or "").strip())
            url = fields.get("link", "")
            items.append({
                "guid": fields.get("guid") or url,
                "title": unescape(fields.get("title", "")),
                "url": url,
                "description": strip_tags(fields.get("description", ""))[:1500],
                "published": parse_date(fields.get("pubDate")),
                "source": source_name,
            })
    elif root_tag == "feed":  # Atom (Reddit)
        for entry in root.iter():
            if _local(entry.tag) != "entry":
                continue
            fields, url = {}, ""
            for child in entry:
                tag = _local(child.tag)
                if tag == "link" and not url:
                    url = child.get("href", "")
                else:
                    fields.setdefault(tag, (child.text or "").strip())
            items.append({
                "guid": fields.get("id") or url,
                "title": unescape(fields.get("title", "")),
                "url": url,
                "description": strip_tags(fields.get("summary") or fields.get("content", ""))[:1500],
                "published": parse_date(fields.get("updated") or fields.get("published")),
                "source": source_name,
            })
    return items


def parse_shopify(raw, source_name, store_url):
    data = json.loads(raw)
    base = store_url.split("/products.json")[0]
    items = []
    for p in data.get("products", []):
        variants = p.get("variants", [])
        prices = [float(v["price"]) for v in variants if v.get("price")]
        price = min(prices) if prices else None
        title = p.get("title", "")
        if price is not None:
            title = f"{title} (${price:.2f})"
        items.append({
            "guid": f"shopify-{p.get('id')}",
            "title": title,
            "url": f"{base}/products/{p.get('handle', '')}",
            "description": strip_tags(p.get("body_html", ""))[:1500],
            "published": parse_date(p.get("published_at")),
            "source": source_name,
        })
    return items


def parse_woot(raw, source_name):
    data = json.loads(raw)
    items = []
    for offer in data if isinstance(data, list) else data.get("Items", []):
        title = offer.get("Title", "")
        price = offer.get("SalePrice", {})
        if isinstance(price, dict):
            price = price.get("Minimum")
        if price:
            title = f"{title} (${price})"
        # Woot states the pre-deal price in ListPrice and its titles never say
        # "was $X", so without this most Woot rows carry no discount. Written
        # into the description as "List price $X" so the normal ORIG_PRICE_RE
        # path picks it up; skipped when it is not actually above the sale price.
        listp = offer.get("ListPrice", {})
        if isinstance(listp, dict):
            listp = listp.get("Minimum")
        description = ""
        if listp and price and listp > price:
            description = f"List price ${listp}"
        items.append({
            "guid": f"woot-{offer.get('OfferId') or offer.get('Id')}",
            "title": title,
            "url": offer.get("Url", ""),
            "description": description,
            "published": parse_date(offer.get("StartDate")),
            "source": source_name,
        })
    return items


# BradsDeals has no deal RSS; its deal tiles are server-rendered HTML. The
# headline anchor carries the deal link, the h3 inside it the title (which
# states the price).
BRADSDEALS_TILE_RE = re.compile(
    r'<a\b[^>]*?href="([^"]+)"[^>]*?class="[^"]*post-tile-vertical__headline-link[^"]*"'
    r'[^>]*>.*?<h3[^>]*post-tile-vertical__headline-text[^>]*>(.*?)</h3>',
    re.S)


def parse_bradsdeals(raw, source_name, page_url):
    """Parse bradsdeals.com/deals server-rendered HTML tiles. A redesign that
    breaks the regex degrades to 0 items logged, never a crash."""
    html_text = raw.decode("utf-8", errors="replace")
    origin = "/".join(page_url.split("/")[:3])  # https://www.bradsdeals.com
    items, seen = [], set()
    for href, title_html in BRADSDEALS_TILE_RE.findall(html_text):
        url = href if href.startswith("http") else origin + href
        if url in seen:
            continue  # tiles repeat across page sections; dedupe key = deal URL
        seen.add(url)
        items.append({
            "guid": url,
            "title": strip_tags(title_html),
            "url": url,
            "description": "",
            "published": None,
            "source": source_name,
        })
    return items


PRICE_RE = re.compile(r"\$\s?(\d{1,4}(?:,\d{3})?(?:\.\d{2})?)\s?([kK]\b)?")
ORIG_PRICE_RE = re.compile(
    r"(?:reg(?:\.|ularly)?|was|orig(?:\.|inally)?|list(?: price)?|valued?(?: at| up to)|MSRP"
    # Phrasings the feeds actually use for the old price: some write
    # "down from $X" rather than "was $X".
    r"|down from(?: the)?|reduced from|normally|usually|retails? for|sold for"
    r"|compared? (?:to|at))\s*:?\s*"
    r"\$\s?(\d{1,4}(?:,\d{3})?(?:\.\d{2})?)\s?([kK]\b)?", re.IGNORECASE)
PCT_OFF_RE = re.compile(r"(\d{1,2})\s?%\s?off", re.IGNORECASE)
# A $ figure the TITLE itself claims as the deal price ("... for $97",
# "Only $29.99", or a parser-appended "($66.99)" from the Woot/Shopify paths).
TITLE_PRICE_RE = re.compile(
    r"(?:\b(?:for|only|just|now)|:|\))\s*\$\s?(\d{1,4}(?:,\d{3})?(?:\.\d{2})?)\s?([kK]\b)?(?![\d.,]|\s*off\b)"
    r"|\(\$\s?(\d{1,4}(?:,\d{3})?(?:\.\d{2})?)\)", re.IGNORECASE)
# Context marking a $ figure as NOT a sale price: shipping charges, coupon
# thresholds, discount amounts, price caps. Taking min() over every figure
# stores shipping charges as prices (see lessons/what-the-feeds-taught.md).
PRICE_CTX_BEFORE = re.compile(
    r"(?:shipping(?:\s+is)?|s\s?&\s?h|s/h|w/|\+|over|under|save|extra|plus|get)\s*$",
    re.IGNORECASE)
PRICE_CTX_AFTER = re.compile(r"^\s*(?:off\b|or\s+more\b)", re.IGNORECASE)


def _price_val(num, k=None):
    v = float(num.replace(",", ""))
    return v * 1000 if k else v


def extract_pricing(title, description=""):
    """Return (sale_price, orig_price, discount_pct). Any may be None.

    Sale price: what the TITLE states, first; else the largest clean figure
    below the stated original; else the smallest clean figure. "Clean" =
    not flanked by shipping/coupon/threshold context (PRICE_CTX_*).
    """
    title = title or ""
    text = f"{title} {description or ''}".strip()
    orig_m = ORIG_PRICE_RE.search(text)
    orig = _price_val(orig_m.group(1), orig_m.group(2)) if orig_m else None
    # A roundup states its own headline discount ("Up to 70% off") while its
    # blurb quotes one sample item's old price. Comparing that sample's price
    # to the roundup's cheapest figure produces a nonsense percentage, so when
    # the TITLE states a discount, the description's old-price figure is
    # dropped and the title's claim stands.
    if orig_m and PCT_OFF_RE.search(title) and orig_m.start() >= len(title):
        orig_m, orig = None, None

    tm = TITLE_PRICE_RE.search(title)
    if tm:
        sale = _price_val(tm.group(1) or tm.group(3), tm.group(2))
    else:
        clean = []
        for m in PRICE_RE.finditer(text):
            if orig_m and orig_m.start() <= m.start() < orig_m.end():
                continue  # the original/list figure itself
            if PRICE_CTX_BEFORE.search(text[max(0, m.start() - 24):m.start()]):
                continue
            if PRICE_CTX_AFTER.match(text[m.end():m.end() + 12]):
                continue
            clean.append(_price_val(m.group(1), m.group(2)))
        if orig is not None:
            below = [p for p in clean if p < orig]
            sale = max(below) if below else None
        else:
            sale = min(clean) if clean else None

    pct = None
    if sale is not None and orig and orig > sale:
        pct = round((orig - sale) / orig * 100)
    else:
        pct_m = PCT_OFF_RE.search(text)
        if pct_m:
            pct = int(pct_m.group(1))
            if sale is not None and orig is None and 0 < pct < 100:
                orig = round(sale / (1 - pct / 100), 2)
    return sale, orig, pct


def load_watchlist():
    if not WATCHLIST_FILE.exists():
        raise SystemExit(f"{WATCHLIST_FILE.name} not found: copy watchlist.example.json to start")
    wl = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    def comp(lst):
        return [re.compile(p, re.IGNORECASE) for p in lst]
    entries = []
    for entry in wl.get("entries", []):
        entries.append({
            "id": entry["id"],
            "tier": entry.get("tier", "watch"),
            "patterns": comp(entry.get("patterns", [])),
            "exclude_patterns": comp(entry.get("exclude_patterns", [])),
            "require_patterns": comp(entry.get("require_patterns", [])),
            "price_max_usd": entry.get("price_max_usd"),
            "min_discount_pct": entry.get("min_discount_pct"),
            "needs_detail": entry.get("needs_detail", False),
        })
    return {
        "entries": entries,
        "never_show": comp(wl.get("never_show", [])),
        "hidden_for_now": comp(wl.get("hidden_for_now", [])),
        # Categories whose deals only show when a watchlist entry matched them.
        "matched_only_categories": list(wl.get("matched_only_categories", [])),
        # If set, unmatched "computers" deals show only for these part types
        # (PART_TYPES keys); everything else in the category is suppressed.
        "unmatched_computer_parts": wl.get("unmatched_computer_parts"),
    }


# Words that make a CATEGORY_RULES hit a false positive: the rule's word is
# present but the deal is something else (a cartoon-mouse plush filed under
# Electronics off the computer-mouse word). A title matching one of these skips
# that category and falls through.
CATEGORY_ANTI = {
    "camping-outdoors": r"\bwine cooler\b|\bbeverage cooler\b",
    "electronics": r"mickey|minnie|mouse ears|console table|camera bag",
    "computers": r"monitor stand|laptop (bag|sleeve|backpack|stand)",
}


def categorize(title):
    """Category comes from the TITLE only.

    Feed descriptions list unrelated products, so scoring the description too
    drags roundups into whatever a stray mention hits first (a warehouse-club
    sale filed as 'electronics' because its blurb names a tablet). Watchlist
    MATCHING still reads the description (that is how a roundup earns its
    'why it's in there' tag); only the category label is title-driven."""
    for cat, pattern in CATEGORY_RULES:
        if not re.search(pattern, title, re.IGNORECASE):
            continue
        anti = CATEGORY_ANTI.get(cat)
        if anti and re.search(anti, title, re.IGNORECASE):
            continue
        return cat
    return "other"


def score_item(item, wl):
    """Return (matched_ids, tier, suppressed, category).

    never_show always wins. hidden_for_now hides unless a watchlist entry
    explicitly matched (explicit interest beats a generic category hide).
    """
    haystack = f"{item['title']} {item['description']}"
    sale, orig, pct = extract_pricing(item["title"], item["description"])
    category = categorize(item["title"])

    if any(p.search(haystack) for p in wl["never_show"]):
        return [], None, 1, category

    matched, tier = [], None
    for entry in wl["entries"]:
        if not any(p.search(haystack) for p in entry["patterns"]):
            continue
        if entry["require_patterns"] and not all(p.search(haystack) for p in entry["require_patterns"]):
            continue
        if any(p.search(haystack) for p in entry["exclude_patterns"]):
            continue
        etier = entry["tier"]
        if etier == "hot":
            cap = entry["price_max_usd"]
            if entry["needs_detail"] or (cap and sale and sale > cap):
                etier = "watch"
            floor = entry["min_discount_pct"]
            if floor:
                # A discount the feed states wins. Only when the feed states
                # none does the comparison with similar stored listings decide.
                satisfied = pct is not None and pct >= floor
                if not satisfied and pct is None:
                    ctx = baseline_context(product_key(item["title"], item.get("source", "")), sale)
                    if ctx:
                        satisfied = ctx[2] >= floor
                if not satisfied:
                    etier = "watch"
        matched.append(entry["id"])
        if etier == "hot" or tier is None:
            tier = etier

    suppressed = 0
    if not matched and any(p.search(haystack) for p in wl["hidden_for_now"]):
        suppressed = 1
    # Optional parts filter: unmatched computer deals outside the listed part
    # types never display. They still reach the price archive, which runs
    # independently of display.
    parts = wl["unmatched_computer_parts"]
    if parts is not None and not matched and not suppressed and category == "computers":
        part, _ = detect_part(haystack)
        if part not in parts:
            suppressed = 1
    return matched, tier, suppressed, category


def detect_part(haystack):
    """(part_type, spec) for the computer-parts price archive, else (None, None)."""
    for part, pattern in PART_TYPES:
        if re.search(pattern, haystack, re.IGNORECASE):
            spec_m = SPEC_RE.search(haystack)
            spec = f"{spec_m.group(1)}{spec_m.group(2).upper()}" if spec_m else ""
            return part, spec
    return None, None


# --- Corpus-derived discount baseline ---------------------------------------
# product_key groups structurally-similar listings so the scorer can compare a
# price against what "normal" looks like for that group. Order-independent:
# source prefix + the k most distinctive (longest, non-noise) title tokens.
# source prefix is deliberate -- Woot titles are product names, Slickdeals
# titles are retailer+deal copy, so cross-source conflation degrades medians.
_BASELINE_NOISE = re.compile(
    r"\b(deal|deals|sale|sales|save|savings|off|free|shipping|ship|today|now|new|"
    r"best|top|great|huge|big|hot|price|prices|lowest|clearance|refurb|refurbished|"
    r"open|box|w/|with|for|and|the|a|an|of|to|in|on|at|from|up|your|you|get|only|"
    r"more|plus|extra|coupon|code|promo|limited|time|day|days|week|weekend|"
    r"pack|ct|count|pcs|piece|set|bundle|assorted|various|colors|color|size|sizes)\b",
    re.IGNORECASE,
)
_BASELINE_NONWORD = re.compile(r"[^a-z0-9 ]+")
_BASELINE_NUMISH = re.compile(r"^\d+(\.\d+)?$")

_baseline_cache = None  # {product_key: (n_observed, median_price, mad)}; loaded per run


_BASELINE_UNIT = re.compile(
    r"\b(\d+(?:\.\d+)?)[- ]?(pack|pk|ct|count|pcs?|pieces?|oz|lbs?|gal|qt|ft|tb|gb|mah|"
    r"person|inch|ml)\b", re.IGNORECASE)


def product_key(title, source, k=4):
    """Order-independent key: source prefix + k most distinctive title tokens.

    Bare numbers are noise (prices, model suffixes) and are dropped, but a
    number glued to a unit is the product ("4-pack" vs "12-pack", "2TB" vs
    "4TB", "4-person" tent): those are fused into one token first so
    pack-size / capacity variants stop collapsing into one price group.
    """
    t = _BASELINE_UNIT.sub(lambda m: m.group(1).replace(".", "") + m.group(2).lower(),
                           (title or "").lower())
    t = _BASELINE_NONWORD.sub(" ", t)
    t = _BASELINE_NOISE.sub(" ", t)
    toks = [w for w in t.split() if len(w) > 2 and not _BASELINE_NUMISH.match(w)]
    if len(toks) < 2:
        return ""
    core = sorted(sorted(set(toks), key=len, reverse=True)[:k])
    return f"{source}|{'|'.join(core)}"


BASELINE_MIN_N = 4          # observations before a group is trusted for scoring
BASELINE_DISPERSION = 0.60  # skip groups whose MAD/median exceeds this (churn, not signal)


def baseline_context(pk, sale):
    """(median, n, implied_pct) for a priced deal against its corpus group, or
    None when the group is too small / too dispersed / absent. Shared by the
    scorer's hot/watch gate and the stored per-row comparison the digest reads."""
    if not (pk and sale and _baseline_cache):
        return None
    base = _baseline_cache.get(pk)
    if not base or base[0] < BASELINE_MIN_N:
        return None
    n, med, mad = base
    if not med or (mad and mad / med > BASELINE_DISPERSION):
        return None
    return med, n, round(100 * (med - sale) / med)


def refresh_baseline_group(db, pk, day_iso, min_n=3):
    """Recompute ONE group's baseline exactly from the deals table (median +
    MAD of the current prices of every row in the group). Creates the group
    once it reaches min_n rows."""
    if not pk:
        return None
    prices = [p for (p,) in db.execute(
        "SELECT price FROM deals WHERE product_key=? AND price > 0", (pk,))]
    if len(prices) < min_n:
        return None
    med = statistics.median(prices)
    m = statistics.median([abs(p - med) for p in prices]) or 0.0
    db.execute(
        "INSERT OR REPLACE INTO price_baseline (product_key, n_observed, median_price, "
        "min_price, mad, last_updated) VALUES (?,?,?,?,?,?)",
        (pk, len(prices), round(med, 2), round(min(prices), 2), round(m, 2), day_iso))
    if _baseline_cache is not None:
        _baseline_cache[pk] = (len(prices), round(med, 2), round(m, 2))
    return len(prices)


def load_baseline(db):
    """Load the whole price_baseline table into a dict (score_item runs in a
    tight per-deal loop -- don't hit SQLite per call)."""
    global _baseline_cache
    rows = db.execute(
        "SELECT product_key, n_observed, median_price, mad FROM price_baseline"
    ).fetchall()
    _baseline_cache = {r[0]: (r[1], r[2], r[3]) for r in rows}


def build_baseline_groups(priced):
    """priced: iterable of (product_key, sale_price). Return {pk: [prices,...]}."""
    groups = {}
    for pk, sale in priced:
        if pk and sale and sale > 0:
            groups.setdefault(pk, []).append(sale)
    return groups


def write_baseline(db, groups, day_iso, min_n=3):
    """Replace price_baseline from grouped prices (exact medians)."""
    db.execute("DELETE FROM price_baseline")
    for pk, prices in groups.items():
        if len(prices) >= min_n:
            med = statistics.median(prices)
            m = statistics.median([abs(p - med) for p in prices]) or 0.0
            db.execute(
                "INSERT INTO price_baseline (product_key, n_observed, median_price, "
                "min_price, mad, last_updated) VALUES (?,?,?,?,?,?)",
                (pk, len(prices), round(med, 2), round(min(prices), 2),
                 round(m, 2), day_iso))


def open_db():
    db = sqlite3.connect(DB_FILE)
    db.execute("""CREATE TABLE IF NOT EXISTS deals (
        guid TEXT PRIMARY KEY,
        source TEXT, title TEXT, url TEXT, description TEXT,
        price REAL, published TEXT, first_seen TEXT,
        matched TEXT, tier TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_first_seen ON deals(first_seen)")
    for col, decl in [("orig_price", "REAL"), ("discount_pct", "INTEGER"),
                      ("category", "TEXT"), ("suppressed", "INTEGER DEFAULT 0"),
                      ("last_seen", "TEXT"), ("sold_out", "INTEGER DEFAULT 0"),
                      # Price-drop tracking: lowest price ever seen on this row,
                      # the price it showed before the latest drop, and when that
                      # drop was detected (the digest's second event kind).
                      ("low_price", "REAL"), ("prev_price", "REAL"),
                      ("price_dropped_at", "TEXT"),
                      # When Woot's API last said the offer was live; the row's
                      # baseline group and the corpus comparison the digest
                      # reports (median of the group, observations, implied %
                      # below that median).
                      ("last_verified", "TEXT"), ("product_key", "TEXT"),
                      ("baseline_median", "REAL"), ("baseline_n", "INTEGER"),
                      ("implied_pct", "INTEGER")]:
        try:
            db.execute(f"ALTER TABLE deals ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass  # column already exists
    db.execute("CREATE INDEX IF NOT EXISTS idx_product_key ON deals(product_key)")
    db.execute("UPDATE deals SET last_seen=first_seen WHERE last_seen IS NULL")
    db.execute("UPDATE deals SET low_price=price WHERE low_price IS NULL AND price IS NOT NULL")
    db.execute("""CREATE TABLE IF NOT EXISTS price_archive (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seen_date TEXT, part_type TEXT, spec TEXT, title TEXT,
        price REAL, orig_price REAL, discount_pct INTEGER,
        source TEXT, url TEXT)""")
    # Corpus-derived discount baseline: per product-group price stats so the
    # scorer can synthesize an implied discount for rows whose feed states none.
    db.execute("""CREATE TABLE IF NOT EXISTS price_baseline (
        product_key TEXT PRIMARY KEY,
        n_observed  INTEGER,
        median_price REAL,
        min_price   REAL,
        mad         REAL,
        last_updated TEXT)""")
    return db


def refresh_existing(db, item, wl, now_iso):
    """Re-sync an already-stored offer with what the feed says NOW.

    Every returned offer is rescored against the current watchlist (title,
    matches, tier, category, suppression all refreshed; first_seen never
    touched), so a repriced offer cannot keep a hot tier above its ceiling and
    a watchlist edit reaches rows the feeds keep returning.

    Returns None when the GUID is not stored yet (caller inserts it), else
    (tier, dropped): dropped is True when this poll detected a meaningful
    price drop (see PRICE_DROP_MIN_PCT), in which case prev_price and
    price_dropped_at are stamped so the digest can report it once.
    """
    row = db.execute(
        "SELECT price, low_price, product_key FROM deals WHERE guid=?",
        (item["guid"],)).fetchone()
    if row is None:
        return None
    old_price, low_price, old_pk = row
    sale, orig, pct = extract_pricing(item["title"], item["description"])
    floor_price = low_price if low_price is not None else old_price
    dropped = bool(sale and floor_price
                   and sale <= floor_price * (1 - PRICE_DROP_MIN_PCT / 100))
    new_low = min(p for p in (low_price, sale) if p is not None) if (sale or low_price) else None
    pk = product_key(item["title"], item.get("source", ""))
    if sale != old_price or pk != old_pk:
        # This row's current price is one of its group's observations. Store it
        # and recompute the group's median BEFORE scoring, so the tier and the
        # stored comparison both use the new figure. A retitled offer can move
        # to another group, so the group it left is recomputed too.
        db.execute("UPDATE deals SET price=?, product_key=? WHERE guid=?",
                   (sale, pk, item["guid"]))
        refresh_baseline_group(db, pk, now_iso[:10])
        if old_pk and old_pk != pk:
            refresh_baseline_group(db, old_pk, now_iso[:10])
    matched, tier, suppressed, category = score_item(item, wl)
    ctx = baseline_context(pk, sale) or (None, None, None)
    db.execute(
        "UPDATE deals SET last_seen=?, title=?, description=?, price=?, orig_price=?, "
        "discount_pct=?, matched=?, tier=?, category=?, suppressed=?, low_price=?, "
        "product_key=?, baseline_median=?, baseline_n=?, implied_pct=? WHERE guid=?",
        (now_iso, item["title"], item["description"], sale, orig, pct,
         ",".join(matched), tier, category, suppressed, new_low,
         pk, ctx[0], ctx[1], ctx[2], item["guid"]))
    if dropped:
        db.execute(
            "UPDATE deals SET prev_price=?, price_dropped_at=? WHERE guid=?",
            (old_price if old_price is not None else floor_price, now_iso, item["guid"]))
    return tier, dropped


def run():
    global SOURCES, SOURCE_FRESH_DAYS
    SOURCES = load_sources()  # stops with a pointer to sources.example.json when missing
    SOURCE_FRESH_DAYS = {s["name"]: s["fresh_days"] for s in SOURCES if s.get("fresh_days")}
    wl = load_watchlist()
    db = open_db()
    load_baseline(db)  # score_item consults the corpus baseline for no-discount deals
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    new_count, hot_new, suppressed_count, archived_count = 0, [], 0, 0
    rescored, hot_drops = 0, []

    for source in SOURCES:
        if not source.get("enabled", True):
            continue
        url = source.get("url")
        if source.get("url_file"):  # secret feed URL kept in a file, out of git
            url_path = BASE / source["url_file"]
            if not url_path.exists():
                log(f"SKIP {source['name']}: {source['url_file']} missing")
                continue
            url = url_path.read_text(encoding="utf-8").strip()
        try:
            if source["type"] == "shopify":
                items = parse_shopify(fetch(url), source["name"], url)
            elif source["type"] == "woot":
                items = parse_woot(
                    fetch(url, {"x-api-key": woot_key(source)}),
                    source["name"])
            elif source["type"] == "bradsdeals_html":
                items = parse_bradsdeals(fetch(url), source["name"], url)
            else:
                items = parse_feed(fetch(url), source["name"])
        except Exception as e:
            log(f"FETCH FAIL {source['name']}: {e}")
            continue
        # CCC's "no alerts yet" placeholder is metadata, not a deal.
        items = [i for i in items if CCC_PLACEHOLDER_URL not in (i["url"] or "")]

        fresh = 0
        for item in items:
            if not item["guid"] or not item["title"]:
                continue
            existing = refresh_existing(db, item, wl, now_iso)
            if existing is not None:
                # Already stored: fully rescored + drop-checked above.
                tier, dropped = existing
                rescored += 1
                if dropped and tier == "hot":
                    hot_drops.append(item["title"])
                continue
            haystack = f"{item['title']} {item['description']}"
            matched, tier, suppressed, category = score_item(item, wl)
            sale, orig, pct = extract_pricing(item["title"], item["description"])
            pub = item["published"].isoformat(timespec="seconds") if item["published"] else None
            pk = product_key(item["title"], item["source"])
            ctx = baseline_context(pk, sale) or (None, None, None)
            db.execute(
                "INSERT INTO deals (guid, source, title, url, description, price, published, "
                "first_seen, matched, tier, orig_price, discount_pct, category, suppressed, "
                "last_seen, low_price, product_key, baseline_median, baseline_n, implied_pct) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["guid"], item["source"], item["title"], item["url"],
                 item["description"], sale, pub, now_iso, ",".join(matched), tier,
                 orig, pct, category, suppressed, now_iso, sale, pk, ctx[0], ctx[1], ctx[2]))
            fresh += 1
            suppressed_count += suppressed
            if tier == "hot":
                hot_new.append(item["title"])
            # Computer-parts price archive: priced parts only, suppression-exempt
            # (archive is mechanical + invisible; never_show items are junk though).
            if sale is not None and category == "computers":
                part, spec = detect_part(haystack)
                if part:
                    db.execute(
                        "INSERT INTO price_archive (seen_date, part_type, spec, title, "
                        "price, orig_price, discount_pct, source, url) VALUES (?,?,?,?,?,?,?,?,?)",
                        (now_iso[:10], part, spec, item["title"], sale, orig, pct,
                         item["source"], item["url"]))
                    archived_count += 1
            # Incremental baseline update: exact median/MAD recomputed from the
            # group's rows. A group is created once it reaches 3 priced rows.
            if sale and sale > 0:
                refresh_baseline_group(db, pk, now_iso[:10])
        db.commit()
        new_count += fresh
        log(f"OK {source['name']}: {len(items)} items, {fresh} new")

    woot_src = next((s for s in SOURCES if s["type"] == "woot" and s.get("enabled", True)), None)
    if woot_src:
        try:
            refresh_woot_availability(db, woot_src)
        except Exception as e:
            log(f"WOOT AVAILABILITY: skipped ({e})")

    write_digest(db, now)
    write_dashboard(db, now)
    db.close()
    log(f"RUN DONE: {new_count} new ({suppressed_count} suppressed, "
        f"{archived_count} part-prices archived), {rescored} repeat offers rescored" +
        (f", {len(hot_new)} HOT: {'; '.join(hot_new[:5])}" if hot_new else "") +
        (f", {len(hot_drops)} HOT PRICE DROP: {'; '.join(hot_drops[:5])}" if hot_drops else ""))


def rebuild():
    """Rescore stored deals, re-extract pricing, and regenerate the dashboard
    without fetching sources. Also rebuilds price_archive from the deals table
    (deals is the source of truth; archive rows are derived), so a --rebuild
    followed by --consolidate regenerates price_history from corrected prices."""
    wl = load_watchlist()
    db = open_db()
    rows = db.execute(
        "SELECT guid, title, description, first_seen, source, url FROM deals").fetchall()
    now = datetime.now(timezone.utc)

    # Pass 1: rebuild price_baseline from all priced deals (exact medians), then
    # load it so pass 2 scores against fresh baselines in this same rebuild.
    priced = []
    for guid, title, description, first_seen, source, url in rows:
        sale, _orig, _pct = extract_pricing(title or "", description or "")
        priced.append((product_key(title, source), sale))
    write_baseline(db, build_baseline_groups(priced), now.isoformat(timespec="seconds")[:10])
    db.commit()
    load_baseline(db)

    # Pass 2: rescore stored deals + rebuild price_archive from the deals table.
    db.execute("DELETE FROM price_archive")
    archived = 0
    for guid, title, description, first_seen, source, url in rows:
        item = {"title": title or "", "description": description or "", "source": source or ""}
        matched, tier, suppressed, category = score_item(item, wl)
        sale, orig, pct = extract_pricing(item["title"], item["description"])
        pk = product_key(title, source)
        ctx = baseline_context(pk, sale) or (None, None, None)
        db.execute(
            "UPDATE deals SET matched=?, tier=?, category=?, suppressed=?, "
            "price=?, orig_price=?, discount_pct=?, "
            "low_price=MIN(COALESCE(low_price, ?), COALESCE(?, low_price)), "
            "product_key=?, baseline_median=?, baseline_n=?, implied_pct=? WHERE guid=?",
            (",".join(matched), tier, category, suppressed, sale, orig, pct, sale, sale,
             pk, ctx[0], ctx[1], ctx[2], guid))
        if sale is not None and category == "computers":
            part, spec = detect_part(f"{item['title']} {item['description']}")
            if part:
                db.execute(
                    "INSERT INTO price_archive (seen_date, part_type, spec, title, "
                    "price, orig_price, discount_pct, source, url) VALUES (?,?,?,?,?,?,?,?,?)",
                    ((first_seen or "")[:10], part, spec, title, sale, orig, pct, source, url))
                archived += 1
    db.commit()
    write_digest(db, now)
    write_dashboard(db, now)
    db.close()
    log(f"REBUILD DONE: rescored {len(rows)} stored deals, re-extracted pricing, "
        f"rebuilt price_archive ({archived} part-price rows) without fetching sources")


def rescore():
    """--rescore: apply a watchlist / category-rule change to the stored corpus
    WITHOUT re-extracting prices, rebuilding price_archive, or rebuilding
    baselines. Updates matched, tier, category,
    suppressed and the stored corpus comparison, then renders both outputs.
    Use --rebuild only when pricing or baseline logic changed."""
    wl = load_watchlist()
    db = open_db()
    load_baseline(db)
    now = datetime.now(timezone.utc)
    rows = db.execute(
        "SELECT guid, title, description, source, price, product_key FROM deals").fetchall()
    for guid, title, description, source, price, pk in rows:
        item = {"title": title or "", "description": description or "", "source": source or ""}
        matched, tier, suppressed, category = score_item(item, wl)
        ctx = baseline_context(pk, price) or (None, None, None)
        db.execute(
            "UPDATE deals SET matched=?, tier=?, category=?, suppressed=?, "
            "baseline_median=?, baseline_n=?, implied_pct=? WHERE guid=?",
            (",".join(matched), tier, category, suppressed, ctx[0], ctx[1], ctx[2], guid))
    db.commit()
    write_digest(db, now)
    write_dashboard(db, now)
    db.close()
    log(f"RESCORE DONE: rescored {len(rows)} stored deals against the current "
        f"watchlist (pricing, price_archive and baselines untouched)")


def write_digest(db, now):
    """digest_feed.json -- the file a downstream notifier reads.

    Also carries needs_eval: deals >$50 or >=20% off, flagged for a closer
    look (is this discount rare? wait or buy now?).
    """
    cutoff = (now - timedelta(hours=DIGEST_WINDOW_HOURS)).isoformat(timespec="seconds")
    # Two event kinds share the window: a deal first discovered inside it
    # ("new"), or an older deal whose price just dropped meaningfully inside it
    # ("price_drop"). event_at is the timestamp the digest filters on.
    rows = db.execute(
        "SELECT guid, source, title, url, price, orig_price, discount_pct, tier, matched, "
        "first_seen, category, price_dropped_at, prev_price, last_seen, "
        "baseline_median, baseline_n, implied_pct FROM deals "
        "WHERE matched != '' AND suppressed=0 AND sold_out=0 "
        "AND (first_seen >= ? OR price_dropped_at >= ?) "
        "ORDER BY tier DESC, MAX(first_seen, COALESCE(price_dropped_at, '')) DESC",
        (cutoff, cutoff)).fetchall()
    matches = []
    for r in rows:
        is_new = r[9] >= cutoff
        # discount_source tells the digest WHICH kind of number it holds:
        # "feed" = the source stated a list price / % off; "corpus" = only the
        # watcher's own comparison against similar listings exists (never a
        # list-price discount); "none" = nothing to compare against.
        discount_source = "feed" if r[6] is not None else ("corpus" if r[16] is not None else "none")
        matches.append(
            {"guid": r[0], "source": r[1], "title": r[2], "url": r[3], "price": r[4],
             "orig_price": r[5], "discount_pct": r[6], "tier": r[7],
             "matched": r[8].split(","), "first_seen": r[9], "category": r[10],
             "event": "new" if is_new else "price_drop",
             "event_at": r[9] if is_new else r[11],
             "price_dropped_at": r[11], "prev_price": r[12], "last_seen": r[13],
             "discount_source": discount_source,
             "corpus_median_price": r[14], "corpus_observations": r[15],
             "corpus_implied_pct": r[16],
             "needs_eval": bool((r[4] and r[4] > 50) or (r[6] and r[6] >= 20)
                                or (r[16] and r[16] >= 20))})
    payload = {
        "generated": now.isoformat(timespec="seconds"),
        "window_hours": DIGEST_WINDOW_HOURS,
        "matches": matches,
    }
    DIGEST_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


CATEGORY_LABELS = {
    "computers": "Computers & parts",
    "electronics": "Electronics",
    "camping-outdoors": "Camping & outdoors",
    "home": "Home & smart home",
    "clothing": "Clothing",
    "other": "Everything else",
}


def matched_only_categories():
    """The watchlist's matched_only_categories, read without compiling the
    whole watchlist (the dashboard-only path never loads it)."""
    if not WATCHLIST_FILE.exists():
        return []
    return list(json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
                .get("matched_only_categories", []))

# Short names for the one-tap category buttons on the dashboard (the long
# CATEGORY_LABELS strings are section headings, too wide for a chip row).
CATEGORY_CHIPS = {
    "": "All",
    "electronics": "Electronics",
    "computers": "PC parts",
    "home": "Home",
    "camping-outdoors": "Camping",
    "clothing": "Clothing",
    "other": "Everything else",
}


# Woot availability. The feed is NOT a complete live catalog: a live offer can
# be missing from it entirely while its page is still selling. So absence
# proves nothing and is NOT used. The real signal is the
# developer.woot.com/getoffers endpoint, which returns IsSoldOut per offer id.
WOOT_OFFERS_URL = "https://developer.woot.com/getoffers"
WOOT_BATCH = 25          # documented maximum offer ids per request
WOOT_RECHECK_LIMIT = 500  # newest displayable rows per poll (20 requests, 1/s)


def post_json(url, payload, extra_headers=None):
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return json.loads(resp.read())


def refresh_woot_availability(db, source):
    """Mark sold-out Woot offers so the dashboard stops linking to dead deals.

    Only the newest displayable rows are re-checked (a poll costs ~20 requests
    against a 1,000/day budget). Rows already marked sold out are never
    re-checked -- Woot offers do not come back.
    """
    key = woot_key(source)
    if not key:
        return 0
    now = datetime.now(timezone.utc)
    verified_iso = now.isoformat(timespec="seconds")
    # Check exactly what the dashboard will SHOW, using the dashboard's own
    # section definitions (section_query): hot + watch matches first (the rows
    # most likely to be clicked), then each category section, then the "older
    # listings" view so a stale-but-live Woot offer gets its last_verified
    # stamp and stays on the main page. Checking "the newest N overall"
    # instead misses old matches that are still on the page.
    seen, ids = set(), []
    queries = [section_query(k, now, "guid", source=source["name"]) for k in ("hot", "watch")]
    queries += [section_query("category", now, "guid", cat=cat, source=source["name"])
                for cat in CATEGORY_LABELS]
    queries += [section_query(k, now, "guid", fresh=False, source=source["name"])
                for k in ("hot", "watch")]
    for sql, params in queries:
        for (guid,) in db.execute(sql, params):
            if guid not in seen and len(ids) < WOOT_RECHECK_LIMIT:
                seen.add(guid)
                ids.append(guid.replace("woot-", ""))
    marked = 0
    for i in range(0, len(ids), WOOT_BATCH):
        batch = ids[i:i + WOOT_BATCH]
        try:
            offers = post_json(WOOT_OFFERS_URL, batch, {"x-api-key": key})
        except Exception as e:
            log(f"WOOT AVAILABILITY: batch failed ({e}) -- keeping prior state")
            break
        for offer in offers:
            # getoffers names the offer id "Id"; the feed calls it "OfferId".
            oid = offer.get("Id") or offer.get("OfferId")
            if offer.get("IsSoldOut"):
                cur = db.execute("UPDATE deals SET sold_out=1 WHERE guid=?", (f"woot-{oid}",))
                marked += cur.rowcount
            else:
                # Confirmed live by the API: counts as fresh even when the offer
                # has rotated out of the feed.
                db.execute("UPDATE deals SET last_verified=? WHERE guid=?",
                           (verified_iso, f"woot-{oid}"))
        time.sleep(1.1)  # documented limit: 1 request/second
    db.commit()
    log(f"WOOT AVAILABILITY: checked {len(ids)} offers, {marked} newly sold out")
    return marked


SECTION_LIMITS = {"hot": 50, "watch": 100, "matched_only": 40, "category": 60, "older": 100}


def fresh_sql(now):
    """SQL predicate + params: the row is still current per its source's
    freshness window (SOURCE_FRESH_DAYS, else FRESH_DAYS_DEFAULT), judged on
    the later of last_seen (still in the feed) and last_verified (Woot API)."""
    latest = "MAX(last_seen, COALESCE(last_verified, ''))"
    default_cut = (now - timedelta(days=FRESH_DAYS_DEFAULT)).isoformat(timespec="seconds")
    if not SOURCE_FRESH_DAYS:  # a CASE with no WHEN branch is a syntax error
        return f"{latest} >= ?", [default_cut]
    case = "CASE source " + " ".join("WHEN ? THEN ?" for _ in SOURCE_FRESH_DAYS) + " ELSE ? END"
    params = []
    for name, days in SOURCE_FRESH_DAYS.items():
        params += [name, (now - timedelta(days=days)).isoformat(timespec="seconds")]
    params.append((now - timedelta(days=FRESH_DAYS_DEFAULT)).isoformat(timespec="seconds"))
    return f"MAX(last_seen, COALESCE(last_verified, '')) >= ({case})", params


def section_query(kind, now, cols, cat=None, fresh=True, source=None):
    """THE definition of what each dashboard section renders, as (sql, params).

    kind: "hot" / "watch" (tier sections), or "category" with cat= (a
    matched_only_categories section shows watchlist matches only). fresh=False
    selects the same rows that fell OUT of the freshness window (the "older
    listings" view). Shared by write_dashboard, tuning_stats and
    refresh_woot_availability so telemetry and availability checks describe
    the page that actually renders.
    """
    fsql, fparams = fresh_sql(now)
    where, params = ["suppressed=0", "sold_out=0"], []
    if kind in ("hot", "watch"):
        where.append("tier=?"); params.append(kind); limit = SECTION_LIMITS[kind]
    elif cat in matched_only_categories():
        where.append("category=? AND matched != ''"); params.append(cat)
        limit = SECTION_LIMITS["matched_only"]
    else:
        where.append("category=?"); params.append(cat); limit = SECTION_LIMITS["category"]
    if source:
        where.append("source=?"); params.append(source)
    where.append(fsql if fresh else f"NOT ({fsql})"); params += fparams
    if not fresh:
        limit = SECTION_LIMITS["older"]
    return (f"SELECT {cols} FROM deals WHERE {' AND '.join(where)} "
            f"ORDER BY first_seen DESC LIMIT {limit}", params)


def write_dashboard(db, now):
    def deal_cell(title, url, price, orig, pct, matched, show_matched):
        price_s = ""
        if price is not None:
            price_s = f"${price:,.2f}"
            if pct:
                save = f", save ${orig - price:,.0f}" if orig and orig > price else ""
                price_s += f' <span class="disc">(-{pct}%{save})</span>'
        match_s = f' <span class="tag">{escape(matched)}</span>' if show_matched and matched else ""
        return price_s, match_s

    def age_label(last_seen, last_verified):
        latest = max(last_seen or "", last_verified or "")
        if not latest:
            return ""
        try:
            days = (now - datetime.fromisoformat(latest)).days
        except ValueError:
            return ""
        how = "verified" if (last_verified or "") >= (last_seen or "") else "seen"
        return f"{how} today" if days < 1 else f"{how} {days}d ago"

    def rows_html(rows, show_matched=False):
        out = []
        for (source, title, url, price, orig, pct, first_seen, matched, category,
             last_seen, last_verified) in rows:
            price_s, match_s = deal_cell(title, url, price, orig, pct, matched, show_matched)
            out.append(
                f'<tr data-deal="1" data-category="{escape(category or "other")}" '
                f'data-source="{escape(source.lower())}" data-title="{escape(title.lower())}" '
                f'data-price="{price if price is not None else ""}" '
                f'data-discount="{pct if pct is not None else "0"}" '
                f'data-matched="{1 if matched else 0}"><td class="src">{escape(source)}</td>'
                f'<td><a href="{escape(url)}" target="_blank">{escape(title)}</a>{match_s}</td>'
                f'<td class="price">{price_s}</td>'
                f'<td class="time">{escape((first_seen or "")[:16].replace("T", " "))}'
                f'<br><span class="age">{escape(age_label(last_seen, last_verified))}</span></td></tr>')
        return "\n".join(out) or '<tr><td colspan="4" class="empty">nothing yet</td></tr>'

    cols = ("source, title, url, price, orig_price, discount_pct, first_seen, matched, category, "
            "last_seen, last_verified")
    hot = db.execute(*section_query("hot", now, cols)).fetchall()
    watch = db.execute(*section_query("watch", now, cols)).fetchall()
    # Older listings: matches whose source stopped listing them (and, for
    # Woot, the API has not re-confirmed them) inside the freshness window.
    # Feed absence is not proof of expiry, so they are shown apart, not hidden.
    older = (db.execute(*section_query("hot", now, cols, fresh=False)).fetchall()
             + db.execute(*section_query("watch", now, cols, fresh=False)).fetchall())

    cat_sections = []
    for cat, label in CATEGORY_LABELS.items():
        rows = db.execute(*section_query("category", now, cols, cat=cat)).fetchall()
        cat_sections.append(
            f'<section><h2>{escape(label)}</h2><table>{rows_html(rows, True)}</table></section>')

    total = db.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
    n_suppressed = db.execute("SELECT COUNT(*) FROM deals WHERE suppressed=1").fetchone()[0]
    n_archive = db.execute("SELECT COUNT(*) FROM price_archive").fetchone()[0]

    cat_chips = "".join(
        f'<button type="button" class="chip{" on" if not cat else ""}" '
        f'data-cat="{cat}">{escape(label)}</button>'
        for cat, label in CATEGORY_CHIPS.items())

    html_doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Deal Feed Watcher</title>
<style>
:root {{ color-scheme: dark; }}
body {{ font: 14px/1.45 system-ui, sans-serif; background:#14161a; color:#d7dae0; margin:0; padding:1.2rem; }}
h1 {{ font-size:1.25rem; margin:0 0 .2rem; }}
h2 {{ font-size:1rem; margin:1.4rem 0 .4rem; border-bottom:1px solid #2a2e35; padding-bottom:.25rem; }}
.meta {{ color:#8b919c; font-size:.8rem; }}
.filters {{ display:flex; flex-wrap:wrap; gap:.5rem; align-items:center; margin:1rem 0; }}
.filters input, .filters select, .filters button {{ background:#20242b; color:#d7dae0; border:1px solid #3a414c; border-radius:4px; padding:.4rem .5rem; font:inherit; }}
.filters input[type=number] {{ width:6rem; }}
.filters button {{ cursor:pointer; }}
.cats {{ display:flex; flex-wrap:wrap; gap:.4rem; margin:1rem 0 .4rem; }}
.chip {{ background:#20242b; color:#c3c8d1; border:1px solid #3a414c; border-radius:999px;
  padding:.45rem .85rem; font:inherit; cursor:pointer; }}
.chip:hover {{ border-color:#5a6473; }}
.chip.on {{ background:#2f6fb5; border-color:#2f6fb5; color:#fff; }}
.count {{ color:#8b919c; font-size:.8rem; margin:.2rem 0 .6rem; }}
table {{ border-collapse:collapse; width:100%; }}
td {{ padding:.3rem .5rem; border-bottom:1px solid #22262c; vertical-align:top; }}
a {{ color:#7ab7ff; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
.src {{ color:#8b919c; white-space:nowrap; width:1%; }}
.price {{ white-space:nowrap; text-align:right; color:#9fd89f; width:1%; }}
.disc {{ color:#ffcf87; }}
.time {{ white-space:nowrap; color:#6b717c; font-size:.78rem; width:1%; }}
.tag {{ background:#2a3442; color:#9cc4ee; border-radius:3px; padding:0 .35rem; font-size:.72rem; white-space:nowrap; }}
.hot h2 {{ color:#ff9d76; border-color:#4a3226; }}
.age {{ color:#8b919c; font-size:.72rem; }}
.older h2 {{ color:#8b919c; }} .older a {{ color:#7f9bbf; }}
.empty {{ color:#6b717c; font-style:italic; }}
/* The mobile block below sets display on rows/tables, which outranks the
   browser's default [hidden] rule -- without this the filters visibly do
   nothing on a phone. */
[hidden] {{ display:none !important; }}
@media (max-width: 600px) {{
  body {{ padding:.8rem; font-size:16px; }}
  .filters {{ gap:.4rem; }} .filters input[type=search] {{ flex:1 1 100%; min-width:0; }}
  table, tbody {{ display:block; }} tr[data-deal] {{ display:grid; grid-template-columns:minmax(0,1fr) auto; padding:.65rem 0; border-bottom:1px solid #22262c; }}
  tr[data-deal] td {{ border:0; padding:0; }}
  tr[data-deal] .src {{ grid-column:1; grid-row:1; width:auto; font-size:.8rem; }}
  tr[data-deal] td:nth-child(2) {{ grid-column:1 / 3; grid-row:2; padding:.15rem 0; min-width:0; }}
  tr[data-deal] .price {{ grid-column:2; grid-row:1; text-align:right; }}
  tr[data-deal] .time {{ grid-column:2; grid-row:3; text-align:right; font-size:.72rem; }}
}}
</style></head><body>
<h1>Deal Feed Watcher</h1>
<div class="meta">Updated {now.strftime('%Y-%m-%d %H:%M UTC')} &middot; {total} deals tracked
&middot; {n_suppressed} suppressed (never-show / hidden-for-now) &middot; {n_archive} part-prices archived
&middot; config: watchlist.json</div>
<div class="cats" id="cats" role="group" aria-label="Filter by category">{cat_chips}</div>
<div class="count" id="count"></div>
<form class="filters" id="filters">
  <input id="query" type="search" placeholder="Search deals" aria-label="Search deals">
  <input id="max-price" type="number" min="0" step="1" placeholder="Max price" aria-label="Maximum price">
  <input id="min-discount" type="number" min="0" max="100" step="1" placeholder="Min % off" aria-label="Minimum discount">
  <label><input id="matched-only" type="checkbox"> Matches only</label>
  <button type="reset">Clear</button>
</form>
<section class="hot"><h2>&#128293; Hot matches</h2><table>{rows_html(hot, True)}</table></section>
<section><h2>Watch matches</h2><table>{rows_html(watch, True)}</table></section>
{''.join(cat_sections)}
<section class="older"><h2>Older listings (matches not seen in their feed for {FRESH_DAYS_DEFAULT}+ days -- may still be live)</h2><table>{rows_html(older, True)}</table></section>
<script>
(() => {{
  const form = document.querySelector('#filters');
  const query = document.querySelector('#query');
  const maxPrice = document.querySelector('#max-price'), minDiscount = document.querySelector('#min-discount');
  const matchedOnly = document.querySelector('#matched-only');
  const chips = [...document.querySelectorAll('#cats .chip')];
  const count = document.querySelector('#count');
  let cat = '';
  function applyFilters() {{
    const text = query.value.trim().toLowerCase(), max = Number(maxPrice.value), min = Number(minDiscount.value);
    let shown = 0;
    document.querySelectorAll('tr[data-deal]').forEach(row => {{
      const search = `${{row.dataset.title}} ${{row.dataset.source}}`;
      const visible = (!text || search.includes(text)) && (!cat || row.dataset.category === cat)
        && (!maxPrice.value || (row.dataset.price && Number(row.dataset.price) <= max))
        && (!minDiscount.value || Number(row.dataset.discount) >= min)
        && (!matchedOnly.checked || row.dataset.matched === '1');
      row.hidden = !visible;
      if (visible) shown++;
    }});
    // Collapse sections that ended up empty so a category view is just its deals.
    document.querySelectorAll('section').forEach(sec => {{
      sec.hidden = ![...sec.querySelectorAll('tr[data-deal]')].some(r => !r.hidden);
    }});
    const label = cat ? (chips.find(c => c.dataset.cat === cat) || {{}}).textContent : '';
    count.textContent = shown + ' deal' + (shown === 1 ? '' : 's') + ' shown' + (label ? ' in ' + label : '');
  }}
  chips.forEach(chip => chip.addEventListener('click', () => {{
    cat = chip.dataset.cat;
    chips.forEach(c => c.classList.toggle('on', c === chip));
    applyFilters();
  }}));
  form.addEventListener('input', applyFilters); form.addEventListener('change', applyFilters);
  form.addEventListener('reset', () => setTimeout(() => {{
    cat = '';
    chips.forEach(c => c.classList.toggle('on', c.dataset.cat === ''));
    applyFilters();
  }}));
  applyFilters();
}})();
</script>
</body></html>
"""
    DASHBOARD_FILE.write_text(html_doc, encoding="utf-8")


TUNING_STATS_FILE = BASE / "tuning_stats.json"


def tuning_stats():
    """--tuning-stats: write tuning_stats.json for a periodic watchlist
    review (by you or an agent that only proposes edits). Zero-AI telemetry: where
    rows come from, which watchlist entries still earn their place, what each
    suppression pattern eats, and the shown-but-unmatched noise frontier."""
    TOP_N = 8
    wl_raw = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    db = open_db()
    now = datetime.now(timezone.utc)
    cut7 = (now - timedelta(days=7)).isoformat(timespec="seconds")
    cut30 = (now - timedelta(days=30)).isoformat(timespec="seconds")

    per_source = {}
    for name, n7, n30 in db.execute(
            "SELECT source, SUM(first_seen >= ?), SUM(first_seen >= ?) "
            "FROM deals GROUP BY source ORDER BY source", (cut7, cut30)):
        per_source[name] = {"new_7d": n7 or 0, "new_30d": n30 or 0}

    entries = {}
    for e in wl_raw.get("entries", []):
        eid = e["id"]
        row = db.execute(
            "SELECT COUNT(*), MAX(first_seen) FROM deals "
            "WHERE (','||matched||',') LIKE ?", (f"%,{eid},%",)).fetchone()
        hits30 = db.execute(
            "SELECT COUNT(*) FROM deals WHERE (','||matched||',') LIKE ? "
            "AND first_seen >= ?", (f"%,{eid},%", cut30)).fetchone()[0]
        # tier/patterns/note travel with the stats so a review judges against
        # the entry's INTENT, not just its hit counts (without them a designed
        # broad match reads as a false positive and a new entry as stale).
        entries[eid] = {"hits_total": row[0], "hits_30d": hits30,
                        "last_hit": row[1], "tier": e.get("tier", "watch"),
                        "patterns": e.get("patterns", []),
                        "exclude_patterns": e.get("exclude_patterns", []),
                        "needs_detail": e.get("needs_detail", False),
                        "note": e.get("note", "")}
    dead = sorted(eid for eid, s in entries.items() if s["hits_30d"] == 0)

    # What each suppression pattern is eating (30d window): proves the pattern
    # still earns its place, and surfaces overreach.
    supp_rows = db.execute(
        "SELECT title, description FROM deals WHERE suppressed=1 "
        "AND first_seen >= ?", (cut30,)).fetchall()
    def pattern_samples(patterns):
        out = {}
        for pat in patterns:
            rx = re.compile(pat, re.IGNORECASE)
            hits = [t for t, d in supp_rows if rx.search(f"{t} {d or ''}")]
            out[pat] = {"suppressed_30d": len(hits), "sample_titles": hits[:TOP_N]}
        return out
    suppression = {"never_show": pattern_samples(wl_raw.get("never_show", [])),
                   "hidden_for_now": pattern_samples(wl_raw.get("hidden_for_now", []))}

    # The noise frontier: rows ACTUALLY RENDERED on the page that match
    # nothing -- taken from the dashboard's own section queries, so a category
    # the page never shows unmatched reports zero here instead of phantom
    # "shown" rows. The stored-but-not-rendered unmatched rows are reported
    # apart as archive candidates.
    unmatched, archive_candidates = {}, {}
    for cat in CATEGORY_LABELS:
        rendered = [t for t, m in db.execute(
            *section_query("category", now, "title, matched", cat=cat)) if not m]
        if rendered:
            unmatched[cat] = {"rendered_unmatched": len(rendered),
                              "sample_titles": rendered[:TOP_N]}
    for cat, in db.execute("SELECT DISTINCT category FROM deals"):
        n = db.execute(
            "SELECT COUNT(*) FROM deals WHERE category=? AND suppressed=0 "
            "AND sold_out=0 AND matched='' AND first_seen >= ?",
            (cat, cut30)).fetchone()[0]
        shown = unmatched.get(cat, {}).get("rendered_unmatched", 0)
        if n - shown > 0:
            archive_candidates[cat or "other"] = n - shown

    payload = {
        "generated": now.isoformat(timespec="seconds"),
        "windows": {"short_days": 7, "long_days": 30},
        "per_source_new_rows": per_source,
        "watchlist_entries": entries,
        "dead_entries_30d": dead,
        "suppression_patterns": suppression,
        "shown_but_unmatched": unmatched,
        # Unmatched rows stored in the last 30 days that the page does NOT
        # render (matched-only categories without a match, rows past the section limits or
        # the freshness window). Not evidence of on-page noise.
        "stored_unmatched_not_rendered_30d": archive_candidates,
    }
    TUNING_STATS_FILE.write_text(json.dumps(payload, indent=2) + "\n",
                                 encoding="utf-8")
    db.close()
    log(f"TUNING STATS: {len(per_source)} sources, {len(entries)} entries "
        f"({len(dead)} dead 30d), written to {TUNING_STATS_FILE.name}")


def consolidate():
    """Monthly: fold price_archive rows from BEFORE the current month into
    price_history.json (per part_type+spec: count/min/median/avg discount),
    then delete the raw rows. Keeps the DB small while preserving the
    price-over-time comparison for future part purchases."""
    db = open_db()
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    rows = db.execute(
        "SELECT substr(seen_date,1,7) m, part_type, spec, price, discount_pct "
        "FROM price_archive WHERE substr(seen_date,1,7) < ?", (this_month,)).fetchall()
    if not rows:
        log("CONSOLIDATE: nothing older than the current month.")
        db.close()
        return

    history = {}
    if PRICE_HISTORY_FILE.exists():
        history = json.loads(PRICE_HISTORY_FILE.read_text(encoding="utf-8"))
    buckets = {}
    for month, part, spec, price, pct in rows:
        buckets.setdefault((month, part, spec or "unspecified"), []).append((price, pct))
    for (month, part, spec), vals in sorted(buckets.items()):
        # A month already folded in keeps its saved figures: rows for it can
        # only reappear through --rebuild, which re-derives them from the deals
        # still stored and would otherwise replace fuller history with less.
        if spec in history.get(month, {}).get(part, {}):
            continue
        prices = [v[0] for v in vals]
        pcts = [v[1] for v in vals if v[1] is not None]
        history.setdefault(month, {}).setdefault(part, {})[spec] = {
            "deals": len(prices),
            "min_price": min(prices),
            "median_price": round(statistics.median(prices), 2),
            "avg_discount_pct": round(sum(pcts) / len(pcts)) if pcts else None,
        }
    PRICE_HISTORY_FILE.write_text(json.dumps(history, indent=2, sort_keys=True) + "\n",
                                  encoding="utf-8")
    db.execute("DELETE FROM price_archive WHERE substr(seen_date,1,7) < ?", (this_month,))
    db.commit()
    db.close()
    log(f"CONSOLIDATE: folded {len(rows)} raw rows into price_history.json "
        f"({len(buckets)} part/spec/month buckets), raw rows purged.")


if __name__ == "__main__":
    try:
        if "--consolidate" in sys.argv:
            consolidate()
        elif "--tuning-stats" in sys.argv:
            tuning_stats()
        elif "--dashboard" in sys.argv:
            _db = open_db()
            write_dashboard(_db, datetime.now(timezone.utc))
            _db.close()
            log("DASHBOARD: regenerated from the existing database (no fetch).")
        elif "--rebuild" in sys.argv:
            rebuild()
        elif "--rescore" in sys.argv:
            rescore()
        else:
            run()
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
