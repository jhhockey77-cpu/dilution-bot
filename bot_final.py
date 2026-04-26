import os, re, asyncio, aiohttp, discord
from datetime import datetime, timedelta

POLYGON_KEY   = "0NCwDp9jz7LfO6C0y0y4tKPRLvmH5mX1"
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID      = 1497766198904094770
TICKER_RE     = re.compile(r'\$([A-Za-z]{1,5})\b')
SEC_HEADERS   = {"User-Agent": "DilutionBot jh.hockey77@gmail.com"}

CHINA_CODES = {"cn", "hk"}

# ── Pattern banks ─────────────────────────────────────────────────────────────

ATM_PATTERNS = [
    re.compile(r'at-the-market', re.I),
    re.compile(r'\batm\s+offering', re.I),
    re.compile(r'at the market offering', re.I),
    re.compile(r'sales agreement', re.I),
    re.compile(r'equity distribution agreement', re.I),
    re.compile(r'controlled equity offering', re.I),
]

PIPE_PATTERNS = [
    re.compile(r'\bPIPE\b'),
    re.compile(r'private investment in public equity', re.I),
    re.compile(r'registered direct', re.I),
    re.compile(r'private placement', re.I),
]

OFFERING_8K_PATTERNS = [
    re.compile(r'public offering', re.I),
    re.compile(r'underwritten offering', re.I),
    re.compile(r'best efforts offering', re.I),
    re.compile(r'registered direct offering', re.I),
    re.compile(r'private placement', re.I),
    re.compile(r'\bPIPE\b'),
]

WARRANT_PATTERNS = [
    re.compile(r'warrant[s]?\s+to\s+purchase', re.I),
    re.compile(r'([\d,]+(?:\.\d+)?)\s+warrant', re.I),
    re.compile(r'warrant[s]?\s+exercisable', re.I),
    re.compile(r'pre-funded\s+warrant', re.I),
]

RSPLIT_PATTERNS = [
    re.compile(r'reverse\s+stock\s+split', re.I),
    re.compile(r'reverse\s+split', re.I),
    re.compile(r'(\d+)-for-(\d+)\s+reverse', re.I),
]

