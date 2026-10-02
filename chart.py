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
from matplotlib.patches import Rectangle
import matplotlib.dates as mdates

log = logging.getLogger(__name__)

TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h"]
_BYBIT_TF = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240"}
_TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}

# TradingView light theme colours
UP, DOWN = "#089981", "#F23645"
TP_FILL, SL_FILL = "#089981", "#F23645"
GRID, TEXT, BG = "#F0F3FA", "#787B86", "#FFFFFF"


# ---------------------------------------------------------------- data
async def get_klines(session: aiohttp.ClientSession, pair: str, tf: str = "15m", limit: int = 90):
    """List of (open_time_utc, o, h, l, c). Binance spot -> Binance futures -> Bybit."""
    for base in ("https://api.binance.com/api/v3/klines", "https://fapi.binance.com/fapi/v1/klines"):
        try:
            async with session.get(base, params={"symbol": pair, "interval": tf, "limit": limit}) as r:
                if r.status == 200:
                    j = await r.json()
                    if j:
                        return [(datetime.fromtimestamp(k[0] / 1000, timezone.utc),
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
                        return [(datetime.fromtimestamp(int(k[0]) / 1000, timezone.utc),
                                 float(k[1]), float(k[2]), float(k[3]), float(k[4]))
                                for k in reversed(rows)]
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, IndexError):
            pass
    log.warning("no klines for %s", pair)
    return None


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


def _label(ax, x, y, text, color, va):
    ax.text(x, y, text, ha="center", va=va, fontsize=9.5, color="white", fontweight="bold",
            zorder=6, bbox=dict(boxstyle="round,pad=0.35", fc=color, ec="white", lw=1))


def render(t: dict, candles: list, tf: str = "15m", price: float = None, brand: str = "") -> bytes:
    """PNG bytes of the chart with the position box drawn at the entry candle."""
    side, entry, tp, sl = t["side"], float(t["entry"]), float(t["tp"]), float(t["sl"])
    price = float(price if price is not None else candles[-1][4])
    n = len(candles)
    w = 0.62  # candle body width in index units
    box_len = max(18, n // 4)           # position box width (candles)
    x0, x1 = n - 1, n - 1 + box_len      # box starts at the activation candle

    fig, ax = plt.subplots(figsize=(10, 6.2), dpi=120)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    for i, (_, o, h, l, c) in enumerate(candles):
        col = UP if c >= o else DOWN
        ax.vlines(i, l, h, color=col, lw=1, zorder=2)
        ax.add_patch(Rectangle((i - w / 2, min(o, c)), w, max(abs(c - o), 1e-12),
                               fc=col, ec=col, lw=0.6, zorder=3))

    # position box (green = profit zone, red = loss zone)
    ax.add_patch(Rectangle((x0, min(entry, tp)), box_len, abs(tp - entry),
                           fc=TP_FILL, alpha=0.18, ec="none", zorder=1))
    ax.add_patch(Rectangle((x0, min(entry, sl)), box_len, abs(sl - entry),
                           fc=SL_FILL, alpha=0.18, ec="none", zorder=1))
    ax.hlines(entry, x0, x1, color="#555", lw=1.2, zorder=4)
    ax.hlines([tp], x0, x1, color=TP_FILL, lw=0.8, alpha=0.6, zorder=4)
    ax.hlines([sl], x0, x1, color=SL_FILL, lw=0.8, alpha=0.6, zorder=4)

    risk = abs(entry - sl) or 1e-12
    tp_pct = abs(tp - entry) / entry * 100
    sl_pct = abs(sl - entry) / entry * 100
    rr = abs(tp - entry) / risk
    pnl_r = (1 if side == "LONG" else -1) * (price - entry) / risk
    xm = (x0 + x1) / 2
    top_is_tp = tp > entry
    _label(ax, xm, tp, f"Target: {_fmt(tp)} ({tp_pct:.2f}%)", TP_FILL,
           "bottom" if top_is_tp else "top")
    _label(ax, xm, sl, f"Stop: {_fmt(sl)} ({sl_pct:.2f}%)", SL_FILL,
           "top" if top_is_tp else "bottom")
    _label(ax, xm, entry, f"{side.title()} @ {_fmt(entry)}\nRisk/reward ratio: {rr:.2f}",
           UP if pnl_r >= 0 else DOWN, "center")

    # live price line + tags
    ax.axhline(price, color=TEXT, lw=0.8, ls=(0, (1, 2)), zorder=1)
    _tag(ax, tp, _fmt(tp), TP_FILL)
    _tag(ax, sl, _fmt(sl), SL_FILL)
    _tag(ax, entry, _fmt(entry), "#787B86")
    _tag(ax, price, _fmt(price), UP if candles[-1][4] >= candles[-1][1] else DOWN)

    lo = min(min(c[3] for c in candles), tp, sl)
    hi = max(max(c[2] for c in candles), tp, sl)
    pad = (hi - lo) * 0.08
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlim(-1, x1 + 3)

    # axes styling
    ax.yaxis.tick_right()
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _fmt(v)))
    ax.grid(True, color=GRID, lw=1)
    ax.set_axisbelow(True)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(colors=TEXT, labelsize=9, length=0)
    step = max(1, n // 6)
    ticks = list(range(0, n, step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([candles[i][0].strftime("%d %b %H:%M") for i in ticks])

    title = f"{t.get('symbol', t['pair'])} · {tf} · {side}"
    ax.text(0.01, 0.98, title, transform=ax.transAxes, ha="left", va="top",
            fontsize=13, fontweight="bold", color="#131722")
    if brand:
        ax.text(0.5, 0.5, brand, transform=ax.transAxes, ha="center", va="center",
                fontsize=42, color="#131722", alpha=0.05, fontweight="bold")

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


async def snapshot(session, t: dict, tf: str = "15m", price: float = None, brand: str = ""):
    """Fetch candles and render in a worker thread. Returns PNG bytes or None."""
    candles = await get_klines(session, t["pair"], tf)
    if not candles:
        return None
    try:
        return await asyncio.to_thread(render, t, candles, tf, price, brand)
    except Exception:
        log.exception("chart render failed for %s", t.get("pair"))
        return None
