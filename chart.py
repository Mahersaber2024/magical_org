"""chart.py: TradingView-style entry snapshot (candles + Long/Short position box).

No TradingView account, no browser: candles come from the public Binance API
(Bybit fallback) and the image is drawn with matplotlib, all on the server.
"""
import asyncio
import io
import logging
from datetime import datetime, timezone

import aiohttp
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, Rectangle
from matplotlib.offsetbox import AnchoredOffsetbox, DrawingArea, HPacker, TextArea
import matplotlib.dates as mdates

log = logging.getLogger(__name__)

TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h"]
_BYBIT_TF = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240"}
_TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}

# TradingView light theme colours
UP, DOWN = "#089981", "#F23645"
TP_FILL, SL_FILL = "#089981", "#F23645"
GRID, TEXT, BG = "#F0F3FA", "#787B86", "#FFFFFF"
VISIBLE = 50  # candles shown on the chart (zoom level: smaller = more zoomed in)


# name, icon colour, icon glyph (TradingView-like legend)
COINS = {
    "BTC": ("Bitcoin", "#F7931A", "B"), "ETH": ("Ethereum", "#627EEA", "Ξ"),
    "BNB": ("BNB", "#F3BA2F", "B"), "SOL": ("Solana", "#9945FF", "S"),
    "XRP": ("XRP", "#23292F", "X"), "DOGE": ("Dogecoin", "#C2A633", "D"),
    "ADA": ("Cardano", "#0033AD", "A"), "TON": ("Toncoin", "#0098EA", "T"),
    "TRX": ("TRON", "#EB0029", "T"), "AVAX": ("Avalanche", "#E84142", "A"),
    "LINK": ("Chainlink", "#2A5ADA", "L"), "DOT": ("Polkadot", "#E6007A", "P"),
    "LTC": ("Litecoin", "#345D9D", "Ł"), "BCH": ("Bitcoin Cash", "#8DC351", "B"),
    "ETC": ("Ethereum Classic", "#328332", "Ξ"), "SHIB": ("Shiba Inu", "#E42D04", "S"),
    "PEPE": ("Pepe", "#4C9540", "P"), "SUI": ("Sui", "#4DA2FF", "S"),
    "ARB": ("Arbitrum", "#28A0F0", "A"), "OP": ("Optimism", "#FF0420", "O"),
    "NEAR": ("NEAR Protocol", "#000000", "N"), "ATOM": ("Cosmos", "#2E3148", "A"),
    "XLM": ("Stellar", "#14B6E7", "S"), "APT": ("Aptos", "#06F7F7", "A"),
    "FIL": ("Filecoin", "#0090FF", "F"), "INJ": ("Injective", "#0082FA", "I"),
    "WIF": ("dogwifhat", "#A8805C", "W"), "NOT": ("Notcoin", "#000000", "N"),
    "MATIC": ("Polygon", "#8247E5", "P"), "POL": ("Polygon", "#8247E5", "P"),
    "UNI": ("Uniswap", "#FF007A", "U"), "AAVE": ("Aave", "#B6509E", "A"),
    "XAU": ("Gold", "#D4AF37", "G"), "PAXG": ("PAX Gold", "#E4CE4D", "G"),
}
QUOTES = {"USDT": "TetherUS", "USDC": "USD Coin", "FDUSD": "First Digital USD",
          "USD": "U.S. Dollar", "BTC": "Bitcoin", "ETH": "Ethereum"}
TF_LABEL = {"1m": "1", "5m": "5", "15m": "15", "1h": "1h", "4h": "4h"}


def _split_pair(pair: str):
    for q in sorted(QUOTES, key=len, reverse=True):
        if pair.endswith(q) and len(pair) > len(q):
            return pair[:-len(q)], q
    return pair, ""


def _legend(ax, pair: str, tf: str, source: str, last):
    """'(icon) Bitcoin / TetherUS · 15 · BINANCE  ●  O… H… L… C…' in the top-left corner."""
    base, quote = _split_pair(pair)
    name, colour, glyph = COINS.get(base, (base, "#787B86", base[:1]))
    da = DrawingArea(18, 18, 0, 0)
    da.add_artist(Circle((9, 9), 9, fc=colour, ec="none"))
    da.add_artist(plt.Text(9, 8.5, glyph, ha="center", va="center", color="white",
                           fontsize=10, fontweight="bold"))
    dot = DrawingArea(16, 16, 0, 0)
    dot.add_artist(Circle((8, 8), 7.5, fc="#089981", alpha=0.18, ec="none"))
    dot.add_artist(Circle((8, 8), 4, fc="#22AB94", ec="none"))
    title = f"{name} / {QUOTES.get(quote, quote)}" if quote else name
    title += f" · {TF_LABEL.get(tf, tf)} · {source}"
    _, o, h, l, c = last
    vcol = UP if c >= o else DOWN
    parts = [da, TextArea(title, textprops=dict(fontsize=12.5, color="#131722")), dot]
    for k, v in (("O", o), ("H", h), ("L", l), ("C", c)):
        parts.append(HPacker(children=[
            TextArea(k, textprops=dict(fontsize=11, color="#131722")),
            TextArea(_fmt(v), textprops=dict(fontsize=11, color=vcol))], pad=0, sep=1))
    box = AnchoredOffsetbox(loc="upper left", child=HPacker(children=parts, align="center",
                                                            pad=0, sep=7),
                            frameon=False, pad=0.3, borderpad=0.9)
    ax.add_artist(box)


