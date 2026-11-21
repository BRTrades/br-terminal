"""
BR Terminal – Daten-Update
Läuft automatisch auf GitHub (alle 15 Min) und schreibt data/latest.json für die Website.

Quellen (alle kostenlos):
  CBOE        Optionsdaten NDX/SPX  -> GEX-Level, GEX-Landschaft, Vol-Surface (15 Min verzögert)
  Yahoo       NQ, ES, VIX, Zinsen, Dollar, Mag 7 (ca. 10–15 Min verzögert)
  ForexFactory Wirtschaftskalender
  RSS         Headlines (CNBC, MarketWatch)

  python fetch_data.py              -> Update
  python fetch_data.py --telegram   -> Update + Telegram (braucht TG_TOKEN, TG_CHAT als Umgebungsvariablen)
"""
import datetime as dt
import email.utils
import json
import math
import os
import pathlib
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ET_TZ = ZoneInfo("America/New_York")
BER = ZoneInfo("Europe/Berlin")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
OUT = pathlib.Path(__file__).with_name("data") / "latest.json"
PAT = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
NOW = dt.datetime.now(dt.timezone.utc)


def get(url, as_json=True):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if as_json else raw


def safe(fn, default=None, label=""):
    try:
        return fn()
    except Exception as e:
        print(f"[warn] {label}: {e}")
        return default


# ───────────────────────── Optionen / GEX ─────────────────────────
def load_options(sym):
    d = get(f"https://cdn.cboe.com/api/global/delayed_quotes/options/_{sym}.json")["data"]
    spot = float(d.get("current_price") or d.get("close"))
    rows = []
    for o in d["options"]:
        m = PAT.match(o.get("option", ""))
        if not m:
            continue
        _, ymd, cp, k = m.groups()
        exp = dt.datetime.strptime(ymd, "%y%m%d").replace(hour=21, tzinfo=dt.timezone.utc)
        T = (exp - NOW).total_seconds() / (365 * 24 * 3600)
        if T <= 0:
            continue
        iv = float(o.get("iv") or 0)
        if iv <= 0 or iv > 3:
            continue
        rows.append({"K": float(k) / 1000, "cp": cp, "T": max(T, 1 / (365 * 24)),
                     "days": (exp - NOW).total_seconds() / 86400, "iv": iv,
                     "oi": float(o.get("open_interest") or 0)})
    return spot, rows


def bs_gamma(S, K, T, iv):
    sq = iv * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * iv * iv * T) / sq
    return math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (S * sq)


def gex_of(S, r):
    g = bs_gamma(S, r["K"], r["T"], r["iv"]) * r["oi"] * 100 * S * S * 0.01
    return g if r["cp"] == "C" else -g


def gex_levels(spot, rows, rng=0.06, extra=4):
    rows = [r for r in rows if r["days"] <= 45 and r["oi"] > 0]
    lo, hi = spot * (1 - rng), spot * (1 + rng)
    calls, puts = {}, {}
    for r in rows:
        v = gex_of(spot, r)
        tgt = calls if r["cp"] == "C" else puts
        tgt[r["K"]] = tgt.get(r["K"], 0) + v
    strikes = [k for k in set(calls) | set(puts) if lo <= k <= hi]
    net = {k: calls.get(k, 0) + puts.get(k, 0) for k in strikes}
    above = [k for k in strikes if k >= spot] or strikes
    below = [k for k in strikes if k < spot] or strikes
    cw = max(above, key=lambda k: calls.get(k, 0))
    pw = min(below, key=lambda k: puts.get(k, 0))
    out = {cw: "CW"}
    out.setdefault(pw, "PW")
    steps = 160
    grid = [lo + (hi - lo) * i / steps for i in range(steps + 1)]
    tot = [sum(gex_of(S, r) for r in rows) for S in grid]
    flips = []
    for i in range(steps):
        a, b = tot[i], tot[i + 1]
        if (a < 0) != (b < 0):
            flips.append(grid[i] + (grid[i + 1] - grid[i]) * (a / (a - b)))
    zg = round(min(flips, key=lambda x: abs(x - spot))) if flips else None
    for k in sorted(net, key=lambda k: -abs(net[k])):
        if sum(1 for v in out.values() if v.startswith("G")) >= extra:
            break
        if k not in out:
            out[k] = "G+" if net[k] > 0 else "G-"
    s = ",".join(f"{k:g}={t}" for k, t in sorted(out.items()))
    if zg:
        s += f",{zg}=ZG"
    return {"spot": spot, "total_bn": sum(net.values()) / 1e9, "cw": cw, "pw": pw, "zg": zg,
            "tags": {str(k): t for k, t in out.items()}, "string": s,
            "flip_curve": {"x": grid, "y": [t / 1e9 for t in tot]}}