MAJORITY_PATTERNS = [
    re.compile(r'(\d{1,3}(?:\.\d+)?)\s*%.*?(?:beneficially own|beneficial owner)', re.I),
    re.compile(r'beneficially own[s]?\s+(\d{1,3}(?:\.\d+)?)\s*%', re.I),
    re.compile(r'(\d{1,3}(?:\.\d+)?)\s*%\s+of\s+(?:the\s+)?(?:outstanding|issued)', re.I),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

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

def snippet(text, match, before=60, after=100, maxlen=140):
    st = max(0, match.start() - before)
    en = min(len(text), match.end() + after)
    return text[st:en].strip()[:maxlen]

# ── Polygon fetchers ──────────────────────────────────────────────────────────

async def poly(session, url):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            return await r.json() if r.status == 200 else {}
    except: return {}

async def get_details(s, t):
    # Try active ticker first, fall back to inactive (delisted) search
    d = await poly(s, f"https://api.polygon.io/v3/reference/tickers/{t}?apiKey={POLYGON_KEY}")
    if d.get("results"):
        return d["results"]
    d2 = await poly(s, f"https://api.polygon.io/v3/reference/tickers?ticker={t}&active=false&apiKey={POLYGON_KEY}")
    results = d2.get("results", [])
    return results[0] if results else {}

async def get_ticker_history(s, t):
    d = await poly(s, f"https://api.polygon.io/v3/reference/tickers/{t}/events?apiKey={POLYGON_KEY}")
    events = d.get("results", {}).get("events", [])
    prior = []
    for ev in events:
        if ev.get("type") == "ticker_change":
            old = ev.get("ticker_symbol", "") or ev.get("old_ticker", "")
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

# ── SEC fetchers ──────────────────────────────────────────────────────────────

async def edgar_json(s, url):
    try:
        async with s.get(url, headers=SEC_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.json(content_type=None) if r.status == 200 else None
    except: return None

async def edgar_text(s, url, mb=60000):
    try:
        async with s.get(url, headers=SEC_HEADERS, timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status != 200: return ""
            raw = await r.content.read(mb)
            text = raw.decode("utf-8", errors="ignore")
            text = re.sub(r'<[^>]+>', ' ', text)
            return re.sub(r'\s+', ' ', text)
    except: return ""

async def get_filings(s, cik_raw, forms, n=6):
    cik = cik_raw.lstrip("0").zfill(10)
    data = await edgar_json(s, f"https://data.sec.gov/submissions/CIK{cik}.json")
    if not data: return []
    rec   = data.get("filings", {}).get("recent", {})
    flist = rec.get("form", [])
    dates = rec.get("filingDate", [])
    accs  = rec.get("accessionNumber", [])
    docs  = rec.get("primaryDocument", [])
    out = []
    for i, f in enumerate(flist):
        if f in forms:
            out.append({"form": f, "date": dates[i], "acc": accs[i],
                        "doc": docs[i] if i < len(docs) else ""})
            if len(out) >= n: break
    return out

# ── Dilution detectors ────────────────────────────────────────────────────────

async def detect_atm(s, cik_raw):
    cik_i = int(cik_raw.lstrip("0") or "0")
    filings = await get_filings(s, cik_raw, {"S-3", "S-3/A", "424B3", "424B4"}, 5)
    for f in filings:
        if not f["doc"]: continue
        acc = f["acc"].replace("-", "")
        text = await edgar_text(s, f"https://www.sec.gov/Archives/edgar/data/{cik_i}/{acc}/{f['doc']}")
        if not text: continue
        for pat in ATM_PATTERNS:
            m = pat.search(text)
            if m:
                return {"active": True, "form": f["form"], "date": f["date"],
                        "evidence": snippet(text, m)[:120]}
    return {"active": False}

async def detect_shelf(s, cik_raw):
    """Check for a live S-3 shelf (filed in last 3 years, which is the SEC effective window)."""
    filings = await get_filings(s, cik_raw, {"S-3", "S-3/A"}, 3)
    cutoff = (datetime.utcnow() - timedelta(days=3*365)).strftime("%Y-%m-%d")
    for f in filings:
        if f["date"] >= cutoff:
            return {"active": True, "form": f["form"], "date": f["date"]}
    return {"active": False}

async def detect_pipe_rd(s, cik_raw):
    """Scan recent S-1 / 424B3 filings for PIPE or registered direct language."""
    cik_i = int(cik_raw.lstrip("0") or "0")
    filings = await get_filings(s, cik_raw, {"S-1", "S-1/A", "424B3", "424B4"}, 5)
    hits = []
    for f in filings:
        if not f["doc"]: continue
        acc = f["acc"].replace("-", "")
        text = await edgar_text(s, f"https://www.sec.gov/Archives/edgar/data/{cik_i}/{acc}/{f['doc']}")
        if not text: continue
        for pat in PIPE_PATTERNS:
            m = pat.search(text)
            if m:
                hits.append({"form": f["form"], "date": f["date"],
                             "label": m.group(0)[:30]})
                break
    return hits  # list of recent PIPE/RD filings

async def detect_8k_offerings(s, cik_raw):
    """Scan recent 8-Ks for dilutive offering announcements."""
    cik_i = int(cik_raw.lstrip("0") or "0")
    filings = await get_filings(s, cik_raw, {"8-K", "8-K/A"}, 8)
    hits = []
    for f in filings:
        if not f["doc"]: continue
        acc = f["acc"].replace("-", "")
        text = await edgar_text(s, f"https://www.sec.gov/Archives/edgar/data/{cik_i}/{acc}/{f['doc']}", mb=30000)
        if not text: continue
        for pat in OFFERING_8K_PATTERNS:
            m = pat.search(text)
            if m:
                hits.append({"date": f["date"], "label": snippet(text, m, before=0, after=60, maxlen=80)})
                break
        if len(hits) >= 3: break
    return hits

async def detect_warrants(s, cik_raw):
    """Check most recent S-1/424B for warrant mentions."""
    cik_i = int(cik_raw.lstrip("0") or "0")
    filings = await get_filings(s, cik_raw, {"424B3", "424B4", "S-1", "S-1/A"}, 3)
    for f in filings:
        if not f["doc"]: continue
        acc = f["acc"].replace("-", "")
        text = await edgar_text(s, f"https://www.sec.gov/Archives/edgar/data/{cik_i}/{acc}/{f['doc']}")
        if not text: continue
        for pat in WARRANT_PATTERNS:
            m = pat.search(text)
            if m:
                return {"found": True, "form": f["form"], "date": f["date"],
                        "ctx": snippet(text, m, before=0, after=80, maxlen=100)}
    return {"found": False}

async def detect_reverse_split(s, cik_raw):
    """Check recent 8-K and DEF 14A for reverse split history."""
    cik_i = int(cik_raw.lstrip("0") or "0")
    filings = await get_filings(s, cik_raw, {"8-K", "8-K/A", "DEF 14A"}, 10)
    hits = []
    for f in filings:
        if not f["doc"]: continue
        acc = f["acc"].replace("-", "")
        text = await edgar_text(s, f"https://www.sec.gov/Archives/edgar/data/{cik_i}/{acc}/{f['doc']}", mb=30000)
        if not text: continue
        for pat in RSPLIT_PATTERNS:
            m = pat.search(text)
            if m:
                hits.append({"date": f["date"], "ctx": snippet(text, m, before=0, after=60, maxlen=80)})
                break
        if len(hits) >= 2: break
    return hits

async def detect_majority(s, cik_raw):
    cik_i = int(cik_raw.lstrip("0") or "0")
    filings = await get_filings(s, cik_raw, {"DEF 14A", "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"}, 4)
    best = {"found": False}
    high = 0.0
    for f in filings:
        if not f["doc"]: continue
        acc = f["acc"].replace("-", "")
        text = await edgar_text(s, f"https://www.sec.gov/Archives/edgar/data/{cik_i}/{acc}/{f['doc']}", mb=80000)
        if not text: continue
        for pat in MAJORITY_PATTERNS:
            for m in pat.finditer(text):
                try: pct = float(m.group(1))
                except: continue
                if pct >= 50 and pct > high:
                    high = pct
                    best = {"found": True, "pct": pct, "form": f["form"], "date": f["date"],
                            "ctx": snippet(text, m, before=80, after=40, maxlen=120)}
    return best

# ── Embed builder ─────────────────────────────────────────────────────────────

async def build_embed(ticker):
    ticker = ticker.upper()
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
            (atm, shelf, pipe_rd, offerings_8k,
             warrants, rsplits, maj) = await asyncio.gather(
                detect_atm(s, cik),
                detect_shelf(s, cik),
                detect_pipe_rd(s, cik),
                detect_8k_offerings(s, cik),
                detect_warrants(s, cik),
                detect_reverse_split(s, cik),
                detect_majority(s, cik))

    float_v = flt.get("free_float")
    float_p = flt.get("free_float_percent")
    si_v    = si.get("short_interest")
    dtc     = si.get("days_to_cover")
    si_dt   = si.get("settlement_date", "")
    sv_r    = sv.get("short_volume_ratio")
    si_pct  = (float(si_v)/float(float_v))*100 if si_v and float_v else None
    atm_on  = atm.get("active", False)
    maj_on  = maj.get("found", False)
    locale  = (det.get("locale") or "").lower()
    country = (det.get("address", {}).get("country") or "").lower()
    china_hq = locale in CHINA_CODES or country in CHINA_CODES

    any_red = atm_on or maj_on or china_hq or shelf.get("active") or rsplits or (si_pct and si_pct > 25)
    color = 0xFF3333 if any_red else (0xFF9900 if (pipe_rd or offerings_8k or warrants.get("found") or (si_pct and si_pct > 12)) else 0x00CC66)

    price_s = f"${price:,.3f}" if price else "N/A"
    emb = discord.Embed(
        title=f"📊  {ticker}  —  {det.get('name', ticker)}",
        color=color, timestamp=datetime.utcnow())
    emb.description = (f"**Price:** {price_s}  ·  **Mkt Cap:** {fmt_num(det.get('market_cap'))}  ·  "
                       f"**Float:** {fmt_num(float_v)} ({fmt_pct(float_p)})")

    # ── Dilution signals ──────────────────────────────────────────────────────
    dil_lines = []

    # ATM
    if atm_on:
        dil_lines.append(f"🔴 **Active ATM** — `{atm['form']}` {atm['date']}\n> {atm.get('evidence','')[:100]}...")
    else:
        dil_lines.append("🟢 No active ATM")

    # Live shelf
    if shelf.get("active"):
        dil_lines.append(f"🔴 **Live S-3 Shelf** — filed {shelf['date']} (within 3yr window)")
    else:
        dil_lines.append("🟢 No active shelf registration")

    # PIPE / Registered Direct
    if pipe_rd:
        for p in pipe_rd[:2]:
            dil_lines.append(f"🟠 **{p['label'].strip()}** — `{p['form']}` {p['date']}")
    else:
        dil_lines.append("🟢 No recent PIPE / Registered Direct")

    # 8-K offerings
    if offerings_8k:
        for o in offerings_8k[:2]:
            dil_lines.append(f"🟠 **8-K offering** {o['date']} — {o['label'].strip()}")

    # Warrants
    if warrants.get("found"):
        dil_lines.append(f"🟠 **Warrants outstanding** — `{warrants['form']}` {warrants['date']}\n> {warrants.get('ctx','')[:80]}")
    else:
        dil_lines.append("🟢 No warrant overhang detected")

    # Reverse splits
    if rsplits:
        for r in rsplits[:2]:
            dil_lines.append(f"🔴 **Reverse split** {r['date']} — {r['ctx'].strip()[:70]}")
    else:
        dil_lines.append("🟢 No reverse split history")

    emb.add_field(name="⚠️  Dilution Signals", value="\n".join(dil_lines), inline=False)

    # ── Ownership ─────────────────────────────────────────────────────────────
    if maj_on:
        maj_val = (f"🔴  **{fmt_pct(maj.get('pct'))} MAJORITY HOLDER**\n"
                   f"`{maj.get('form')}` {maj.get('date')}\n"
                   f"> {maj.get('ctx','')[:100]}...")
    else:
        maj_val = "🟢  No >50% majority holder found"
    emb.add_field(name="Insider / Majority Ownership", value=maj_val, inline=False)

    # ── China / HQ ────────────────────────────────────────────────────────────
    if china_hq:
        china_val = f"🔴  **CHINA-BASED** (locale: `{det.get('locale','?').upper()}`)"
    else:
        china_val = "🟢  Not China-headquartered"
    emb.add_field(name="HQ / Domicile", value=china_val, inline=False)

    # ── Ticker history ────────────────────────────────────────────────────────
    if prior_tickers:
        ticker_val = "⚠️  **Prior tickers:** " + "  ·  ".join(
            f"`{p['ticker']}` ({p['date']})" for p in prior_tickers[:5])
    else:
        ticker_val = "🟢  No ticker changes found"
    emb.add_field(name="Ticker History", value=ticker_val, inline=False)

    # ── Short interest ────────────────────────────────────────────────────────
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

# ── Bot ───────────────────────────────────────────────────────────────────────

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