# ---------------------------------------------------------------- data
async def get_klines(session: aiohttp.ClientSession, pair: str, tf: str = "15m", limit: int = 90):
    """(source, [(open_time_utc, o, h, l, c), ...]). Binance spot -> futures -> Bybit."""
    for base in ("https://api.binance.com/api/v3/klines", "https://fapi.binance.com/fapi/v1/klines"):
        try:
            async with session.get(base, params={"symbol": pair, "interval": tf, "limit": limit}) as r:
                if r.status == 200:
                    j = await r.json()
                    if j:
                        return "BINANCE", [(datetime.fromtimestamp(k[0] / 1000, timezone.utc),
                                 float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in j]
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, IndexError):
            pass
    for cat in ("linear", "spot"):
        try:
            async with session.get("https://api.bybit.com/v5/market/kline",
                                   params={"category": cat, "symbol": pair,
                                           "interval": _BYBIT_TF.get(tf, "15"), "limit": limit}) as r:
                if r.status == 200:
                    rows = (await r.json())["result"]["list"]
                    if rows:
                        return "BYBIT", [(datetime.fromtimestamp(int(k[0]) / 1000, timezone.utc),
                                 float(k[1]), float(k[2]), float(k[3]), float(k[4]))
                                for k in reversed(rows)]
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, IndexError):
            pass
    log.warning("no klines for %s", pair)
    return None, None


# ---------------------------------------------------------------- drawing
def _fmt(x: float) -> str:
    if x >= 1000:
        return f"{x:,.1f}"
    if x >= 1:
        return f"{x:,.4f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")


def _tag(ax, y, text, color):
    """Coloured price tag on the right axis, like TradingView."""
    ax.annotate(text, xy=(1, y), xycoords=("axes fraction", "data"), xytext=(4, 0),
                textcoords="offset points", va="center", ha="left", fontsize=10, color="white",
                fontweight="bold", annotation_clip=False,
                bbox=dict(boxstyle="square,pad=0.3", fc=color, ec=color))


def _place_tags(ax, fig, tags):
    """Right-axis price tags that never overlap: sorted top to bottom, each one is pushed
    down just enough to clear the one above (the price tag also holds the countdown)."""
    H, CD, GAP = 21, 17, 2  # tag height, countdown height, gap (pixels)
    pt = 72 / fig.dpi
    items = []
    for y, text, color, cd in tags:
        py = ax.transData.transform((0, y))[1]
        items.append([py, py, text, color, cd, y])
    items.sort(key=lambda it: -it[0])
    floor = None  # lowest pixel used so far
    for it in items:
        top = it[1] + H / 2
        if floor is not None and top > floor - GAP:
            it[1] -= top - (floor - GAP)
        floor = it[1] - H / 2 - (CD if it[4] else 0)
    for py0, py, text, color, cd, y in items:
        off = (py - py0) * pt
        kw = dict(xy=(1, y), xycoords=("axes fraction", "data"), textcoords="offset points",
                  va="center", ha="left", color="white", annotation_clip=False)
        ax.annotate(text, xytext=(4, off), fontsize=10, fontweight="bold",
                    bbox=dict(boxstyle="square,pad=0.3", fc=color, ec=color), **kw)
        if cd:
            ax.annotate(cd, xytext=(4, off - CD * pt), fontsize=9.5,
                        bbox=dict(boxstyle="square,pad=0.3", fc=color, ec=color), **kw)


def _label(ax, x, y, text, color, va):
    ax.text(x, y, text, ha="center", va=va, fontsize=9.5, color="white", fontweight="bold",
            zorder=6, bbox=dict(boxstyle="round,pad=0.35", fc=color, ec="white", lw=1))