def gex_landscape(spot, rows, ratio):
    """GEX nach Strike (x) und Laufzeit (y) – für die 3D-Landschaft."""
    buckets = [(0, 1.5, "0–1 T"), (1.5, 7, "2–7 T"), (7, 14, "1–2 W"), (14, 30, "2–4 W"), (30, 45, "1–1,5 M")]
    width = spot * 0.0025
    lo, hi = spot * 0.95, spot * 1.05
    nb = int((hi - lo) / width)
    z = [[0.0] * nb for _ in buckets]
    for r in rows:
        if r["oi"] <= 0 or not (lo <= r["K"] < hi):
            continue
        for bi, (a, b, _) in enumerate(buckets):
            if a <= r["days"] < b:
                z[bi][int((r["K"] - lo) / width)] += gex_of(spot, r) / 1e9
    x = [round((lo + width * (i + 0.5)) * ratio, 0) for i in range(nb)]
    return {"x": x, "y": [b[2] for b in buckets], "z": [[round(v, 3) for v in row] for row in z]}


def vol_surface(spot, rows):
    """Implizite Vola nach Moneyness (x) und Laufzeit (y) – die Regenbogen-Fläche."""
    mgrid = [i / 100 for i in range(-10, 11)]
    exps = {}
    for r in rows:
        if r["days"] > 120:
            continue
        m = r["K"] / spot - 1
        if abs(m) > 0.12:
            continue
        if (r["cp"] == "P" and m <= 0) or (r["cp"] == "C" and m >= 0):  # nur OTM
            exps.setdefault(round(r["days"], 1), []).append((m, r["iv"]))
    days = sorted(d for d, pts in exps.items() if len(pts) >= 8)
    if len(days) > 14:  # gleichmäßig ausdünnen
        days = [days[round(i * (len(days) - 1) / 13)] for i in range(14)]
    z = []
    for d in days:
        pts = sorted(exps[d])
        row = []
        for m in mgrid:
            v = None
            for (m1, v1), (m2, v2) in zip(pts, pts[1:]):
                if m1 <= m <= m2:
                    v = v1 if m2 == m1 else v1 + (v2 - v1) * (m - m1) / (m2 - m1)
                    break
            row.append(round(v * 100, 2) if v else None)
        z.append(row)

    def iv_at(d_target, m_target):
        if not days:
            return None
        d = min(days, key=lambda x: abs(x - d_target))
        row = z[days.index(d)]
        return row[mgrid.index(m_target)]

    front = next((d for d in days if d >= 1), days[0] if days else None)
    atm_front = iv_at(front, 0.0) if front else None
    atm_30 = iv_at(30, 0.0)
    skew = (iv_at(30, -0.05) or 0) - (iv_at(30, 0.05) or 0)
    return {"x": [round(m * 100) for m in mgrid], "y": [round(d) for d in days], "z": z,
            "atm_front": atm_front, "atm_30": atm_30, "skew": round(skew, 2)}


# ───────────────────────── Kurse (Yahoo) ─────────────────────────
def yahoo(sym, interval="1d", rng="5d"):
    u = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?interval={interval}&range={rng}"
    return get(u)["chart"]["result"][0]


def quote(sym, name):
    r = yahoo(sym)
    m = r["meta"]
    last = m.get("regularMarketPrice")
    prev = m.get("chartPreviousClose") or m.get("previousClose")
    closes = [c for c in r["indicators"]["quote"][0]["close"] if c is not None]
    if len(closes) >= 2:
        prev = closes[-2]
    return {"sym": sym, "name": name, "last": last, "chg_pct": (last / prev - 1) * 100 if prev else 0}


