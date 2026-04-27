"""
MasterBot — dilution tracker for Discord
Polygon.io (Massive plan) + SEC EDGAR

Triggered by $TICKER mentions. Reports:
  - Active ATM (S-3 + 424B5 + 8-K + sales agreement)
  - Live S-3 shelf (with EFFECT verification)
  - PIPE / Registered Direct (with dollar size)
  - Recent 8-K offerings
  - Warrants outstanding (with negation guard)
  - Reverse splits
  - Majority ownership (>50%) from 13D/G "Item 5" or DEF 14A beneficial-ownership tables
  - China HQ flag
  - Float / SI / DTC / short-vol-ratio
  - Ticker history (prior tickers)
"""

import os, re, asyncio, aiohttp, discord
from datetime import datetime, timedelta
from html import unescape

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
POLYGON_KEY   = "0NCwDp9jz7LfO6C0y0y4tKPRLvmH5mX1"
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID      = 1497766198904094770
GENERAL_CHANNEL_ID = 1497766199713464502  # only respond to ticker mentions in #general
TICKER_RE     = re.compile(r'\$([A-Za-z]{1,5})\b')
SEC_HEADERS   = {"User-Agent": "DilutionBot jh.hockey77@gmail.com",
                 "Accept-Encoding": "gzip, deflate"}
CHINA_CODES   = {"cn", "hk"}
CHINA_DESC_RE = re.compile(
    r"\b(?:PRC|People'?s\s+Republic\s+of\s+China|mainland\s+China|Hong\s+Kong|"
    r"China[\s\-]based|based\s+in\s+China|incorporated\s+in\s+(?:the\s+)?Cayman\s+Islands.*?(?:operations?|subsidiar))\b",
    re.I)

# SEC effectiveness window for an S-3 shelf
SHELF_WINDOW_DAYS = 3 * 365 + 30   # 3 years + small grace

# ─────────────────────────────────────────────────────────────────────────────
# Form-name normalisation — EDGAR returns BOTH "SC 13D" and "SCHEDULE 13D"
# ─────────────────────────────────────────────────────────────────────────────
def _form_set(*names):
    """Expand short and long names: SC 13D <-> SCHEDULE 13D, with /A variants."""
    out = set()
    for n in names:
        out.add(n)
        if n.startswith("SC "):
            out.add("SCHEDULE " + n[3:])
        elif n.startswith("SCHEDULE "):
            out.add("SC " + n[9:])
    return out

