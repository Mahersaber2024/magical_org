import asyncio
import html
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv

load_dotenv()
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tehran")
logger = logging.getLogger(__name__)


# ====================== Formatting (Jalali date, prices, R values) ======================

_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
           "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
_DAYS = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]


def to_fa(s) -> str:
    return str(s).translate(_FA)


def g2j(gy: int, gm: int, gd: int):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100
            + (gy2 + 399) // 400 + gd + g_d_m[gm - 1])
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def fa_date(dt: datetime = None) -> str:
    dt = dt or datetime.now(ZoneInfo(TIMEZONE))
    jy, jm, jd = g2j(dt.year, dt.month, dt.day)
    return f"{_DAYS[dt.weekday()]} {to_fa(jd)} {_MONTHS[jm - 1]} {to_fa(jy)}"


def fmt_price(x: float) -> str:
    s = f"{x:,.4f}" if abs(x) >= 1 else f"{x:.8f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def fmt_rr(rr: float) -> str:
    if abs(rr - round(rr)) < 0.005:
        return f"1:{int(round(rr))}"
    return "1:" + f"{rr:.2f}".rstrip("0").rstrip(".")


def fmt_r(r: float) -> str:
    if abs(r) < 0.005:
        return "0R"
    return f"{r:+.2f}".rstrip("0").rstrip(".") + "R"


def fmt_step(s: float) -> str:
    return (f"{s:.2f}".rstrip("0").rstrip(".")) + "R"


# ====================== Signal parser ======================

_NUM = r"([\d][\d.,'’_ ]*)"
_ENTRY = re.compile(r"^(?:int|entry|ent|en)\s*[:=]?\s*" + _NUM + "$", re.I)
_TP = re.compile(r"^(?:tp|take\s*profit)\s*[:=]?\s*" + _NUM + "$", re.I)
_SL = re.compile(r"^(?:sl|stop(?:\s*loss)?)\s*[:=]?\s*" + _NUM + "$", re.I)

# Persian / Arabic-Indic digits and separators -> ASCII
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫٬", "01234567890123456789.,")
_JUNK = str.maketrans("", "", "`*$_~|\u200c\u200f\u200e\u00a0")

QUOTES = ("USDT", "USDC", "BUSD", "FDUSD")


def _num(s: str) -> float:
    """Parse prices written as 83865.2 / 83,865.20 / 83.865,20 / 83 865.20 / 0,5."""
    s = re.sub(r"[\s'’_]", "", s).strip(".,")
    has_c, has_d = "," in s, "." in s
    if has_c and has_d:
        # the separator that appears last is the decimal point
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_c:
        # 83,865 / 1,234,567 -> thousands ; 0,5 / 83,5 -> decimal comma
        if re.fullmatch(r"\d{1,3}(,\d{3})+", s):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    elif has_d and s.count(".") > 1:
        s = s.replace(".", "")  # 1.234.567
    return float(s)


def normalize_pair(raw: str):
    s = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    s = s.replace("PERP", "")
    if not s:
        return None, None
    for q in QUOTES:
        if s.endswith(q) and len(s) > len(q):
            return s, s[: -len(q)]
    return s + "USDT", s


def parse_signal(text: str):
    text = text.translate(_DIGITS).translate(_JUNK)
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    symbol = entry = tp = sl = None
    for line in lines:
        try:
            if m := _ENTRY.match(line):
                entry = _num(m.group(1))
            elif m := _TP.match(line):
                tp = _num(m.group(1))
            elif m := _SL.match(line):
                sl = _num(m.group(1))
            elif symbol is None:
                symbol = line
        except ValueError:
            return "عدد نامعتبر در خط: " + line
    if not symbol or entry is None or tp is None or sl is None:
        return "فرمت درست نیست. باید نماد، Int، Tp و Sl را بفرستی."
    pair, base = normalize_pair(symbol)
    if not pair:
        return "نماد نامعتبر است."
    if tp > entry > sl:
        side = "LONG"
    elif tp < entry < sl:
        side = "SHORT"
    else:
        return "ترتیب قیمت‌ها منطقی نیست (LONG: SL<Entry<TP | SHORT: TP<Entry<SL)."
    risk = abs(entry - sl)
    rr = round(abs(tp - entry) / risk, 2)
    return {"pair": pair, "symbol": base, "side": side,
            "entry": entry, "tp": tp, "sl": sl, "rr": rr}


# ====================== Live prices (Binance / Bybit public API) ======================

logger = logging.getLogger(__name__)

_PROVIDERS = [
    ("https://api.binance.com/api/v3/ticker/price", lambda j: j["price"]),
    ("https://fapi.binance.com/fapi/v1/ticker/price", lambda j: j["price"]),
    ("https://api.bybit.com/v5/market/tickers?category=linear",
     lambda j: j["result"]["list"][0]["lastPrice"]),
    ("https://api.bybit.com/v5/market/tickers?category=spot",
     lambda j: j["result"]["list"][0]["lastPrice"]),
]

_session = None


async def _sess():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6))
    return _session