def nq_candles():
    r = yahoo("NQ=F", "5m", "5d")
    q = r["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(r["timestamp"]):
        o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        if None in (o, h, l, c):
            continue
        out.append({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v or 0})
    return out


def session_levels(candles):
    """Vortag (RTH 9:30–16:00 NY): High, Low, Close, VWAP + Overnight-High/Low."""
    by_day = {}
    for c in candles:
        t = dt.datetime.fromtimestamp(c["time"], ET_TZ)
        if dt.time(9, 30) <= t.time() < dt.time(16, 0):
            by_day.setdefault(t.date(), []).append(c)
    now_et = NOW.astimezone(ET_TZ)
    done = [d for d, cs in by_day.items()
            if dt.datetime.fromtimestamp(cs[-1]["time"], ET_TZ).time() >= dt.time(15, 50)
            and not (d == now_et.date() and now_et.time() < dt.time(16, 0))]
    if not done:
        return {}
    pd_ = max(done)
    cs = by_day[pd_]
    vol = sum(c["volume"] for c in cs) or 1
    vwap = sum((c["high"] + c["low"] + c["close"]) / 3 * c["volume"] for c in cs) / vol
    end = dt.datetime.combine(pd_, dt.time(16, 0), ET_TZ).timestamp()
    nxt = [d for d in by_day if d > pd_]
    stop = dt.datetime.combine(min(nxt), dt.time(9, 30), ET_TZ).timestamp() if nxt else 1e12
    on = [c for c in candles if end <= c["time"] < stop]
    lv = {"PDH": max(c["high"] for c in cs), "PDL": min(c["low"] for c in cs),
          "PDC": cs[-1]["close"], "pVWAP": vwap, "date": str(pd_)}
    if on:
        lv["ONH"] = max(c["high"] for c in on)
        lv["ONL"] = min(c["low"] for c in on)
    return lv


# ───────────────────────── News ─────────────────────────
def calendar():
    data = get("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    today = NOW.astimezone(BER).date()
    out = []
    for e in data:
        if e.get("country") != "USD" or e.get("impact") not in ("High", "Medium"):
            continue
        t = dt.datetime.fromisoformat(e["date"])
        if t.astimezone(BER).date() < today:
            continue
        out.append({"time": t.isoformat(), "title": e["title"], "impact": e["impact"],
                    "forecast": e.get("forecast", ""), "previous": e.get("previous", "")})
    return sorted(out, key=lambda x: x["time"])[:14]


FEEDS = [("CNBC", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
         ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
         ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories")]


def headlines():
    items, seen = [], set()
    for src, url in FEEDS:
        root = safe(lambda: ET.fromstring(get(url, as_json=False)), None, src)
        if root is None:
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            pub = it.findtext("pubDate")
            t = safe(lambda: email.utils.parsedate_to_datetime(pub).astimezone(dt.timezone.utc), None, "date")
            items.append({"title": title, "source": src, "link": it.findtext("link"),
                          "time": t.isoformat() if t else None})
    items.sort(key=lambda x: x["time"] or "", reverse=True)
    return items[:15]


# ───────────────────────── Bias ─────────────────────────
def daily_bias(nq_last, lv, gex_nq, market, mag7):
    score, reasons = 0, []

    def add(pts, txt):
        nonlocal score
        score += pts
        reasons.append({"pts": pts, "text": txt})

    if gex_nq and gex_nq.get("zg"):
        if nq_last > gex_nq["zg"]:
            add(1, "NQ über Zero Gamma → Dealer dämpfen Bewegungen, Dips werden eher gekauft")
        else:
            add(-1, "NQ unter Zero Gamma → Dealer verstärken Bewegungen, Abverkäufe laufen leichter")
    if lv.get("pVWAP"):
        if nq_last > lv["pVWAP"]:
            add(1, f"Über dem Vortags-VWAP ({lv['pVWAP']:.0f}) → Käufer haben Kontrolle")
        else:
            add(-1, f"Unter dem Vortags-VWAP ({lv['pVWAP']:.0f}) → Verkäufer haben Kontrolle")
    if lv.get("PDH") and nq_last > lv["PDH"]:
        add(1, "Über dem Vortageshoch → Ausbruch nach oben")
    elif lv.get("PDL") and nq_last < lv["PDL"]:
        add(-1, "Unter dem Vortagestief → Ausbruch nach unten")
    m = {x["sym"]: x for x in market}
    vix = m.get("^VIX")
    if vix:
        if vix["chg_pct"] > 3:
            add(-1, f"VIX steigt ({vix['chg_pct']:+.1f} %) → mehr Angst im Markt")
        elif vix["chg_pct"] < -3:
            add(1, f"VIX fällt ({vix['chg_pct']:+.1f} %) → Risk-on")
    tnx = m.get("^TNX")
    if tnx and abs(tnx["chg_pct"]) > 1:
        add(-1 if tnx["chg_pct"] > 0 else 1,
            f"US-Zinsen {'steigen' if tnx['chg_pct'] > 0 else 'fallen'} ({tnx['chg_pct']:+.1f} %) → "
            f"{'Gegenwind' if tnx['chg_pct'] > 0 else 'Rückenwind'} für Tech")
    if mag7:
        up = sum(1 for x in mag7 if x["chg_pct"] > 0)
        if up >= 5:
            add(1, f"{up}/7 Mag-7-Aktien im Plus → breite Stärke")
        elif up <= 2:
            add(-1, f"Nur {up}/7 Mag-7-Aktien im Plus → Schwäche in den Schwergewichten")
    label = "BULLISH" if score >= 2 else "BEARISH" if score <= -2 else "NEUTRAL"
    return {"label": label, "score": score, "reasons": reasons}


# ───────────────────────── Erklärungen ─────────────────────────
def explain_vol(v):
    if not v or not v["y"]:
        return []
    out = []
    if v["atm_front"] and v["atm_30"]:
        if v["atm_front"] > v["atm_30"] + 1:
            out.append(f"Kurzfristige Optionen sind teurer als langfristige ({v['atm_front']:.1f} % vs. {v['atm_30']:.1f} %). "
                       "Der Markt erwartet <b>bald</b> eine große Bewegung, z. B. wegen News. Vorsicht, Stress-Signal.")
        else:
            out.append(f"Normale Struktur: kurzfristig {v['atm_front']:.1f} %, 30 Tage {v['atm_30']:.1f} %. "
                       "Kein akuter Stress erwartet.")
    if v["skew"] > 4:
        out.append(f"Puts sind deutlich teurer als Calls (Skew {v['skew']:.1f}). Große Spieler sichern sich stark nach "
                   "<b>unten</b> ab. Abverkäufe können schnell gehen.")
    elif v["skew"] > 1.5:
        out.append(f"Normaler Skew ({v['skew']:.1f}): Puts etwas teurer als Calls, das ist der Normalzustand.")
    else:
        out.append(f"Sehr flacher Skew ({v['skew']:.1f}): kaum Absicherung nach unten, der Markt ist sorglos. "
                   "Das kippt manchmal plötzlich.")
    return out


def explain_gex(g, ratio):
    if not g:
        return []
    out = []
    if g["total_bn"] > 0:
        out.append(f"Netto-GEX positiv ({g['total_bn']:+.1f} Mrd $): Dealer handeln <b>gegen</b> den Markt "
                   "(kaufen Dips, verkaufen Rallys). Level halten eher, Range-Tag wahrscheinlicher.")
    else:
        out.append(f"Netto-GEX negativ ({g['total_bn']:+.1f} Mrd $): Dealer handeln <b>mit</b> dem Markt. "
                   "Bewegungen werden verstärkt, Level brechen eher, Trend-Tag wahrscheinlicher.")
    out.append(f"Call Wall {g['cw'] * ratio:.0f} wirkt oft als Deckel nach oben, Put Wall {g['pw'] * ratio:.0f} als Boden.")
    return out


# ───────────────────────── Main ─────────────────────────
def main():
    data = {"updated": NOW.isoformat()}

    candles = safe(nq_candles, [], "NQ")
    nq_last = candles[-1]["close"] if candles else None
    data["nq"] = {"candles": candles[-500:], "last": nq_last}
    lv = safe(lambda: session_levels(candles), {}, "Session-Level") if candles else {}

    market = [x for x in (safe(lambda s=s, n=n: quote(s, n), None, s) for s, n in [
        ("NQ=F", "Nasdaq Fut."), ("ES=F", "S&P Fut."), ("^VIX", "VIX"), ("^TNX", "US 10J Zins"),
        ("DX-Y.NYB", "Dollar-Index"), ("CL=F", "Öl"), ("GC=F", "Gold"), ("BTC-USD", "Bitcoin")]) if x]
    mag7 = [x for x in (safe(lambda s=s: quote(s, s), None, s) for s in
                        ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"]) if x]
    data["market"], data["mag7"] = market, mag7

    ndx = safe(lambda: load_options("NDX"), None, "NDX")
    spx = safe(lambda: load_options("SPX"), None, "SPX")
    ratio = nq_last / ndx[0] if (ndx and nq_last) else 1.0
    g_ndx = safe(lambda: gex_levels(*ndx), None, "GEX NDX") if ndx else None
    g_spx = safe(lambda: gex_levels(*spx), None, "GEX SPX") if spx else None
    gex_nq = None
    if g_ndx:
        gex_nq = {"cw": g_ndx["cw"] * ratio, "pw": g_ndx["pw"] * ratio,
                  "zg": g_ndx["zg"] * ratio if g_ndx["zg"] else None}
        data["gex"] = {"total_bn": g_ndx["total_bn"], "ratio": ratio, "ndx_spot": g_ndx["spot"],
                       "levels": [{"price": float(k) * ratio, "tag": t} for k, t in g_ndx["tags"].items()]
                       + ([{"price": gex_nq["zg"], "tag": "ZG"}] if gex_nq["zg"] else []),
                       "landscape": safe(lambda: gex_landscape(ndx[0], ndx[1], ratio), None, "Landschaft"),
                       "explain": explain_gex(g_ndx, ratio)}
    if ndx:
        v = safe(lambda: vol_surface(*ndx), None, "Vol-Surface")
        data["vol"] = v
        data["vol_explain"] = explain_vol(v)
    data["gex_string"] = "|".join(f"{n}:{g['string']}" for n, g in (("SPX", g_spx), ("NDX", g_ndx)) if g)
    data["session"] = lv
    data["calendar"] = safe(calendar, [], "Kalender")
    data["news"] = safe(headlines, [], "News")
    data["bias"] = daily_bias(nq_last, lv, gex_nq, market, mag7) if nq_last else None

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"OK – {OUT} geschrieben")

    if "--telegram" in sys.argv:
        send_telegram(data, gex_nq)


def send_telegram(d, gex_nq):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not (token and chat):
        print("Telegram übersprungen (TG_TOKEN/TG_CHAT fehlen)")
        return
    b = d.get("bias") or {}
    icon = {"BULLISH": "🟢", "BEARISH": "🔴"}.get(b.get("label"), "⚪")
    lines = [f"📊 <b>BR Terminal</b> · {NOW.astimezone(BER):%d.%m. %H:%M}",
             f"{icon} Daily Bias: <b>{b.get('label', '–')}</b>"]
    lines += [f"• {r['text']}" for r in b.get("reasons", [])[:4]]
    if gex_nq:
        g = d["gex"]
        lines.append(f"\n<b>NQ-Level</b> (GEX {'🟢 positiv' if g['total_bn'] > 0 else '🔴 negativ'})")
        lines.append(f"Call Wall {gex_nq['cw']:.0f} · Put Wall {gex_nq['pw']:.0f}"
                     + (f" · Zero Gamma {gex_nq['zg']:.0f}" if gex_nq["zg"] else ""))
    hi = [e for e in d.get("calendar", []) if e["impact"] == "High"
          and dt.datetime.fromisoformat(e["time"]).astimezone(BER).date() == NOW.astimezone(BER).date()]
    if hi:
        lines.append("\n<b>⚠️ News heute</b>")
        lines += [f"{dt.datetime.fromisoformat(e['time']).astimezone(BER):%H:%M} {e['title']}" for e in hi]
    lines.append(f"\nFür TradingView antippen:\n<code>{d['gex_string']}</code>")
    body = urllib.parse.urlencode({"chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=30)
    print("Telegram gesendet")


if __name__ == "__main__":
    main()