ATM_PROSPECTUS_FORMS = _form_set("424B5", "424B3", "424B4", "S-3", "S-3/A", "S-3ASR", "POS AM")
SHELF_FORMS          = _form_set("S-3", "S-3/A", "S-3ASR")
PIPE_FORMS           = _form_set("424B3", "424B4", "424B5", "S-1", "S-1/A", "POS AM")
OFFERING_8K_FORMS    = _form_set("8-K", "8-K/A")
WARRANT_FORMS        = _form_set("424B3", "424B4", "424B5", "S-1", "S-1/A")
SPLIT_FORMS          = _form_set("8-K", "8-K/A", "DEF 14A", "DEF 14C", "PRE 14A", "PRE 14C")
OWNERSHIP_FORMS      = _form_set("SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "DEF 14A")
EFFECT_FORMS         = {"EFFECT"}

# ─────────────────────────────────────────────────────────────────────────────
# Pattern banks
# ─────────────────────────────────────────────────────────────────────────────
ATM_PATTERNS = [
    re.compile(r'at[\s\-]the[\s\-]market(?:\s+offering)?', re.I),
    re.compile(r'\batm\s+(?:offering|program|sales)\b', re.I),
    re.compile(r'sales\s+agreement', re.I),
    re.compile(r'equity\s+distribution\s+agreement', re.I),
    re.compile(r'controlled\s+equity\s+offering', re.I),
    re.compile(r'open\s+market\s+sale\s+agreement', re.I),
]
ATM_TERMINATED_PATTERNS = [
    re.compile(r'(?:terminated|expired|completed)\s+(?:the\s+)?(?:sales\s+agreement|atm|at[\s\-]the[\s\-]market)', re.I),
    re.compile(r'(?:sales\s+agreement|atm)\s+(?:has\s+been\s+|was\s+)?(?:terminated|expired)', re.I),
    re.compile(r'no\s+longer\s+(?:in\s+effect|active)', re.I),
]

PIPE_PATTERNS = [
    re.compile(r'\bPIPE\b'),
    re.compile(r'private\s+investment\s+in\s+public\s+equity', re.I),
    re.compile(r'registered\s+direct\s+offering', re.I),
    re.compile(r'private\s+placement\s+(?:of|with|offering)', re.I),
    re.compile(r'securities\s+purchase\s+agreement', re.I),
]
# Anti-pattern: skip risk-factor / forward-looking language only
PIPE_RISK_FACTOR_RE = re.compile(
    r'(?:may|might|could|future)\s+(?:engage\s+in|conduct|complete|undertake)\s+(?:additional\s+)?'
    r'(?:private\s+placements?|registered\s+direct|PIPE)', re.I)

OFFERING_8K_PATTERNS = [
    re.compile(r'(?:underwritten|public|registered\s+direct|best[\s\-]efforts)\s+offering', re.I),
    re.compile(r'\bPIPE\b'),
    re.compile(r'private\s+placement\s+(?:of|with)', re.I),
    re.compile(r'securities\s+purchase\s+agreement', re.I),
    re.compile(r'(?:closed|completed|priced)\s+(?:its|the|an?)\s+(?:public\s+)?offering', re.I),
    re.compile(r'sales\s+agreement', re.I),
]

WARRANT_PATTERNS = [
    re.compile(r'(?:pre[\s\-]funded|common|series\s+\w+)\s+warrant[s]?\b', re.I),
    re.compile(r'warrant[s]?\s+to\s+purchase\s+(?:up\s+to\s+)?(?:[\d,]+|an\s+aggregate)', re.I),
    re.compile(r'([\d,]+(?:\.\d+)?)\s+warrant[s]?\s+(?:exercisable|outstanding)', re.I),
]

# Patterns to extract warrant SHARE COUNT (how many underlying shares).
# Each match must capture the share number in group 1. We'll sum across
# multiple matches to handle multi-tranche offerings (Series A + B + Pre-Funded).
WARRANT_SHARES_PATTERNS = [
    # "107,142,857 Class A Shares Underlying the Series A Warrants"
    re.compile(r'([\d,]{4,})\s+(?:Class\s+\w+\s+|Common\s+|Ordinary\s+)?[Ss]hares\s+(?:[Uu]nderlying|[Ii]ssuable\s+upon\s+(?:the\s+)?exercise\s+of)\s+(?:the\s+)?(?:Series\s+\w+\s+|Pre[\s\-]?[Ff]unded\s+|Common\s+)?[Ww]arrant', re.I),
    # "warrants to purchase up to 1,234,567 shares"
    re.compile(r'[Ww]arrant[s]?\s+to\s+purchase\s+(?:up\s+to\s+)?(?:an\s+aggregate\s+of\s+)?([\d,]{4,})\s+(?:Class\s+\w+\s+)?(?:[Oo]rdinary\s+|[Cc]ommon\s+)?[Ss]hares', re.I),
    # "warrants exercisable for an aggregate of 1,234,567 shares"
    re.compile(r'[Ww]arrant[s]?\s+exercisable\s+(?:for\s+|to\s+purchase\s+)?(?:an\s+aggregate\s+of\s+)?([\d,]{4,})\s+(?:Class\s+\w+\s+)?(?:[Oo]rdinary\s+|[Cc]ommon\s+)?[Ss]hares', re.I),
    # "warrants representing the right to purchase 1,234,567 shares"
    re.compile(r'[Ww]arrant[s]?\s+representing\s+the\s+right\s+to\s+purchase\s+([\d,]{4,})\s+[Ss]hares', re.I),
    # "a maximum of 214,285,714 Class A Shares issuable upon exercise of the Warrants"
    re.compile(r'(?:maximum\s+of\s+|aggregate\s+of\s+)([\d,]{4,})\s+(?:Class\s+\w+\s+)?(?:[Oo]rdinary\s+|[Cc]ommon\s+)?[Ss]hares\s+issuable\s+upon\s+(?:the\s+)?exercise\s+of\s+(?:the\s+)?[Ww]arrant', re.I),
]

# Patterns to extract warrant EXERCISE PRICE
WARRANT_PRICE_PATTERNS = [
    # "exercise price of $X.XX per share"
    re.compile(r'exercise\s+price\s+of\s+\$?\s*([\d]+(?:\.\d+)?)\s*(?:per\s+share)?', re.I),
    # "exercisable at an exercise price of $X.XX"
    re.compile(r'exercisable\s+at\s+an\s+exercise\s+price\s+(?:equal\s+to\s+|of\s+)?\$?\s*([\d]+(?:\.\d+)?)', re.I),
    # "at an exercise price equal to $X.XX"
    re.compile(r'exercise\s+price\s+(?:equal\s+to\s+|of\s+)?\$?\s*([\d]+(?:\.\d+)?)', re.I),
    # "exercisable at $X.XX per share"
    re.compile(r'exercisable\s+at\s+(?:a\s+price\s+of\s+)?\$?\s*([\d]+(?:\.\d+)?)\s+per\s+share', re.I),
    # "$X.XX per share exercise price"
    re.compile(r'\$\s*([\d]+(?:\.\d+)?)\s+(?:per\s+share\s+)?exercise\s+price', re.I),
]
WARRANT_NEGATION_PATTERNS = [
    re.compile(r'no\s+warrants?\s+(?:are\s+)?(?:outstanding|exercisable|issued)', re.I),
    re.compile(r'we\s+(?:do\s+not\s+have|have\s+no)\s+(?:any\s+)?warrants', re.I),
]

RSPLIT_PATTERNS = [
    re.compile(r'reverse\s+stock\s+split', re.I),
    re.compile(r'(\d+)[\s\-](?:to|for)[\s\-](\d+)\s+reverse', re.I),
]

# Majority ownership — only structured beneficial-ownership phrasing
MAJORITY_PATTERNS = [
    re.compile(r'beneficially\s+own[s]?\s+(?:approximately\s+)?(\d{1,3}(?:\.\d+)?)\s*%', re.I),
    re.compile(r'(\d{1,3}(?:\.\d+)?)\s*%\s+of\s+(?:our|the\s+company\'s|the)\s+(?:outstanding|issued)\s+(?:shares|common\s+stock|voting)', re.I),
    re.compile(r'beneficial\s+owner(?:ship)?\s+of\s+(?:approximately\s+)?(\d{1,3}(?:\.\d+)?)\s*%', re.I),
    re.compile(r'\baggregate\s+beneficial\s+ownership\s+of\s+(?:approximately\s+)?(\d{1,3}(?:\.\d+)?)\s*%', re.I),
]
# Item 5 of 13D/G has structured "Percent of class: X%" — most reliable
ITEM5_PATTERNS = [
    re.compile(r'(?:percent\s+of\s+class|aggregate\s+amount\s+beneficially\s+owned)[^%]{0,200}?(\d{1,3}(?:\.\d+)?)\s*%', re.I),
]
# Anti-patterns — phrases that surface % numbers we should ignore
MAJORITY_BLACKLIST_NEAR = re.compile(
    r'(?:director\s+independence|board\s+independence|audit\s+committee|attendance|quorum|'
    r'voting\s+threshold|approval\s+of|in\s+favor\s+of|tax\s+rate|effective\s+rate|'
    r'gross\s+margin|operating\s+margin|interest\s+rate|growth\s+rate)', re.I)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def fmt_num(n, d=2):
    if n is None: return "N/A"
    try:
        n = float(n)
        if abs(n) >= 1e9: return f"{n/1e9:.{d}f}B"
        if abs(n) >= 1e6: return f"{n/1e6:.{d}f}M"
        if abs(n) >= 1e3: return f"{n/1e3:.{d}f}K"
        return f"{n:,.0f}"
    except: return "N/A"

def fmt_pct(n, d=1):
    if n is None: return "N/A"
    try: return f"{float(n):.{d}f}%"
    except: return "N/A"

def fmt_money(n, d=1):
    if n is None: return None
    try:
        n = float(n)
        if n >= 1e9: return f"${n/1e9:.{d}f}B"
        if n >= 1e6: return f"${n/1e6:.{d}f}M"
        if n >= 1e3: return f"${n/1e3:.{d}f}K"
        return f"${n:,.0f}"
    except: return None

def snippet(text, match, before=60, after=100, maxlen=140):
    st = max(0, match.start() - before)
    en = min(len(text), match.end() + after)
    return re.sub(r'\s+', ' ', text[st:en]).strip()[:maxlen]


# ─────────────────────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────────────────────
# SEC policy: ≤10 req/s with proper User-Agent. We rate-limit globally via a
# single-thread-token "bucket" — every request awaits a minimum gap.
_EDGAR_GAP = 0.12  # ~8 req/s ceiling (SEC limit is 10/s)
_edgar_lock = None  # lazy-init inside the running loop to avoid "attached to a different loop" errors
_edgar_lock_loop = None
_edgar_last  = 0.0

def _get_edgar_lock():
    global _edgar_lock, _edgar_lock_loop
    loop = asyncio.get_running_loop()
    if _edgar_lock is None or _edgar_lock_loop is not loop:
        _edgar_lock = asyncio.Lock()
        _edgar_lock_loop = loop
    return _edgar_lock

async def _edgar_acquire():
    global _edgar_last
    lock = _get_edgar_lock()
    async with lock:
        loop = asyncio.get_running_loop()
        now = loop.time()
        wait = _EDGAR_GAP - (now - _edgar_last)
        if wait > 0:
            await asyncio.sleep(wait)
        _edgar_last = loop.time()

async def poly(session, url):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.json(content_type=None) if r.status == 200 else {}
    except Exception:
        return {}

async def edgar_json(s, url, retries=3):
    for attempt in range(retries):
        await _edgar_acquire()
        try:
            async with s.get(url, headers=SEC_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                if r.status == 429:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                return None
        except Exception:
            await asyncio.sleep(0.5 * (attempt + 1))
    return None

async def edgar_text(s, url, mb=120000, retries=3):
    for attempt in range(retries):
        await _edgar_acquire()
        try:
            async with s.get(url, headers=SEC_HEADERS, timeout=aiohttp.ClientTimeout(total=25)) as r:
                if r.status == 200:
                    raw = await r.content.read(mb)
                    text = raw.decode("utf-8", errors="ignore")
                    text = re.sub(r'<[^>]+>', ' ', text)
                    text = unescape(text)
                    return re.sub(r'\s+', ' ', text)
                if r.status == 429:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                return ""
        except Exception:
            await asyncio.sleep(0.5 * (attempt + 1))
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Polygon fetchers
# ─────────────────────────────────────────────────────────────────────────────
async def get_details(s, t):
    d = await poly(s, f"https://api.polygon.io/v3/reference/tickers/{t}?apiKey={POLYGON_KEY}")
    if d.get("results"):
        return d["results"]
    d2 = await poly(s, f"https://api.polygon.io/v3/reference/tickers?ticker={t}&active=false&apiKey={POLYGON_KEY}")
    results = d2.get("results", [])
    return results[0] if results else {}

async def get_ticker_history(s, t):
    """FIXED: correct path is /vX/reference/tickers/{t}/events  and  events[].ticker_change.ticker"""
    d = await poly(s, f"https://api.polygon.io/vX/reference/tickers/{t}/events?apiKey={POLYGON_KEY}")
    events = (d.get("results") or {}).get("events", [])
    prior = []
    for ev in events:
        if ev.get("type") == "ticker_change":
            ch = ev.get("ticker_change") or {}
            old = ch.get("ticker") or ev.get("ticker_symbol") or ""
            date = ev.get("date", "")
            if old and old.upper() != t.upper():
                prior.append({"ticker": old.upper(), "date": date})
    return prior

async def get_float(s, t):
    d = await poly(s, f"https://api.polygon.io/stocks/vX/float?ticker={t}&apiKey={POLYGON_KEY}")
    r = d.get("results", [])
    return r[0] if r else {}

async def get_si(s, t):
    d = await poly(s, f"https://api.polygon.io/stocks/v1/short-interest?ticker={t}&limit=10&apiKey={POLYGON_KEY}")
    r = d.get("results", [])
    if not r: return {}
    r.sort(key=lambda x: x.get("settlement_date", ""), reverse=True)
    return r[0]

async def get_sv(s, t):
    d = await poly(s, f"https://api.polygon.io/stocks/v1/short-volume?ticker={t}&limit=5&apiKey={POLYGON_KEY}")
    r = d.get("results", [])
    if not r: return {}
    r.sort(key=lambda x: x.get("date", ""), reverse=True)
    return r[0]

async def get_price(s, t):
    d = await poly(s, f"https://api.polygon.io/v2/aggs/ticker/{t}/prev?adjusted=true&apiKey={POLYGON_KEY}")
    r = d.get("results", [])
    return r[0].get("c") if r else None


# ─────────────────────────────────────────────────────────────────────────────
# EDGAR — submissions, filings, exhibits
# ─────────────────────────────────────────────────────────────────────────────
async def get_submissions(s, cik_raw):
    cik = cik_raw.lstrip("0").zfill(10)
    return await edgar_json(s, f"https://data.sec.gov/submissions/CIK{cik}.json")

def _iter_filings(submissions, forms, n=10, since_date=None):
    """Yield up to n filings matching `forms` (already form-set expanded) since `since_date`."""
    if not submissions: return
    rec = submissions.get("filings", {}).get("recent", {})
    flist = rec.get("form", [])
    dates = rec.get("filingDate", [])
    accs  = rec.get("accessionNumber", [])
    docs  = rec.get("primaryDocument", [])
    yielded = 0
    for i, f in enumerate(flist):
        if f in forms:
            if since_date and dates[i] < since_date:
                continue
            yield {
                "form": f, "date": dates[i], "acc": accs[i],
                "doc": docs[i] if i < len(docs) else "",
            }
            yielded += 1
            if yielded >= n:
                return

# Per-build_embed cache so multiple detectors share filing text
_filing_cache: dict = {}

async def get_filing_text(s, cik_raw, filing, mb=120000):
    """Fetch primary doc text. Falls back to scanning index.json for first .htm exhibit.
    Cached by accession # for the duration of one build_embed call."""
    cache_key = filing["acc"]
    if cache_key in _filing_cache:
        return _filing_cache[cache_key]

    cik_i = int(cik_raw.lstrip("0") or "0")
    acc = filing["acc"].replace("-", "")
    text = ""
    if filing.get("doc"):
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_i}/{acc}/{filing['doc']}"
        text = await edgar_text(s, url, mb=mb)
    if not text or len(text) < 500:
        idx = await edgar_json(s, f"https://www.sec.gov/Archives/edgar/data/{cik_i}/{acc}/index.json")
        if idx:
            items = idx.get("directory", {}).get("item", [])
            candidates = sorted(
                [it for it in items if it["name"].lower().endswith((".htm", ".html", ".txt"))
                 and not it["name"].lower().endswith("-index.html")
                 and not it["name"].lower().endswith("-index-headers.html")],
                key=lambda it: (
                    0 if "ex99" in it["name"].lower() or "ex-99" in it["name"].lower() else
                    1 if "ex10" in it["name"].lower() or "ex-10" in it["name"].lower() else 2
                ))
            for it in candidates[:2]:
                url = f"https://www.sec.gov/Archives/edgar/data/{cik_i}/{acc}/{it['name']}"
                text = await edgar_text(s, url, mb=mb)
                if text and len(text) > 500:
                    break
    _filing_cache[cache_key] = text
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Offering-size parser
# ─────────────────────────────────────────────────────────────────────────────
# All these handle the literal phrasings seen in real prospectuses
_SIZE_PATTERNS = [
    # "aggregate offering price of up to $70,000,000"
    re.compile(r'aggregate\s+(?:offering\s+(?:price|amount)|principal\s+amount|proceeds?)\s+of\s+(?:up\s+to\s+)?\$\s*([\d,]+(?:\.\d+)?)\s*(million|billion)?', re.I),
    # "Up to $70,000,000 of Common Stock" (cover page)
    re.compile(r'\bup\s+to\s+\$\s*([\d,]+(?:\.\d+)?)\s*(million|billion)?\s+(?:of|aggregate|in)', re.I),
    # "gross proceeds of $X"
    re.compile(r'(?:gross|net|total)\s+proceeds\s+(?:of|to\s+us\s+from\s+the\s+offering\s+of)\s+(?:approximately\s+)?\$\s*([\d,]+(?:\.\d+)?)\s*(million|billion)?', re.I),
    # "we may sell up to $50 million / $50,000,000"
    re.compile(r'(?:we\s+may\s+(?:sell|offer|issue)|the\s+offering\s+is\s+for)\s+(?:up\s+to\s+)?\$\s*([\d,]+(?:\.\d+)?)\s*(million|billion)?', re.I),
    # "$X million offering / placement"
    re.compile(r'\$\s*([\d,]+(?:\.\d+)?)\s*(million|billion)\s+(?:offering|placement|registered\s+direct|public\s+offering|private\s+placement|atm|at[\s\-]the[\s\-]market)', re.I),
    # bare "$X million" only when following "for", "of", "totaling"
    re.compile(r'(?:for|of|totaling|raised|raising)\s+(?:approximately\s+)?\$\s*([\d,]+(?:\.\d+)?)\s*(million|billion)\b', re.I),
]
_SHARES_AT_RE = re.compile(
    r'([\d,]+(?:\.\d+)?)\s*(million|billion)?\s+(?:shares|units)\s+(?:of\s+(?:our\s+)?(?:common\s+stock|class\s+\w+\s+common)\s+)?at\s+\$\s*([\d,]+(?:\.\d+)?)\s*(?:per\s+share)?',
    re.I)

def _scale(val, unit):
    u = (unit or "").lower()
    if u in ("billion", "b"): return val * 1e9
    if u in ("million", "m"): return val * 1e6
    return val

def parse_offering_size(text, mkt_cap=None):
    """Extract dollar offering size + per-share price. Returns label like '$70.0M @ $0.50/sh'."""
    if not text: return ""
    gross = None
    price = None

    # Try each pattern; take FIRST plausible result (earliest in doc usually = headline number)
    for pat in _SIZE_PATTERNS:
        m = pat.search(text)
        if not m: continue
        try:
            v = float(m.group(1).replace(",", ""))
            v = _scale(v, m.group(2) if m.lastindex and m.lastindex >= 2 else None)
        except Exception:
            continue
        # Sanity: must be at least $100k and (if mkt cap known) <= 100x mkt cap
        if v < 100_000:
            continue
        if mkt_cap and v > mkt_cap * 100:
            continue
        gross = v
        break

    # Per-share price + cross-check shares*price ≈ gross
    m2 = _SHARES_AT_RE.search(text)
    if m2:
        try:
            shares = _scale(float(m2.group(1).replace(",", "")), m2.group(2))
            px     = float(m2.group(3).replace(",", ""))
            if 0.0001 < px < 10_000 and shares < 1e10:
                price = px
                if gross is None:
                    derived = shares * px
                    if derived >= 100_000 and (not mkt_cap or derived <= mkt_cap * 100):
                        gross = derived
        except Exception:
            pass

    parts = []
    if gross:
        m_str = fmt_money(gross, d=1)
        if m_str:
            parts.append(m_str)
    if price and 0.0001 < price < 100_000:
        parts.append(f"@ ${price:.4f}/sh" if price < 0.01 else f"@ ${price:.2f}/sh")
    return "  ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Detectors
# ─────────────────────────────────────────────────────────────────────────────
def _is_pipe_risk_factor(text, match):
    """True if the PIPE match is generic risk-factor language (‘may engage in’)."""
    st = max(0, match.start() - 100)
    en = min(len(text), match.end() + 50)
    window = text[st:en]
    return bool(PIPE_RISK_FACTOR_RE.search(window))

def _has_warrant_negation(text, match):
    st = max(0, match.start() - 80)
    en = min(len(text), match.end() + 80)
    window = text[st:en]
    return any(p.search(window) for p in WARRANT_NEGATION_PATTERNS)

def _is_majority_blacklisted(text, match):
    st = max(0, match.start() - 120)
    en = min(len(text), match.end() + 60)
    window = text[st:en]
    return bool(MAJORITY_BLACKLIST_NEAR.search(window))


async def detect_atm(s, cik_raw, submissions, mkt_cap=None):
    """Active ATM = recent S-3/424B5/424B3/424B4/8-K with at-the-market or sales-agreement language.

    Strategy: prefer 424B5/S-3 prospectus matches over 8-Ks (better evidence).
    Skip filings that explicitly terminate the program. Require strong patterns
    (at-the-market, ATM offering) over weak ones (sales agreement) for 8-Ks.
    """
    cutoff_18mo = (datetime.utcnow() - timedelta(days=540)).strftime("%Y-%m-%d")
    prospectus = list(_iter_filings(submissions, ATM_PROSPECTUS_FORMS, n=5, since_date=cutoff_18mo))
    eightks    = list(_iter_filings(submissions, OFFERING_8K_FORMS, n=6, since_date=cutoff_18mo))

    # Strong-evidence patterns (must match for 8-K-only sources)
    STRONG = ATM_PATTERNS[:3]   # at-the-market, ATM offering, sales agreement

    # Pass 1 — prospectus filings (424B5 etc) — strong source
    for f in prospectus:
        text = await get_filing_text(s, cik_raw, f, mb=80000)
        if not text: continue
        if any(p.search(text) for p in ATM_TERMINATED_PATTERNS): continue
        for pat in ATM_PATTERNS:
            m = pat.search(text)
            if m:
                size = parse_offering_size(text, mkt_cap)
                return {"active": True, "form": f["form"], "date": f["date"],
                        "evidence": snippet(text, m)[:120], "size": size}

    # Pass 2 — 8-K filings — require strong pattern AND "at-the-market" language
    for f in eightks:
        text = await get_filing_text(s, cik_raw, f, mb=60000)
        if not text: continue
        if any(p.search(text) for p in ATM_TERMINATED_PATTERNS): continue
        # Require BOTH a strong pattern AND "at-the-market" language
        atm_lang = re.search(r'at[\s\-]the[\s\-]market', text, re.I)
        if not atm_lang: continue
        for pat in STRONG:
            m = pat.search(text)
            if m:
                size = parse_offering_size(text, mkt_cap)
                # Use the at-the-market match for better evidence snippet
                return {"active": True, "form": f["form"], "date": f["date"],
                        "evidence": snippet(text, atm_lang)[:120], "size": size}
    return {"active": False}


async def detect_shelf(s, cik_raw, submissions, mkt_cap=None):
    """Live S-3 shelf = S-3 filed within ~3 years AND has matching EFFECT (or is S-3ASR auto-effective)."""
    cutoff = (datetime.utcnow() - timedelta(days=SHELF_WINDOW_DAYS)).strftime("%Y-%m-%d")
    s3_filings = list(_iter_filings(submissions, SHELF_FORMS, n=4, since_date=cutoff))
    if not s3_filings: return {"active": False}
    effects = list(_iter_filings(submissions, EFFECT_FORMS, n=20))
    effect_dates = sorted([e["date"] for e in effects], reverse=True)

    for f in s3_filings:
        # S-3ASR is automatically effective on filing
        is_effective = (f["form"] == "S-3ASR")
        if not is_effective:
            # Look for EFFECT filing dated >= S-3 filing date
            is_effective = any(d >= f["date"] for d in effect_dates)
        if not is_effective:
            continue
        size = ""
        text = await get_filing_text(s, cik_raw, f, mb=80000)
        if text:
            size = parse_offering_size(text, mkt_cap)
        return {"active": True, "form": f["form"], "date": f["date"], "size": size}
    return {"active": False}


async def detect_pipe_rd(s, cik_raw, submissions, mkt_cap=None):
    """PIPE / Registered Direct in last 12 months — require event-context, dollar size."""
    cutoff = (datetime.utcnow() - timedelta(days=365)).strftime("%Y-%m-%d")
    filings = list(_iter_filings(submissions, PIPE_FORMS, n=8, since_date=cutoff))
    hits = []
    seen_keys = set()  # dedupe by (label, month) — multiple forms reference same offering
    for f in filings:
        text = await get_filing_text(s, cik_raw, f, mb=80000)
        if not text: continue
        for pat in PIPE_PATTERNS:
            m = pat.search(text)
            if not m: continue
            if _is_pipe_risk_factor(text, m): continue
            size = parse_offering_size(text, mkt_cap)
            label = m.group(0).strip()
            if size: label = f"{label} — {size}"
            key = (label.lower(), f["date"][:7])
            if key in seen_keys: break
            seen_keys.add(key)
            hits.append({"form": f["form"], "date": f["date"], "label": label[:70]})
            break
        if len(hits) >= 2: break
    return hits


async def detect_8k_offerings(s, cik_raw, submissions, mkt_cap=None):
    """Recent 8-K offering announcements — last 12mo, deduped by month."""
    cutoff = (datetime.utcnow() - timedelta(days=365)).strftime("%Y-%m-%d")
    filings = list(_iter_filings(submissions, OFFERING_8K_FORMS, n=10, since_date=cutoff))
    hits = []
    seen_months = set()
    for f in filings:
        month_key = f["date"][:7]
        if month_key in seen_months: continue
        text = await get_filing_text(s, cik_raw, f, mb=60000)
        if not text: continue
        for pat in OFFERING_8K_PATTERNS:
            m = pat.search(text)
            if not m: continue
            size = parse_offering_size(text, mkt_cap)
            label = m.group(0).strip()
            if size: label = f"{label} — {size}"
            hits.append({"date": f["date"], "label": label[:80]})
            seen_months.add(month_key)
            break
        if len(hits) >= 3: break
    return hits


def _extract_warrant_details(text, match):
    """Given a warrant detection match, look in a window around it for share count and exercise price.
    Sums multi-tranche warrants (Series A + B + Pre-Funded). Captures exercise price range."""
    # Search in a wide window — prospectus warrant details span 2k+ chars.
    start = max(0, match.start() - 1000)
    end   = min(len(text), match.end() + 3000)
    window = text[start:end]

    # Collect all unique share-count hits across all patterns.
    share_hits = set()
    for pat in WARRANT_SHARES_PATTERNS:
        for sm in pat.finditer(window):
            try:
                n = int(sm.group(1).replace(",", ""))
                if 1000 <= n <= 5_000_000_000:
                    share_hits.add(n)
            except (ValueError, IndexError):
                continue

    # Collect all unique exercise-price hits.
    price_hits = []
    seen_prices = set()
    for pat in WARRANT_PRICE_PATTERNS:
        for pm in pat.finditer(window):
            try:
                p = float(pm.group(1))
                if 0.0001 <= p <= 10_000 and p not in seen_prices:
                    price_hits.append(p)
                    seen_prices.add(p)
            except (ValueError, IndexError):
                continue

    # For shares: dedupe "summary" totals from individual tranche numbers.
    # Filings often state the aggregate (e.g. "maximum of 214,285,714 shares")
    # alongside per-tranche numbers (107M Series A + 85M Series B + 21M pre-funded).
    # Strategy: if the largest hit equals the sum of any subset of the smaller hits
    # (within 10% tolerance), trust the largest as the aggregate. Otherwise sum.
    shares = None
    if share_hits:
        sorted_hits = sorted(share_hits, reverse=True)
        largest = sorted_hits[0]
        smaller = sorted_hits[1:]

        def _is_subset_sum(target, items, tol=0.10):
            # Check if any subset of items sums to ~target.
            from itertools import combinations
            for r in range(1, min(len(items), 5) + 1):
                for combo in combinations(items, r):
                    s = sum(combo)
                    if s > 0 and abs(s - target) / target < tol:
                        return True
            return False

        if smaller and _is_subset_sum(largest, smaller):
            # Largest is the aggregate — use it directly.
            shares = largest
        else:
            # Distinct tranches — sum up to 4.
            shares = sum(sorted_hits[:4])

    # For price: ignore obvious pre-funded penny prices ($0.0001) when reporting
    # the headline price unless they're the only ones.
    real_prices = [p for p in price_hits if p >= 0.05]
    if real_prices:
        price_min, price_max = min(real_prices), max(real_prices)
    elif price_hits:
        price_min = price_max = price_hits[0]
    else:
        price_min = price_max = None

    return shares, price_min, price_max


def _fmt_shares(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return f"{n:,}"


async def detect_warrants(s, cik_raw, submissions, mkt_cap=None):
    """Warrants outstanding — guard against negation. Extract share count + exercise price."""
    cutoff = (datetime.utcnow() - timedelta(days=540)).strftime("%Y-%m-%d")
    filings = list(_iter_filings(submissions, WARRANT_FORMS, n=4, since_date=cutoff))
    for f in filings:
        text = await get_filing_text(s, cik_raw, f, mb=80000)
        if not text: continue
        for pat in WARRANT_PATTERNS:
            m = pat.search(text)
            if not m: continue
            if _has_warrant_negation(text, m): continue
            size = parse_offering_size(text, mkt_cap)
            shares, price_min, price_max = _extract_warrant_details(text, m)
            return {"found": True, "form": f["form"], "date": f["date"],
                    "ctx": snippet(text, m, before=0, after=80, maxlen=100),
                    "size": size, "shares": shares,
                    "price_min": price_min, "price_max": price_max}
    return {"found": False}


async def detect_reverse_split(s, cik_raw, submissions):
    cutoff = (datetime.utcnow() - timedelta(days=730)).strftime("%Y-%m-%d")
    filings = list(_iter_filings(submissions, SPLIT_FORMS, n=10, since_date=cutoff))
    hits = []
    seen_quarters = set()   # dedupe — same event referenced repeatedly
    for f in filings:
        # quarter-bucket dedupe (year + quarter)
        y, mo, _ = f["date"].split("-")
        q = (int(y), (int(mo) - 1) // 3)
        if q in seen_quarters: continue
        text = await get_filing_text(s, cik_raw, f, mb=60000)
        if not text: continue
        for pat in RSPLIT_PATTERNS:
            m = pat.search(text)
            if not m: continue
            ctx = snippet(text, m, before=80, after=80, maxlen=160).lower()
            # Skip generic "may consider a reverse split" boilerplate
            if "may " in ctx or "could " in ctx or "consider " in ctx or "propose" in ctx:
                continue
            hits.append({"date": f["date"], "ctx": snippet(text, m, before=0, after=60, maxlen=80)})
            seen_quarters.add(q)
            break
        if len(hits) >= 2: break
    return hits


async def detect_majority(s, cik_raw, submissions):
    """Majority owner — prefer 13D/G Item 5 percent-of-class; fall back to DEF 14A beneficial-ownership."""
    cutoff = (datetime.utcnow() - timedelta(days=540)).strftime("%Y-%m-%d")
    # 13D/G first
    filings_dg = list(_iter_filings(submissions, _form_set("SC 13D", "SC 13D/A"), n=2, since_date=cutoff))
    filings_dg += list(_iter_filings(submissions, _form_set("SC 13G", "SC 13G/A"), n=2, since_date=cutoff))
    best = {"found": False}
    high = 0.0

    for f in filings_dg:
        text = await get_filing_text(s, cik_raw, f, mb=60000)
        if not text: continue
        # Item 5 patterns
        for pat in ITEM5_PATTERNS:
            for m in pat.finditer(text):
                if _is_majority_blacklisted(text, m): continue
                try: pct = float(m.group(1))
                except: continue
                if 50 <= pct <= 100 and pct > high:
                    high = pct
                    best = {"found": True, "pct": pct, "form": f["form"], "date": f["date"],
                            "ctx": snippet(text, m, before=80, after=40, maxlen=120)}

    # Then DEF 14A beneficial-ownership
    filings_proxy = list(_iter_filings(submissions, _form_set("DEF 14A"), n=2, since_date=cutoff))
    for f in filings_proxy:
        text = await get_filing_text(s, cik_raw, f, mb=120000)
        if not text: continue
        for pat in MAJORITY_PATTERNS:
            for m in pat.finditer(text):
                if _is_majority_blacklisted(text, m): continue
                try: pct = float(m.group(1))
                except: continue
                if 50 <= pct <= 100 and pct > high:
                    high = pct
                    best = {"found": True, "pct": pct, "form": f["form"], "date": f["date"],
                            "ctx": snippet(text, m, before=80, after=40, maxlen=120)}
    return best


# ─────────────────────────────────────────────────────────────────────────────
# Embed builder
# ─────────────────────────────────────────────────────────────────────────────
async def build_embed(ticker):
    ticker = ticker.upper()
    _filing_cache.clear()  # per-call cache for filing text
    async with aiohttp.ClientSession() as s:
        det, flt, si, sv, price, prior_tickers = await asyncio.gather(
            get_details(s, ticker), get_float(s, ticker),
            get_si(s, ticker), get_sv(s, ticker),
            get_price(s, ticker), get_ticker_history(s, ticker))

        cik = det.get("cik", "")
        atm = {"active": False}
        shelf = {"active": False}
        pipe_rd = []
        offerings_8k = []
        warrants = {"found": False}
        rsplits = []
        maj = {"found": False}

        if cik:
            submissions = await get_submissions(s, cik)
            if submissions:
                mkt_cap = det.get("market_cap")
                (atm, shelf, pipe_rd, offerings_8k,
                 warrants, rsplits, maj) = await asyncio.gather(
                    detect_atm(s, cik, submissions, mkt_cap),
                    detect_shelf(s, cik, submissions, mkt_cap),
                    detect_pipe_rd(s, cik, submissions, mkt_cap),
                    detect_8k_offerings(s, cik, submissions, mkt_cap),
                    detect_warrants(s, cik, submissions, mkt_cap),
                    detect_reverse_split(s, cik, submissions),
                    detect_majority(s, cik, submissions))

    float_v  = flt.get("free_float")
    float_p  = flt.get("free_float_percent")
    si_v     = si.get("short_interest")
    dtc      = si.get("days_to_cover")
    si_dt    = si.get("settlement_date", "")
    sv_r     = sv.get("short_volume_ratio")
    si_pct   = (float(si_v) / float(float_v)) * 100 if si_v and float_v else None
    atm_on   = atm.get("active", False)
    maj_on   = maj.get("found", False)
    locale     = (det.get("locale") or "").lower()
    country    = (det.get("address", {}).get("country") or "").lower()
    desc       = det.get("description") or ""
    china_hq   = (locale in CHINA_CODES or country in CHINA_CODES
                  or bool(CHINA_DESC_RE.search(desc[:1000])))

    any_red = atm_on or maj_on or china_hq or shelf.get("active") or rsplits or (si_pct and si_pct > 25)
    color = (0xFF3333 if any_red else
             (0xFF9900 if (pipe_rd or offerings_8k or warrants.get("found") or (si_pct and si_pct > 12))
              else 0x00CC66))

    price_s = f"${price:,.3f}" if price else "N/A"
    emb = discord.Embed(
        title=f"📊  {ticker}  —  {det.get('name', ticker)}",
        color=color, timestamp=datetime.utcnow())
    emb.description = (f"**Price:** {price_s}  ·  **Mkt Cap:** {fmt_num(det.get('market_cap'))}  ·  "
                       f"**Float:** {fmt_num(float_v)} ({fmt_pct(float_p)})")

    # ── Dilution signals
    dil_lines = []

    if atm_on:
        sz = f"  {atm['size']}" if atm.get('size') else ""
        dil_lines.append(f"🔴 **Active ATM**{sz} — `{atm['form']}` {atm['date']}\n> {atm.get('evidence','')[:100]}")
    else:
        dil_lines.append("🟢 No active ATM")

    if shelf.get("active"):
        sz = f"  {shelf['size']}" if shelf.get('size') else ""
        dil_lines.append(f"🔴 **Live S-3 Shelf**{sz} — filed {shelf['date']}")
    else:
        dil_lines.append("🟢 No active shelf registration")

    if pipe_rd:
        for p in pipe_rd[:2]:
            dil_lines.append(f"🟠 **{p['label'].strip()}** — `{p['form']}` {p['date']}")
    else:
        dil_lines.append("🟢 No recent PIPE / Registered Direct")

    if offerings_8k:
        for o in offerings_8k[:2]:
            dil_lines.append(f"🟠 **8-K** {o['date']} — {o['label'].strip()}")

    if warrants.get("found"):
        parts = []
        if warrants.get("shares"):
            parts.append(f"{_fmt_shares(warrants['shares'])} shares")
        pmin, pmax = warrants.get("price_min"), warrants.get("price_max")
        if pmin is not None:
            if pmax is not None and pmax != pmin:
                parts.append(f"@ ${pmin:.4f}–${pmax:.4f}".replace(".0000", ".00"))
            else:
                # smart format: 2 decimals if >= $0.10, else more precision
                fmt = f"${pmin:.2f}" if pmin >= 0.10 else f"${pmin:.4f}"
                parts.append(f"@ {fmt}")
        if warrants.get("size"):
            parts.append(f"({warrants['size']})")
        detail = (" " + " ".join(parts)) if parts else ""
        dil_lines.append(f"🟠 **Warrants outstanding**{detail} — `{warrants['form']}` {warrants['date']}")
    else:
        dil_lines.append("🟢 No warrant overhang detected")

    if rsplits:
        for r in rsplits[:2]:
            dil_lines.append(f"🔴 **Reverse split** {r['date']} — {r['ctx'].strip()[:70]}")
    else:
        dil_lines.append("🟢 No reverse split history")

    emb.add_field(name="⚠️  Dilution Signals", value="\n".join(dil_lines), inline=False)

    # ── Ownership
    if maj_on:
        maj_val = (f"🔴  **{fmt_pct(maj.get('pct'))} MAJORITY HOLDER**\n"
                   f"`{maj.get('form')}` {maj.get('date')}\n"
                   f"> {maj.get('ctx','')[:100]}")
    else:
        maj_val = "🟢  No >50% majority holder found"
    emb.add_field(name="Insider / Majority Ownership", value=maj_val, inline=False)

    # ── HQ
    if china_hq:
        why = []
        if locale in CHINA_CODES: why.append(f"locale={locale.upper()}")
        if country in CHINA_CODES: why.append(f"country={country.upper()}")
        if CHINA_DESC_RE.search(desc[:1000]):
            m = CHINA_DESC_RE.search(desc[:1000])
            if m: why.append(f"description: ‘{m.group(0)}’")
        china_val = "🔴  **CHINA-BASED** (" + "  ·  ".join(why) + ")"
    else:
        china_val = "🟢  Not China-headquartered"
    emb.add_field(name="HQ / Domicile", value=china_val, inline=False)

    # ── Ticker history
    if prior_tickers:
        ticker_val = "⚠️  **Prior tickers:** " + "  ·  ".join(
            f"`{p['ticker']}` ({p['date']})" for p in prior_tickers[:5])
    else:
        ticker_val = "🟢  No ticker changes found"
    emb.add_field(name="Ticker History", value=ticker_val, inline=False)

    # ── Short interest
    short_lines = []
    if si_v:
        short_lines.append(f"**SI:** {fmt_num(si_v)} ({fmt_pct(si_pct)} of float)  ·  as of {si_dt}")
    if dtc:
        short_lines.append(f"**DTC:** {dtc:.1f} days")
    if sv_r:
        short_lines.append(f"**Short Vol Ratio:** {fmt_pct(sv_r)}")
    shares_out = det.get("share_class_shares_outstanding") or det.get("weighted_shares_outstanding")
    if shares_out:
        short_lines.append(f"**Shares Out:** {fmt_num(shares_out)}")
    if short_lines:
        emb.add_field(name="Short Interest", value="\n".join(short_lines), inline=False)

    emb.set_footer(text="Polygon.io + SEC EDGAR  ·  data may be delayed")
    return emb


# ─────────────────────────────────────────────────────────────────────────────
# Bot
# ─────────────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"✅  Dilution bot online as {client.user}  (guild {GUILD_ID})")


@client.event
async def on_message(msg: discord.Message):
    if msg.author == client.user: return
    if msg.guild and msg.guild.id != GUILD_ID: return
    if msg.channel.id != GENERAL_CHANNEL_ID: return
    tickers = list(dict.fromkeys(t.upper() for t in TICKER_RE.findall(msg.content)))
    if not tickers: return
    for ticker in tickers[:2]:
        async with msg.channel.typing():
            try:
                await msg.reply(embed=await build_embed(ticker), mention_author=False)
            except Exception as e:
                await msg.reply(f"⚠️  Error for `${ticker}`: {e}", mention_author=False)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌  DISCORD_TOKEN not set"); exit(1)
    client.run(DISCORD_TOKEN)