def render(t: dict, candles: list, tf: str = "15m", price: float = None, brand: str = "",
           start: datetime = None, badge: tuple = None, source: str = "BINANCE",
           labels: bool = False, ahead: int = 0) -> bytes:
    """PNG bytes of the chart. The position box starts at the candle of `start`
    (activation / signal time; default = last candle). `badge` = (text, colour) top-right.
    `ahead` > 0 (pending orders): the box starts that many candles AFTER the last candle,
    in empty space, so it is clear price has not reached Entry yet (`start` is ignored)."""
    side, entry, tp, sl = t["side"], float(t["entry"]), float(t["tp"]), float(t["sl"])
    price = float(price if price is not None else candles[-1][4])
    n = len(candles)
    w = 0.62  # candle body width in index units
    x0 = n - 1
    if start is not None:
        x0 = 0
        for i, c in enumerate(candles):
            if c[0] <= start:
                x0 = i
    # zoom: show only the last VISIBLE candles (more if the box starts earlier, so the
    # activation candle and a few candles before it always stay on the chart)
    cut = max(0, min(n - VISIBLE, x0 - 8))
    if cut:
        candles = candles[cut:]
        x0 -= cut
        n = len(candles)
    if ahead > 0:
        x0 = n - 1 + ahead
    box_len = max(14, n // 4)           # minimum position box width (candles)
    x1 = max(x0 + box_len, n - 1 + 6)

    fig, ax = plt.subplots(figsize=(10, 6.2), dpi=120)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    for i, (_, o, h, l, c) in enumerate(candles):
        col = UP if c >= o else DOWN
        ax.vlines(i, l, h, color=col, lw=1, zorder=2)
        ax.add_patch(Rectangle((i - w / 2, min(o, c)), w, max(abs(c - o), 1e-12),
                               fc=col, ec=col, lw=0.6, zorder=3))

    # position box (green = profit zone, red = loss zone)
    ax.add_patch(Rectangle((x0, min(entry, tp)), x1 - x0, abs(tp - entry),
                           fc=TP_FILL, alpha=0.18, ec="none", zorder=1))
    ax.add_patch(Rectangle((x0, min(entry, sl)), x1 - x0, abs(sl - entry),
                           fc=SL_FILL, alpha=0.18, ec="none", zorder=1))
    # progress since entry (TradingView style): the box stays anchored at the entry candle and
    # the part between Entry and the current price, up to the last candle, is filled darker:
    # green while in profit, red while in loss. A dashed line joins entry -> current price.
    cur_x = n - 1
    if cur_x > x0 and t.get("opened_at"):  # pending / cancelled orders were never in the trade
        lo_b, hi_b = min(tp, sl), max(tp, sl)
        p_clip = min(max(price, lo_b), hi_b)
        in_profit = (p_clip - entry) * (1 if side == "LONG" else -1) > 0
        if abs(p_clip - entry) > 0:
            ax.add_patch(Rectangle((x0, min(entry, p_clip)), cur_x - x0, abs(p_clip - entry),
                                   fc=TP_FILL if in_profit else SL_FILL, alpha=0.30,
                                   ec="none", zorder=1.5))
        ax.plot([x0, cur_x], [entry, p_clip], color="#787B86", lw=1, ls=(0, (4, 3)), zorder=4)
    ax.hlines(entry, x0, x1, color="#B2B5BE", lw=0.8, alpha=0.9, zorder=4)  # soft grey, thin like TP/SL
    ax.hlines([tp], x0, x1, color=TP_FILL, lw=0.8, alpha=0.6, zorder=4)
    ax.hlines([sl], x0, x1, color=SL_FILL, lw=0.8, alpha=0.6, zorder=4)

    risk = abs(entry - sl) or 1e-12
    tp_pct = abs(tp - entry) / entry * 100
    sl_pct = abs(sl - entry) / entry * 100
    rr = abs(tp - entry) / risk
    pnl_r = (1 if side == "LONG" else -1) * (price - entry) / risk
    xm = (x0 + x1) / 2
    top_is_tp = tp > entry
    if labels:  # Target / Stop / Entry boxes (⚙ setting, off by default)
        _label(ax, xm, tp, f"Target: {_fmt(tp)} ({tp_pct:.2f}%)", TP_FILL,
               "bottom" if top_is_tp else "top")
        _label(ax, xm, sl, f"Stop: {_fmt(sl)} ({sl_pct:.2f}%)", SL_FILL,
               "top" if top_is_tp else "bottom")
        _label(ax, xm, entry, f"{side.title()} @ {_fmt(entry)}\nRisk/reward ratio: {rr:.2f}",
               UP if pnl_r >= 0 else DOWN, "center")

    # live price line + tags
    ax.axhline(price, color=TEXT, lw=0.8, ls=(0, (1, 2)), zorder=1)
    pcol = UP if candles[-1][4] >= candles[-1][1] else DOWN
    # time left until the current candle closes, shown under the price tag (TradingView style)
    cd = None
    left = (candles[-1][0].timestamp() + _TF_SEC.get(tf, 900)) - datetime.now(timezone.utc).timestamp()
    if 0 < left <= _TF_SEC.get(tf, 900):
        h_, r_ = divmod(int(left), 3600)
        cd = f"{h_}:{r_ // 60:02d}:{r_ % 60:02d}" if h_ else f"{r_ // 60:02d}:{r_ % 60:02d}"
    tags = [(tp, _fmt(tp), TP_FILL, None), (sl, _fmt(sl), SL_FILL, None),
            (entry, _fmt(entry), "#787B86", None), (price, _fmt(price), pcol, cd)]

    lo = min(min(c[3] for c in candles), tp, sl)
    hi = max(max(c[2] for c in candles), tp, sl)
    # keep the position box (TP..SL) in the vertical middle of the card:
    # symmetric range around the box center, wide enough to still show every candle
    mid = (tp + sl) / 2
    half = max(hi - mid, mid - lo, 1e-12)
    pad = half * 0.10
    ax.set_ylim(mid - half - pad, mid + half + pad)
    # empty right margin after the box, like TradingView's space before the price scale
    # left: candles run off the edge (no white gap), right: empty margin like TradingView
    ax.set_xlim(1.5, x1 + max(8, n * 0.12))

    # axes styling
    ax.yaxis.tick_right()
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _fmt(v)))
    ax.grid(False)  # clean chart, no grid lines
    ax.set_axisbelow(True)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(colors=TEXT, labelsize=9, length=0)
    step = max(1, n // 6)
    ticks = list(range(max(12, step // 2), n, step))  # first label fully inside the chart
    ax.set_xticks(ticks)
    ax.set_xticklabels([candles[i][0].strftime("%d %b %H:%M") for i in ticks])

    _legend(ax, t["pair"], tf, source, candles[-1])
    if badge:
        ax.text(0.985, 0.025, badge[0], transform=ax.transAxes, ha="right", va="bottom", fontsize=11,
                fontweight="bold", color="white",
                bbox=dict(boxstyle="round,pad=0.4", fc=badge[1], ec=badge[1]))
    if brand:
        ax.text(0.5, 0.5, brand, transform=ax.transAxes, ha="center", va="center",
                fontsize=42, color="#131722", alpha=0.05, fontweight="bold")

    # axes flush with the left/top edges; room only for the price scale and time axis
    fig.subplots_adjust(left=0, right=0.905, top=1, bottom=0.06)
    _place_tags(ax, fig, tags)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return _frame(buf.getvalue())


FRAME_BG = "#E0E3EB"      # outside the chart card (TradingView window frame)
FRAME_BORDER = "#D1D4DC"  # 1px card border
RADIUS, MARGIN = 16, 10


def _frame(png: bytes) -> bytes:
    """Put the chart on a card with rounded TOP corners, like a TradingView window.
    Corners are drawn on a solid frame colour (not transparency), because Telegram
    converts photos to JPEG and transparent corners would turn black."""
    from PIL import Image, ImageDraw
    chart_img = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = chart_img.size
    W, H = w + 2 * MARGIN, h + MARGIN
    out = Image.new("RGB", (W, H), FRAME_BG)
    k = 4  # supersampling for smooth anti-aliased corners

    def card_mask(inset: int) -> Image.Image:
        m = Image.new("L", (w * k, (h + RADIUS) * k), 0)
        ImageDraw.Draw(m).rounded_rectangle(
            (inset * k, inset * k, (w - inset) * k - 1, (h + RADIUS) * k), radius=RADIUS * k, fill=255)
        return m.resize((w, h + RADIUS), Image.LANCZOS).crop((0, 0, w, h))

    out.paste(Image.new("RGB", (w, h), FRAME_BORDER), (MARGIN, MARGIN), card_mask(0))
    out.paste(chart_img, (MARGIN, MARGIN), card_mask(1))
    res = io.BytesIO()
    out.save(res, format="PNG", optimize=True)
    return res.getvalue()


async def snapshot(session, t: dict, tf: str = "15m", price: float = None, brand: str = "",
                   start: datetime = None, badge: tuple = None, labels: bool = False,
                   ahead: int = 0):
    """Fetch candles and render in a worker thread. Returns PNG bytes or None."""
    limit = 90
    if ahead > 0:
        start = None  # pending box floats after the last candle, no need for older candles
    if start is not None:  # make sure the activation candle is on the chart (max 300 candles)
        back = (datetime.now(timezone.utc) - start).total_seconds() / _TF_SEC.get(tf, 900)
        limit = int(min(300, max(90, back + 20)))
    source, candles = await get_klines(session, t["pair"], tf, limit)
    if not candles:
        return None
    try:
        return await asyncio.to_thread(render, t, candles, tf, price, brand, start, badge,
                                       source, labels, ahead)
    except Exception:
        log.exception("chart render failed for %s", t.get("pair"))
        return None