async def get_price(pair: str):
    s = await _sess()
    for url, pick in _PROVIDERS:
        full = url + ("&" if "?" in url else "?") + f"symbol={pair}"
        try:
            async with s.get(full) as r:
                if r.status != 200:
                    continue
                return float(pick(await r.json()))
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, IndexError, ValueError):
            continue
    logger.warning("no price for %s", pair)
    return None


# ====================== Channel post templates ======================

e = html.escape

LINE = "──────────────"


def _head(t):
    return f"{e(t['symbol'])} {t['side']}"


def order_type(side: str, entry: float, price: float) -> str:
    """'limit' if Entry is on the better side of the market, else 'stop'."""
    if side == "LONG":
        return "limit" if entry <= price else "stop"
    return "limit" if entry >= price else "stop"


def _pending_kind(t):
    """(emoji, label): blue = Limit, yellow = Stop. Old trades without a type stay yellow."""
    kind = t.get("order")
    if kind not in ("limit", "stop"):
        return "🟡", "Pending"
    emoji = "🔵" if kind == "limit" else "🟡"
    side = "Buy" if t["side"] == "LONG" else "Sell"
    return emoji, f"Pending {side} {kind.capitalize()}"


def pending_text(t):
    emoji, label = _pending_kind(t)
    return (
        f"{emoji} <b>{_head(t)}</b>  ·  {label}\n"
        f"{LINE}\n"
        f"Entry   {fmt_price(t['entry'])}\n"
        f"TP   {fmt_price(t['tp'])}\n"
        f"SL   {fmt_price(t['sl'])}\n"
        f"R/R   {fmt_rr(t['rr'])}\n"
        f"{LINE}\n"
        f"{fa_date()}"
    )


# Replies carry no date: they are short status lines under the original post.

def open_text(t):
    return f"🟢 Position {t['no']}  ·  Opened"


def cancel_text(t):
    return "⚪️ Pending order cancelled"


def reward_text(t, step):
    return f"🟢 Position {t['no']}  ·  Reward {fmt_step(step)}  ·  In Profit"


def tp_text(t):
    return f"🟢 Position {t['no']}  ·  Take Profit  ·  <b>{fmt_r(t['result_r'])}</b>"


def sl_text(t):
    return f"🔴 Position {t['no']}  ·  Stop Loss  ·  <b>{fmt_r(t['result_r'])}</b>"


def be_text(t):
    return f"⚪️ Position {t['no']}  ·  Break-even  ·  <b>0R</b>"


def be_set_text(t):
    return f"⚪️ Position {t['no']}  ·  Stop moved to Entry  ·  Risk-free"


def manual_text(t):
    return f"⚪️ Position {t['no']}  ·  Closed manually  ·  <b>{fmt_r(t['result_r'])}</b>"


def final_text(t):
    return {"tp": tp_text, "sl": sl_text, "be": be_text, "manual": manual_text}[t["outcome"]](t)


HEAVY = "━━━━━━━━━━━━━━"


def _bar(pct: float, n: int = 10) -> str:
    k = max(0, min(n, round(pct / 100 * n)))
    return "▰" * k + "▱" * (n - k)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def summary_text(trades, title: str = "Performance Report", limit: int = 40):
    rows = sorted(trades, key=lambda x: x["id"])
    n = len(rows)
    total = 0.0
    wins = losses = be = 0
    counts = {}
    rs = []
    lines = []
    for t in rows:
        r = t["result_r"] or 0.0
        rs.append(r)
        total += r
        if r > 0.005:
            wins += 1
            mark = "🟢"
        elif r < -0.005:
            losses += 1
            mark = "🔴"
        else:
            be += 1
            mark = "⚪️"
        for st in t["rewards_hit"]:
            counts[st] = counts.get(st, 0) + 1
        if len(lines) < limit:
            hit = " · ".join(fmt_step(st) for st in sorted(t["rewards_hit"]))
            extra = f"   🏆 {hit}" if hit else ""
            no = t.get("no", t["id"])
            lines.append(f"{mark} <code>#{no}</code> {e(t['symbol'])} {t['side']}  ·  <b>{fmt_r(r)}</b>{extra}")
    if n > limit:
        lines.append(f"… +{n - limit} more")

    head = "🔥" if total > 0.005 else ("📉" if total < -0.005 else "⚖️")
    wr = wins / n * 100
    out = [
        f"📊 <b>{e(title.upper())}</b>",
        f"<i>{fa_date()}</i>",
        HEAVY,
        "<blockquote>" + "\n".join(lines) + "</blockquote>",
        HEAVY,
        f"{head} Net Result   <b>{fmt_r(total)}</b>",
        f"🎯 Win Rate   {_bar(wr)}  <b>{round(wr)}%</b>",
        f"✅ {_plural(wins, 'Win', 'Wins')}  ·  ❌ {_plural(losses, 'Loss', 'Losses')}  ·  ➖ {be} BE",
        f"📈 Avg {fmt_r(total / n)}  ·  🥇 Best {fmt_r(max(rs))}  ·  🥀 Worst {fmt_r(min(rs))}",
    ]
    if counts:
        rw = " · ".join(f"{fmt_step(st)}×{c}" for st, c in sorted(counts.items()))
        out.append(f"🎁 Rewards   {rw}")
    out.append(HEAVY)
    return "\n".join(out)
