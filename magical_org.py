"""magical_org — Telegram signal poster (admin panel, inline-only UI).

The bot itself posts to channels (it must be an admin with post permission).

Commands: /menu, /start and /help only. Everything else is driven by inline buttons.
"""
import asyncio
import copy
import html
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import tempfile
import time
import uuid
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import BotCommand, MenuButtonCommands, ReplyKeyboardMarkup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup as M, ReplyParameters, Update
from telegram import __version__ as PTB_VERSION
try:
    from telegram import CopyTextButton  # python-telegram-bot >= 21.7
except ImportError:  # older library: the copy button is simply left out
    CopyTextButton = None
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import chart
import trading
from trading import fmt_price, fmt_r, fmt_rr, fmt_step

# Only warnings and errors are logged (no per-request / per-job success lines).
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.WARNING)
for _n in ("httpx", "httpcore", "apscheduler", "telegram", "telethon"):
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("bot")


def B(text: str, cb: str) -> InlineKeyboardButton:
    """Inline button with explicit callback_data (2nd positional arg of PTB is `url`)."""
    return InlineKeyboardButton(text, callback_data=cb)


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Owners come from .env. Extra admins (managed from the bot) live in the SQLite database.
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DATA_FILE = os.getenv("DATA_FILE", "data.json")   # legacy JSON store, imported once
DB_FILE = os.getenv("DB_FILE", "data.db")         # SQLite database (channels, trades, settings)
BASE_DIR = Path(__file__).resolve().parent


def _path(p: str) -> Path:
    pp = Path(p)
    return pp if pp.is_absolute() else BASE_DIR / pp


DB_PATH = _path(DB_FILE)
LEGACY_PATH = _path(DATA_FILE)
TZ = ZoneInfo(trading.TIMEZONE)
BOT_NAME = "magical_org"
START_TS = time.time()
LAST_TICK = 0.0
BOT = None  # telegram.Bot, set in post_init


# ====================== Storage (SQLite) ======================
# The whole state is kept in memory as a dict (fast reads) and every save() writes only
# the rows that changed to SQLite, so channels, positions and trade history survive restarts.

DEFAULTS = {
    "channels": [],
    "trades": [],
    "admins": [],
    "trade_counter": 0,
    "settings": {"reward_on": True, "reward_every": 1, "be_after": 0, "poll_seconds": 5,
                 "auto_on": False, "auto_time": "21:00", "auto_mode": "always",
                 "auto_last_ts": "", "auto_last_date": "",
                 "img_on": False, "img_tf": "15m"},
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS channels (key TEXT PRIMARY KEY, pos INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY, channel TEXT, status TEXT, closed_at TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_channel ON trades(channel);
"""

_data = None
_conn = None
_snap = {"counter": None, "settings": None, "admins": set(), "channels": {}, "trades": {}}


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(_SCHEMA)
        _conn.commit()
    return _conn


def _dump(o) -> str:
    return json.dumps(o, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _kv_get(key: str, default: str) -> str:
    r = get_conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def _normalize(d: dict):
    st = d.get("settings") or {}
    if "reward_every" not in st and st.get("reward_steps"):  # migrate old list -> interval
        st["reward_every"] = float(min(st["reward_steps"]))
    for k, v in DEFAULTS.items():
        d.setdefault(k, copy.deepcopy(v))
    for k, v in DEFAULTS["settings"].items():
        d["settings"].setdefault(k, v)


def _load() -> dict:
    global _data
    c = get_conn()
    initialized = c.execute("SELECT 1 FROM kv WHERE key='initialized'").fetchone()
    migrated = False
    if initialized:
        ch_rows = [r[0] for r in c.execute("SELECT data FROM channels ORDER BY pos")]
        tr_rows = c.execute("SELECT id, data FROM trades ORDER BY id").fetchall()
        data = {
            "trade_counter": int(_kv_get("trade_counter", "0")),
            "settings": json.loads(_kv_get("settings", "{}")),
            "admins": [r[0] for r in c.execute("SELECT user_id FROM admins ORDER BY rowid")],
            "channels": [json.loads(x) for x in ch_rows],
            "trades": [json.loads(r[1]) for r in tr_rows],
        }
        # snapshot = what is on disk right now, so save() only writes real changes
        _snap["counter"] = str(data["trade_counter"])
        _snap["settings"] = _kv_get("settings", "{}")
        _snap["admins"] = set(data["admins"])
        _snap["channels"] = {json.loads(x)["key"]: (i, x) for i, x in enumerate(ch_rows)}
        _snap["trades"] = {r[0]: r[1] for r in tr_rows}
    else:
        data = {}
        if LEGACY_PATH.exists():  # one-time import of the old data.json
            with open(LEGACY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            migrated = True
    _normalize(data)
    _data = data
    if not initialized:
        save()
        c.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('initialized', '1')")
        c.commit()
        if migrated:
            os.replace(LEGACY_PATH, str(LEGACY_PATH) + ".migrated")
            log.warning("data.json imported into %s", DB_PATH)
    return data


def db() -> dict:
    if _data is None:
        _load()
    return _data


def save():
    d = db()
    c = get_conn()
    cnt = str(d["trade_counter"])
    st = _dump(d["settings"])
    adm = set(d["admins"])
    ch_now = {ch["key"]: (i, _dump(ch)) for i, ch in enumerate(d["channels"])}
    tr_now = {t["id"]: _dump(t) for t in d["trades"]}
    with c:  # one transaction
        if cnt != _snap["counter"]:
            c.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('trade_counter', ?)", (cnt,))
        if st != _snap["settings"]:
            c.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('settings', ?)", (st,))
        for uid in adm - _snap["admins"]:
            c.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,))
        for uid in _snap["admins"] - adm:
            c.execute("DELETE FROM admins WHERE user_id=?", (uid,))
        for k, v in ch_now.items():
            if _snap["channels"].get(k) != v:
                c.execute("INSERT INTO channels(key, pos, data) VALUES(?,?,?) "
                          "ON CONFLICT(key) DO UPDATE SET pos=excluded.pos, data=excluded.data",
                          (k, v[0], v[1]))
        for k in set(_snap["channels"]) - set(ch_now):
            c.execute("DELETE FROM channels WHERE key=?", (k,))
        for t in d["trades"]:
            js = tr_now[t["id"]]
            if _snap["trades"].get(t["id"]) != js:
                c.execute("INSERT INTO trades(id, channel, status, closed_at, data) VALUES(?,?,?,?,?) "
                          "ON CONFLICT(id) DO UPDATE SET channel=excluded.channel, status=excluded.status, "
                          "closed_at=excluded.closed_at, data=excluded.data",
                          (t["id"], t.get("channel"), t.get("status"), t.get("closed_at"), js))
        for tid in set(_snap["trades"]) - set(tr_now):
            c.execute("DELETE FROM trades WHERE id=?", (tid,))
    _snap.update(counter=cnt, settings=st, admins=adm, channels=ch_now, trades=tr_now)


def make_backup() -> Path:
    """Consistent copy of the live database in a temp file (caller deletes it)."""
    save()
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    dst = sqlite3.connect(tmp)
    try:
        get_conn().backup(dst)
    finally:
        dst.close()
    return Path(tmp)


# ---------- restore ----------
_REQUIRED_TABLES = {"kv", "admins", "channels", "trades"}


def inspect_backup(path: Path) -> dict:
    """Validate an uploaded backup. Returns counts, raises ValueError if it is not usable."""
    with open(path, "rb") as f:
        if f.read(16) != b"SQLite format 3\x00":
            raise ValueError("این فایل دیتابیس SQLite نیست.")
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("فایل بکاپ خراب است (integrity check).")
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not _REQUIRED_TABLES <= tables:
            raise ValueError("این فایل بکاپ magical_org نیست (جدول‌ها ناقص‌اند).")
        if not c.execute("SELECT 1 FROM kv WHERE key='initialized'").fetchone():
            raise ValueError("بکاپ خالی یا ناقص است.")
        st = dict(c.execute("SELECT status, COUNT(*) FROM trades GROUP BY status").fetchall())
        return {
            "channels": c.execute("SELECT COUNT(*) FROM channels").fetchone()[0],
            "admins": c.execute("SELECT COUNT(*) FROM admins").fetchone()[0],
            "trades": sum(st.values()),
            "active": st.get("pending", 0) + st.get("open", 0),
        }
    finally:
        c.close()


def restore_backup(src: Path) -> Path:
    """Swap the live database with `src`. The current data is saved first; returns that copy."""
    global _conn, _data
    save()
    keep_dir = BASE_DIR / "backups"
    keep_dir.mkdir(exist_ok=True)
    before = keep_dir / f"before_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    tmp = make_backup()
    os.replace(tmp, before)
    if _conn is not None:
        _conn.close()
        _conn = None
    for suffix in ("-wal", "-shm"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
    os.replace(src, DB_PATH)
    _data = None
    _snap.update(counter=None, settings=None, admins=set(), channels={}, trades={})
    db()  # load the restored database into memory
    return before


def settings() -> dict:
    return db()["settings"]


def channels() -> list:
    return db()["channels"]


def get_channel(key: str):
    return next((c for c in channels() if c["key"] == key), None)


def add_channel(chat, title: str) -> dict:
    ch = {"key": uuid.uuid4().hex[:6], "chat": chat, "title": title, "counter": 0}
    channels().append(ch)
    save()
    return ch


def delete_channel(key: str):
    db()["channels"] = [c for c in channels() if c["key"] != key]
    save()


def trades() -> list:
    return db()["trades"]


def get_trade(tid: int):
    return next((t for t in trades() if t["id"] == tid), None)


def active_trades(channel_key: str = None) -> list:
    return [t for t in trades() if t["status"] in ("pending", "open")
            and (channel_key is None or t["channel"] == channel_key)]


def reward_steps(every, rr) -> list:
    """Reward levels every `every` R, below the TP level (max 30 levels)."""
    try:
        every = float(every)
    except (TypeError, ValueError):
        return []
    if every <= 0:
        return []
    out, k = [], 1
    while every * k < rr - 1e-9 and k <= 30:
        out.append(round(every * k, 4))
        k += 1
    return out


def create_trade(draft: dict, channel: dict) -> dict:
    d = db()
    d["trade_counter"] += 1
    channel["counter"] += 1
    s = settings()
    t = {
        "id": d["trade_counter"],
        "no": channel["counter"],
        "channel": channel["key"],
        "status": "pending",
        "steps": reward_steps(s["reward_every"], draft["rr"]),
        "be_after": float(s["be_after"]),
        "be_active": False,
        "rewards_hit": [],
        "pending_msg_id": None,
        "open_msg_id": None,
        "result_r": None,
        "outcome": None,
        "reported": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_price": draft["price"],
    }
    for k in ("pair", "symbol", "side", "entry", "tp", "sl", "rr", "order"):
        t[k] = draft[k]
    d["trades"].append(t)
    save()
    return t


def remove_trade(tid: int):
    db()["trades"] = [t for t in trades() if t["id"] != tid]
    save()


# ====================== Access control ======================

def is_owner(uid: int) -> bool:
    return uid in ADMIN_IDS


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS or uid in db()["admins"]


def admin_only(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if not u or not is_admin(u.id):
            return
        return await fn(update, context)
    return wrapper


# ====================== UI helpers ======================

LOCK = asyncio.Lock()
esc = html.escape

# <pre> block (monospace, one block). The home screen also has a copy button for all lines.
SIGNAL_FORMAT = "<pre>BTC\nInt\nTp\nSl</pre>"
SIGNAL_TEXT = "BTC\nInt\nTp\nSl"  # exactly what the copy button puts on the clipboard
SIGNAL_EXAMPLE = "<pre>btc\nInt81000\nTp87000\nSl80000</pre>"

HELP = (
    "📖 <b>راهنما</b>\n\n"
    "<b>ارسال سیگنال</b>\n"
    "قالب را با یک لمس کپی کن، عددها را بنویس و بفرست:\n"
    f"{SIGNAL_FORMAT}\n"
    "نمونه:\n"
    f"{SIGNAL_EXAMPLE}\n"
    "• <code>Int</code> = Entry ، <code>Tp</code> = Take Profit ، <code>Sl</code> = Stop Loss\n"
    "• جهت (LONG/SHORT) و Risk/Reward خودکار محاسبه می‌شود.\n"
    "• پیش‌نمایش می‌آید؛ کانال را انتخاب کن تا خود بات در آن کانال پست بگذارد.\n• بات باید در کانال <b>ادمین</b> با دسترسی «ارسال پست» باشد.\n\n"
    "<b>چرخه‌ی معامله</b>\n"
    "Pending ← Position Opened ← Reward ها ← TP / SL\n"
    "همه‌ی مراحل به‌صورت ریپلای روی پست اصلی ارسال می‌شوند.\n\n"
    "<b>مدیریت</b>\n"
    "تمام بخش‌ها (کانال‌ها، تنظیمات، گزارش، ادمین) از دکمه‌های زیر در دسترس‌اند.\n"
    "گزارش روزانه‌ی خودکار از «تنظیمات ← گزارش خودکار» قابل تنظیم است."
)


async def show(update: Update, text: str, kb=None):
    kw = dict(text=text, parse_mode=ParseMode.HTML, reply_markup=kb,
              disable_web_page_preview=True)
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(**kw)
            return
        except BadRequest as ex:
            if "not modified" in str(ex).lower():
                return
            await q.message.reply_text(**kw)
        except Exception:
            await q.message.reply_text(**kw)
    else:
        await update.effective_message.reply_text(**kw)


async def say(update: Update, text: str, kb=None):
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)


MENU_LABEL = "🏠 منو"
PEND_LABEL = "◷ پندینگ"
POS_LABEL = "● پوزیشن"


def main_kb() -> ReplyKeyboardMarkup:
    """Telegram's own persistent keyboard: Pending + Positions on top, Menu below."""
    return ReplyKeyboardMarkup([[PEND_LABEL, POS_LABEL], [MENU_LABEL]],
                               resize_keyboard=True, is_persistent=True)


def home_kb():
    copy_row = ([[InlineKeyboardButton("📋 کپی قالب سیگنال", copy_text=CopyTextButton(text=SIGNAL_TEXT))]]
                if CopyTextButton else [])
    return M(copy_row + [
        [B("📈 عملکرد", "menu:stats"), B("📊 پست گزارش", "menu:sum")],
        [B("📺 کانال‌ها", "menu:channels"), B("⚙️ تنظیمات", "menu:settings")],
        [B("🛡 پنل ادمین", "adm:home"), B("❓ راهنما", "menu:help")],
    ])


def back_kb():
    return M([[B("⬅️ بازگشت", "menu:home")]])


def cancel_kb():
    return M([[B("✖️ انصراف", "menu:home")]])


def confirm_kb(yes_cb: str, no_cb: str, yes_label="✅ بله، انجام بده"):
    return M([[B(yes_label, yes_cb), B("↩️ خیر", no_cb)]])


def uptime_str(sec: float) -> str:
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = ([f"{d}d"] if d else []) + ([f"{h}h"] if h else []) + [f"{m}m", f"{s}s"]
    return " ".join(parts[:3])


def _counts():
    tr = trades()
    pend = sum(1 for t in tr if t["status"] == "pending")
    opn = sum(1 for t in tr if t["status"] == "open")
    unrep = sum(1 for t in tr if t["status"] == "closed" and not t["reported"])
    return pend, opn, unrep


async def reset_flow(ud: dict):
    for k in ("state", "draft"):
        ud.pop(k, None)
    f = ud.pop("restore_file", None)
    if f:
        Path(f).unlink(missing_ok=True)


# ====================== Views ======================

async def view_home(update):
    pend, opn, unrep = _counts()
    text = (
        f"<b>{BOT_NAME}</b> · پنل مدیریت\n\n"
        "<blockquote>"
        f"📋 معاملات فعال: <b>{pend + opn}</b>  ({pend} Pending · {opn} Open)\n"
        f"📊 گزارش‌نشده: <b>{unrep}</b>\n"
        f"📺 کانال‌ها: <b>{len(channels())}</b>"
        "</blockquote>\n\n"
        "📝 <b>قالب سیگنال</b> (با دکمه‌ی «کپی قالب» هر چهار خط یکجا کپی می‌شود)\n"
        f"{SIGNAL_FORMAT}"
    )
    await show(update, text, home_kb())


async def view_help(update):
    await show(update, HELP, back_kb())


async def view_channels(update):
    chans = channels()
    lines = ["📺 <b>کانال‌ها</b>\n"]
    kb = []
    for c in chans:
        lines.append(f"• {esc(c['title'])} <code>{esc(str(c['chat']))}</code>")
        kb.append([B(f"🔌 تست {c['title']}", f"ch:test:{c['key']}"),
                   B("🗑 حذف", f"ch:del:{c['key']}")])
    if not chans:
        lines.append("هنوز کانالی اضافه نشده است.")
    kb.append([B("➕ افزودن کانال", "ch:add")])
    kb.append([B("⬅️ بازگشت", "menu:home")])
    await show(update, "\n".join(lines), M(kb))


TRADE_VIEWS = {
    "p": ("◷ پندینگ‌ها", "پندینگی وجود ندارد.", ("pending",)),
    "o": ("● پوزیشن‌های فعال", "پوزیشن بازی وجود ندارد.", ("open",)),
    "a": ("معاملات فعال", "معامله‌ی فعالی وجود ندارد.", ("pending", "open")),
}


async def view_trades(update, page: int = 0, kind: str = "a"):
    kind = kind if kind in TRADE_VIEWS else "a"
    title, empty_msg, statuses = TRADE_VIEWS[kind]
    active = [t for t in active_trades() if t["status"] in statuses]
    if not active:
        await show(update, f"<b>{title}</b>\n\n{empty_msg}",
                   M([[B("بروزرسانی", f"trp:0:{kind}")]]))
        return
    per = 5
    pages = (len(active) + per - 1) // per
    page = max(0, min(page, pages - 1))
    chunk = active[page * per:(page + 1) * per]
    pairs = list({t["pair"] for t in chunk})
    res = await asyncio.gather(*(trading.get_price(p) for p in pairs))
    prices = dict(zip(pairs, res))

    sep = "┈┈┈┈┈┈┈┈┈┈┈┈"
    blocks = [f"<b>{title}</b>  ·  {len(active)}"]
    kb = []
    for t in chunk:
        is_open = t["status"] == "open"
        arrow = "↑" if t["side"] == "LONG" else "↓"
        cur = prices.get(t["pair"])
        r = None
        if cur is not None and is_open:
            risk = abs(t["entry"] - t["sl"])
            d = 1 if t["side"] == "LONG" else -1
            r = d * (cur - t["entry"]) / risk

        # header + status
        status = "Open" if is_open else "Pending"
        if is_open and r is not None:
            status += f"  ·  <b>{fmt_r(r)}</b>"
        head = f"<b>#{t['id']}  {esc(t['symbol'])} {t['side']}</b>  ·  {status}"

        # body
        body = [f"Entry {fmt_price(t['entry'])}   TP {fmt_price(t['tp'])}   SL {fmt_price(t['sl'])}"]
        now = f"Now {fmt_price(cur)}" if cur is not None else "Now  —"
        if t["rewards_hit"]:
            now += "   ·   " + " ".join(fmt_step(s) for s in sorted(t["rewards_hit"]))
        if t.get("be_active"):
            now += "   ·   BE"
        body.append(now)
        blocks.append(head + "\n" + "\n".join(body))

        # buttons: [name] [break-even] [close / cancel]
        name = B(f"#{t['id']} {t['symbol']} {arrow}", "noop")
        if is_open:
            be = (B("BE ✓", "noop") if t.get("be_active")
                  else B("Break-even", f"tr:be:{t['id']}:{kind}"))
            kb.append([name, be, B("Close", f"tr:close:{t['id']}:{kind}")])
        else:
            kb.append([name, B("Cancel", f"tr:cancel:{t['id']}:{kind}")])
    if pages > 1:
        kb.append([B("‹", f"trp:{max(page - 1, 0)}:{kind}"), B(f"{page + 1}/{pages}", "noop"),
                   B("›", f"trp:{min(page + 1, pages - 1)}:{kind}")])
    kb.append([B("بروزرسانی", f"trp:{page}:{kind}")])
    await show(update, ("\n" + sep + "\n").join(blocks), M(kb))


async def view_stats(update):
    closed = [t for t in trades() if t["status"] == "closed" and t["result_r"] is not None]
    if not closed:
        pend, opn, _ = _counts()
        await show(update,
                   "📈 <b>عملکرد</b>\n\n"
                   "هنوز نتیجه‌ای برای نمایش نیست.\n"
                   "عملکرد (مجموع R، Win Rate و …) بعد از بسته‌شدن اولین معامله با TP، SL یا بستن دستی محاسبه می‌شود.\n\n"
                   f"<blockquote>● پوزیشن باز: <b>{opn}</b>\n◷ پندینگ: <b>{pend}</b></blockquote>",
                   back_kb())
        return
    rs = [t["result_r"] for t in closed]
    wins = sum(1 for r in rs if r > 0.005)
    losses = sum(1 for r in rs if r < -0.005)
    be = len(rs) - wins - losses
    total = sum(rs)
    body = [
        f"📦 معاملات بسته‌شده: <b>{len(rs)}</b>",
        f"💎 مجموع: <b>{fmt_r(total)}</b>  (میانگین {fmt_r(total / len(rs))})",
        f"🎯 Win Rate: <b>{round(wins / len(rs) * 100)}%</b>",
        f"✅ {wins}  ·  ❌ {losses}  ·  ⚪️ {be}",
        f"🥇 بهترین: {fmt_r(max(rs))}  ·  🥀 بدترین: {fmt_r(min(rs))}",
    ]
    lines = ["📈 <b>عملکرد کلی</b>", "", "<blockquote>" + "\n".join(body) + "</blockquote>"]
    per_ch = []
    for c in channels():
        rows = [t["result_r"] for t in closed if t["channel"] == c["key"]]
        if rows:
            per_ch.append(f"• {esc(c['title'])}: {len(rows)} معامله · <b>{fmt_r(sum(rows))}</b>")
    if per_ch:
        lines += ["", "📺 <b>به تفکیک کانال</b>", "<blockquote>" + "\n".join(per_ch) + "</blockquote>"]
    await show(update, "\n".join(lines), back_kb())


async def view_settings(update):
    s = settings()
    if s["reward_on"]:
        rw = f"هر {fmt_step(s['reward_every'])}"
    else:
        rw = "خاموش"
    be = f"بعد از {fmt_step(s['be_after'])}" if s["be_after"] else "خاموش"
    text = (
        "⚙️ <b>تنظیمات</b>\n\n"
        "<blockquote>"
        f"🏆 پست ریوارد: <b>{rw}</b>\n"
        f"🛡 انتقال SL به Entry: <b>{be}</b>\n"
        f"⏱ چک قیمت: <b>هر {s['poll_seconds']} ثانیه</b>\n"
        f"📅 گزارش خودکار: <b>{_auto_brief(s)}</b>\n"
        f"🖼 عکس ورود: <b>{('روشن · ' + s['img_tf']) if s['img_on'] else 'خاموش'}</b>"
        "</blockquote>"
    )
    tog = "🔕 خاموش کردن ریوارد" if s["reward_on"] else "🔔 روشن کردن ریوارد"
    kb = M([
        [B("🏆 فاصله‌ی ریوارد", "set:rewards"), B(tog, "set:rewtoggle")],
        [B("🛡 Break-even", "set:be"), B("⏱ فاصله‌ی چک", "set:poll")],
        [B("📅 گزارش خودکار", "menu:auto")],
        [B("🖼 خاموش کردن عکس ورود" if s["img_on"] else "🖼 روشن کردن عکس ورود", "set:imgtoggle"),
         B(f"🕯 تایم‌فریم: {s['img_tf']}", "set:imgtf")],
        [B("👁 نمونه‌ی عکس", "set:imgtest")],
        [B("⬅️ بازگشت", "menu:home")],
    ])
    await show(update, text, kb)


def _auto_brief(s: dict) -> str:
    if not s["auto_on"]:
        return "خاموش"
    return f"{s['auto_time']} · " + ("فقط روز سودده" if s["auto_mode"] == "profit" else "هر روز")


async def view_auto(update):
    s = settings()
    on = s["auto_on"]
    mode = ("فقط اگر جمع R آن روز مثبت بود" if s["auto_mode"] == "profit"
            else "همیشه (اگر معامله‌ی بسته‌شده‌ای باشد)")
    text = (
        "📅 <b>گزارش خودکار</b>\n\n"
        "هر روز در ساعت تعیین‌شده، گزارش معاملات بسته‌شده‌ی همان روز به‌صورت جداگانه "
        "برای هر کانال پست می‌شود.\n\n"
        "<blockquote>"
        f"{'✅ فعال' if on else '⛔️ خاموش'}\n"
        f"⏰ ساعت ارسال: <b>{s['auto_time']}</b>  ({esc(trading.TIMEZONE)})\n"
        f"🎯 شرط ارسال: <b>{mode}</b>\n"
        f"🕘 آخرین اجرا: <b>{s.get('auto_last_date') or '—'}</b>"
        "</blockquote>"
    )
    kb = M([
        [B("🔕 خاموش کردن" if on else "🔔 روشن کردن", "auto:toggle")],
        [B("⏰ تغییر ساعت", "auto:time"),
         B("🎯 شرط: " + ("فقط سودده" if s["auto_mode"] == "profit" else "همیشه"), "auto:mode")],
        [B("⬅️ بازگشت", "menu:settings")],
    ])
    await show(update, text, kb)


async def view_sum(update):
    kb = []
    for c in channels():
        n = sum(1 for t in trades()
                if t["channel"] == c["key"] and t["status"] == "closed" and not t["reported"])
        kb.append([B(f"📤 {c['title']} ({n} معامله)", f"sum:go:{c['key']}")])
    kb.append([B("⬅️ بازگشت", "menu:home")])
    text = ("📊 <b>پست گزارش</b>\n\nگزارش معاملات بسته‌شده‌ی گزارش‌نشده را برای کدام کانال بفرستم؟"
            if channels() else "📊 <b>پست گزارش</b>\n\nهنوز کانالی اضافه نشده است.")
    await show(update, text, M(kb))


# ---------- Admin panel ----------

async def view_admin(update):
    uid = update.effective_user.id
    owner = is_owner(uid)
    text = (
        "🛡 <b>پنل ادمین</b>\n\n"
        f"نقش شما: <b>{'👑 مالک' if owner else '🛡 ادمین'}</b>\n"
        f"🆔 آیدی شما: <code>{uid}</code>\n"
        f"👥 مالکان: {len(ADMIN_IDS)}   🛡 ادمین‌ها: {len(db()['admins'])}"
    )
    rows = [[B("👥 ادمین‌ها", "adm:list"), B("🖥 وضعیت سیستم", "adm:status")]]
    if owner:
        rows.append([B("💾 بکاپ داده‌ها", "adm:backup"), B("🧹 پاکسازی تاریخچه", "adm:purge")])
        rows.append([B("♻️ ریستور بکاپ", "adm:restore"), B("🔄 ریستارت بات", "adm:restart")])
    rows.append([B("⬅️ بازگشت", "menu:home")])
    await show(update, text, M(rows))


async def view_admins(update):
    uid = update.effective_user.id
    owner = is_owner(uid)
    lines = ["👥 <b>ادمین‌ها</b>\n", "👑 <b>مالکان</b> (فایل .env)"]
    for i in sorted(ADMIN_IDS):
        lines.append(f"• <code>{i}</code>" + ("  ← شما" if i == uid else ""))
    extra = db()["admins"]
    lines += ["", "🛡 <b>ادمین‌ها</b>"]
    kb = []
    if extra:
        for i in extra:
            lines.append(f"• <code>{i}</code>" + ("  ← شما" if i == uid else ""))
            if owner:
                kb.append([B(f"🗑 حذف {i}", f"adm:del:{i}")])
    else:
        lines.append("ادمین اضافه‌ای وجود ندارد.")
    if owner:
        kb.append([B("➕ افزودن ادمین", "adm:add")])
    kb.append([B("⬅️ بازگشت", "adm:home")])
    await show(update, "\n".join(lines), M(kb))


def _db_size() -> str:
    try:
        kb = DB_PATH.stat().st_size / 1024
    except OSError:
        return "—"
    return f"{kb:.0f} KB" if kb < 1024 else f"{kb / 1024:.1f} MB"


async def view_status(update):
    t0 = time.perf_counter()
    price = await trading.get_price("BTCUSDT")
    ms = int((time.perf_counter() - t0) * 1000)
    api = f"✅ آنلاین ({ms} ms)" if price is not None else "❌ در دسترس نیست"
    tr = trades()
    pend, opn, unrep = _counts()
    closed = sum(1 for t in tr if t["status"] == "closed")
    cancelled = sum(1 for t in tr if t["status"] == "cancelled")
    tick = f"{int(time.time() - LAST_TICK)} ثانیه پیش" if LAST_TICK else "—"
    text = (
        "🖥 <b>وضعیت سیستم</b>\n\n"
        "<blockquote>"
        f"⏱ Uptime: <b>{uptime_str(time.time() - START_TS)}</b>\n"
        f"🐍 Python {sys.version_info.major}.{sys.version_info.minor} · PTB {PTB_VERSION}\n"
        f"📡 API قیمت: {api}\n"
        f"🔁 آخرین چک: {tick} (هر {settings()['poll_seconds']} ثانیه)"
        "</blockquote>\n\n"
        "<blockquote>"
        f"📋 فعال: <b>{pend + opn}</b> ({pend} Pending · {opn} Open)\n"
        f"📦 بسته‌شده: <b>{closed}</b> (گزارش‌نشده {unrep})\n"
        f"🚫 کنسل‌شده: <b>{cancelled}</b>\n"
        f"📺 کانال‌ها: <b>{len(channels())}</b>\n"
        f"🗄 دیتابیس: <b>{_db_size()}</b>"
        "</blockquote>"
    )
    await show(update, text, M([[B("🔄 بروزرسانی", "adm:status"), B("⬅️ پنل ادمین", "adm:home")]]))


def _reschedule(job_queue):
    for j in job_queue.get_jobs_by_name("monitor"):
        j.schedule_removal()
    job_queue.run_repeating(monitor, interval=settings()["poll_seconds"], first=3, name="monitor")


def _auto_hm() -> tuple:
    h, m = settings()["auto_time"].split(":")
    return int(h), int(m)


def _reschedule_auto(job_queue):
    for j in job_queue.get_jobs_by_name("autoreport"):
        j.schedule_removal()
    if not settings()["auto_on"]:
        return
    h, m = _auto_hm()
    job_queue.run_daily(auto_report, time=dtime(h, m, tzinfo=TZ), name="autoreport")


def _catchup_auto(job_queue):
    """If the bot was down at report time, send today's report shortly after start."""
    s = settings()
    if not s["auto_on"]:
        return
    h, m = _auto_hm()
    now = datetime.now(TZ)
    if s.get("auto_last_date") != now.date().isoformat() and (now.hour, now.minute) >= (h, m):
        job_queue.run_once(auto_report, 15, name="autoreport_catchup")


def parse_hhmm(text: str):
    t = text.translate(trading._DIGITS).strip().replace("：", ":").replace(".", ":")
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?", t)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return f"{h:02d}:{mi:02d}"


def _closed_between(t, a, b) -> bool:
    try:
        ts = datetime.fromisoformat(t["closed_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return a < ts <= b


async def auto_report(context: ContextTypes.DEFAULT_TYPE):
    s = settings()
    if not s["auto_on"]:
        return
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    if s.get("auto_last_ts"):
        try:
            since = max(since, datetime.fromisoformat(s["auto_last_ts"]))
        except ValueError:
            pass
    for ch in list(channels()):
        rows = [t for t in trades()
                if t["channel"] == ch["key"] and t["status"] == "closed"
                and t.get("result_r") is not None and _closed_between(t, since, now)]
        if not rows:
            continue
        if s["auto_mode"] == "profit" and sum(t["result_r"] for t in rows) <= 0.005:
            continue
        try:
            async with LOCK:
                await _post(ch, trading.summary_text(rows, "Daily Report"))
                for t in rows:
                    t["reported"] = True
                save()
        except Exception:
            log.exception("auto report failed for channel %s", ch.get("title"))
    s["auto_last_ts"] = now.isoformat()
    s["auto_last_date"] = now.astimezone(TZ).date().isoformat()
    save()


# ====================== Channel posting (bot is the poster) ======================

def _norm_chat(raw: str):
    raw = raw.strip()
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    m = re.search(r"t\.me/([A-Za-z0-9_]+)", raw)
    name = m.group(1) if m else raw.lstrip("@")
    return "@" + name


async def verify_channel(raw: str) -> dict:
    chat = _norm_chat(raw)
    c = await BOT.get_chat(chat)
    m = await BOT.get_chat_member(c.id, BOT.id)
    if m.status == "creator":
        ok = True
    elif m.status == "administrator":
        ok = bool(getattr(m, "can_post_messages", False)) if c.type == "channel" else True
    else:
        ok = False
    return {"chat": c.id, "title": c.title or str(c.id), "can_post": ok}


async def _post(ch, text, reply_to=None):
    rp = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
    m = await BOT.send_message(chat_id=ch["chat"], text=text, parse_mode=ParseMode.HTML,
                               reply_parameters=rp, disable_web_page_preview=True)
    return m.message_id


async def _post_photo(ch, png: bytes, caption: str, reply_to=None):
    rp = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
    m = await BOT.send_photo(chat_id=ch["chat"], photo=png, caption=caption,
                             parse_mode=ParseMode.HTML, reply_parameters=rp)
    return m.message_id


async def _post_open(ch, t, price):
    """'Position Opened' reply. With entry image on: chart photo + caption, else plain text."""
    text = trading.open_text(t)
    s = settings()
    if s.get("img_on"):
        try:
            png = await chart.snapshot(await trading._sess(), t, s.get("img_tf", "15m"),
                                       price, BOT_NAME)
            if png:
                return await _post_photo(ch, png, text, t["pending_msg_id"])
        except Exception:
            log.exception("entry image failed for trade %s, sending text", t.get("id"))
    return await _post(ch, text, t["pending_msg_id"])


# ====================== Commands (/start, /help only) ======================

async def _deny(update: Update):
    uid = update.effective_user.id if update.effective_user else 0
    await update.effective_message.reply_text(
        "⛔ شما دسترسی ادمین ندارید.\n"
        f"آیدی شما: <code>{uid}</code>\n"
        "این آیدی را به مالک بات بدهید تا شما را اضافه کند.",
        parse_mode=ParseMode.HTML)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or not is_admin(u.id):
        await _deny(update)
        return
    await reset_flow(context.user_data)
    await say(update, "👇 برای باز کردن منو، دکمه‌ی «منو» را بزن.", main_kb())
    await view_home(update)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or not is_admin(u.id):
        await _deny(update)
        return
    await say(update, HELP, home_kb())


# ====================== Input flows ======================

async def handle_state(update, context, st, text):
    ud = context.user_data
    if st == "ch_chat":
        try:
            info = await verify_channel(text)
        except Exception as ex:
            await say(update, f"❌ کانال پیدا نشد: <code>{esc(str(ex))}</code>\n"
                              "بات را به کانال اضافه کن و ادمین کن، بعد دوباره آیدی را بفرست.", cancel_kb())
            return
        if not info["can_post"]:
            await say(update, "❌ بات در این کانال ادمین با دسترسی «ارسال پست» نیست.\n"
                              "دسترسی را بده و دوباره آیدی را بفرست.", cancel_kb())
            return
        if any(str(c["chat"]) == str(info["chat"]) for c in channels()):
            await say(update, "ℹ️ این کانال قبلاً اضافه شده است.", cancel_kb())
            return
        ud.pop("state", None)
        add_channel(info["chat"], info["title"])
        await say(update, f"✅ کانال «{esc(info['title'])}» اضافه شد.",
                  M([[B("⬅️ بازگشت", "menu:channels")]]))
    elif st == "adm_add":
        if not re.fullmatch(r"\d{5,15}", text):
            await say(update, "❌ آیدی عددی تلگرام را بفرست (مثلاً <code>123456789</code>).", cancel_kb())
            return
        uid = int(text)
        if uid in ADMIN_IDS or uid in db()["admins"]:
            await say(update, "ℹ️ این کاربر از قبل ادمین است.", cancel_kb())
            return
        db()["admins"].append(uid)
        save()
        ud.pop("state", None)
        await say(update, f"✅ کاربر <code>{uid}</code> به ادمین‌ها اضافه شد.",
                  M([[B("⬅️ بازگشت", "adm:list")]]))
    elif st == "set_rewards":
        try:
            v = float(text.translate(trading._DIGITS).replace(",", "."))
            if not 0.25 <= v <= 50:
                raise ValueError
        except ValueError:
            await say(update, "❌ فقط یک عدد بین 0.25 تا 50 بفرست. مثلاً <code>1</code> یا <code>2</code>",
                      cancel_kb())
            return
        settings()["reward_every"] = v
        settings()["reward_on"] = True
        save()
        ud.pop("state")
        await say(update, f"✅ از این به بعد هر <b>{fmt_step(v)}</b> ریوارد پست می‌شود.",
                  M([[B("⬅️ بازگشت", "menu:settings")]]))
    elif st == "set_be":
        try:
            v = float(text)
            if v < 0:
                raise ValueError
        except ValueError:
            await say(update, "❌ یک عدد بفرست (۰ = غیرفعال).", cancel_kb())
            return
        settings()["be_after"] = v
        save()
        ud.pop("state")
        await say(update, "✅ ذخیره شد.", M([[B("⬅️ بازگشت", "menu:settings")]]))
    elif st == "set_autotime":
        hhmm = parse_hhmm(text)
        if not hhmm:
            await say(update, "❌ ساعت نامعتبر است. مثلاً <code>21:30</code> بفرست.", cancel_kb())
            return
        settings()["auto_time"] = hhmm
        save()
        ud.pop("state", None)
        _reschedule_auto(context.job_queue)
        await say(update, f"✅ ساعت گزارش خودکار روی <b>{hhmm}</b> تنظیم شد.",
                  M([[B("⬅️ بازگشت", "menu:auto")]]))
    elif st == "set_poll":
        if not text.isdigit() or not 2 <= int(text) <= 60:
            await say(update, "❌ عددی بین 2 تا 60 بفرست.", cancel_kb())
            return
        settings()["poll_seconds"] = int(text)
        save()
        ud.pop("state")
        _reschedule(context.job_queue)
        await say(update, "✅ ذخیره شد.", M([[B("⬅️ بازگشت", "menu:settings")]]))


async def handle_signal(update, context, text):
    parsed = trading.parse_signal(text)
    if isinstance(parsed, str):
        await say(update, f"❌ {esc(parsed)}\n\nقالب درست:\n{SIGNAL_FORMAT}", back_kb())
        return
    if not channels():
        await say(update, "❌ اول یک کانال اضافه کن.", M([[B("📺 کانال‌ها", "menu:channels")]]))
        return
    price = await trading.get_price(parsed["pair"])
    if price is None:
        await say(update, f"❌ قیمت <code>{esc(parsed['pair'])}</code> از Binance/Bybit پیدا نشد؛ نماد را چک کن.",
                  back_kb())
        return
    parsed["price"] = price
    parsed["order"] = trading.order_type(parsed["side"], parsed["entry"], price)
    context.user_data["draft"] = parsed
    kb = [[B(f"📤 پست در {c['title']}", f"sig:post:{c['key']}")] for c in channels()]
    kb.append([B("✖️ لغو", "sig:cancel")])
    await say(update,
              f"👀 <b>پیش‌نمایش</b>\n\n{trading.pending_text(parsed)}\n\n"
              f"💵 قیمت لحظه‌ای {esc(parsed['symbol'])}: <b>{fmt_price(price)}</b>", M(kb))


@admin_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == MENU_LABEL:  # Telegram-keyboard button: cancel pending input, open the main menu
        await reset_flow(context.user_data)
        await view_home(update)
        return
    if text in (PEND_LABEL, POS_LABEL):  # persistent-keyboard shortcuts
        await reset_flow(context.user_data)
        await view_trades(update, 0, "p" if text == PEND_LABEL else "o")
        return
    st = context.user_data.get("state")
    if st:
        await handle_state(update, context, st, text)
    else:
        await handle_signal(update, context, text)


RESTORE_HOWTO = (
    "♻️ <b>نحوه‌ی ریستور</b>\n"
    "۱) /start ← 🛡 پنل ادمین ← ♻️ ریستور بکاپ\n"
    "۲) همین فایل را بدون تغییر به‌صورت <b>فایل</b> برای بات بفرست\n"
    "۳) خلاصه را چک کن و «♻️ بله، ریستور کن» را بزن\n"
    "<i>از دیتای فعلی خودکار یک نسخه گرفته می‌شود. فقط مالک می‌تواند ریستور کند.</i>"
)
BACKUP_CAPTION = ("💾 <b>بکاپ دیتابیس</b> (SQLite)\n"
                  "کانال‌ها، معاملات، تاریخچه و تنظیمات\n\n" + RESTORE_HOWTO)

MAX_BACKUP_MB = 20  # Telegram bots can download files up to 20 MB


@admin_only
async def on_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    if ud.get("state") != "restore_wait" or not is_owner(update.effective_user.id):
        await say(update, "📎 برای ریستور، از 🛡 پنل ادمین ← ♻️ ریستور بکاپ شروع کن.", home_kb())
        return
    doc = update.message.document
    if doc.file_size and doc.file_size > MAX_BACKUP_MB * 1024 * 1024:
        await say(update, f"❌ فایل بزرگ‌تر از {MAX_BACKUP_MB} مگابایت است.", cancel_kb())
        return
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        await (await doc.get_file()).download_to_drive(tmp)
        info = inspect_backup(Path(tmp))
    except Exception as ex:
        Path(tmp).unlink(missing_ok=True)
        await say(update, f"❌ {esc(str(ex))}\nیک فایل بکاپ سالم بفرست.", cancel_kb())
        return
    old = ud.pop("restore_file", None)
    if old:
        Path(old).unlink(missing_ok=True)
    ud["restore_file"] = tmp
    await say(update, "♻️ <b>بکاپ معتبر است</b>\n\n<blockquote>"
                      f"📄 {esc(doc.file_name or 'backup.db')}\n"
                      f"📺 کانال‌ها: <b>{info['channels']}</b>\n"
                      f"📋 معاملات: <b>{info['trades']}</b> (فعال: {info['active']})\n"
                      f"🛡 ادمین‌ها: <b>{info['admins']}</b></blockquote>\n"
                      "⚠️ دیتای فعلی با این بکاپ <b>جایگزین</b> می‌شود (یک نسخه از دیتای فعلی نگه داشته می‌شود). ادامه؟",
              confirm_kb("adm:restoreok", "menu:home", "♻️ بله، ریستور کن"))


# ====================== Callbacks ======================

@admin_only
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    p = q.data.split(":")
    a = p[0]
    ud = context.user_data
    uid = update.effective_user.id

    if a == "adm" and len(p) > 1 and p[1] not in ("home", "status", "list") and not is_owner(uid):
        await q.answer("⛔ این بخش فقط برای مالک است.", show_alert=True)
        return
    await q.answer()

    if a == "noop":
        return

    if a == "menu":
        await reset_flow(ud)
        views = {"home": view_home, "channels": view_channels,
                 "trades": view_trades, "settings": view_settings, "sum": view_sum,
                 "stats": view_stats, "help": view_help,
                 "auto": view_auto}
        await views.get(p[1], view_home)(update)

    elif a == "trp":
        await view_trades(update, int(p[1]), p[2] if len(p) > 2 else "a")

    # ---------- auto report ----------
    elif a == "auto":
        sub = p[1]
        s = settings()
        if sub == "toggle":
            s["auto_on"] = not s["auto_on"]
            save()
            _reschedule_auto(context.job_queue)
            await view_auto(update)
        elif sub == "mode":
            s["auto_mode"] = "always" if s["auto_mode"] == "profit" else "profit"
            save()
            await view_auto(update)
        elif sub == "time":
            ud["state"] = "set_autotime"
            presets = [("20:00", "2000"), ("21:00", "2100"), ("22:00", "2200"),
                       ("23:00", "2300"), ("23:55", "2355")]
            kb = M([[B(l, f"auto:t:{v}") for l, v in presets[:3]],
                    [B(l, f"auto:t:{v}") for l, v in presets[3:]],
                    [B("✖️ انصراف", "menu:auto")]])
            await show(update, "⏰ <b>ساعت گزارش خودکار</b>\n\n"
                               f"ساعت را به وقت <b>{esc(trading.TIMEZONE)}</b> بفرست، مثلاً <code>21:30</code>، "
                               "یا از دکمه‌ها انتخاب کن.", kb)
        elif sub == "t":
            v = p[2]
            s["auto_time"] = f"{v[:2]}:{v[2:]}"
            ud.pop("state", None)
            save()
            _reschedule_auto(context.job_queue)
            await view_auto(update)

    # ---------- admin panel ----------
    elif a == "adm":
        sub = p[1]
        if sub == "home":
            await reset_flow(ud)
            await view_admin(update)
        elif sub == "list":
            await view_admins(update)
        elif sub == "status":
            await view_status(update)
        elif sub == "add":
            ud["state"] = "adm_add"
            await show(update, "➕ <b>افزودن ادمین</b>\n\nآیدی عددی تلگرام کاربر را بفرست.\n"
                               "(کاربر می‌تواند با /start آیدی خودش را ببیند.)", cancel_kb())
        elif sub == "del":
            await show(update, f"🗑 ادمین <code>{esc(p[2])}</code> حذف شود؟",
                       confirm_kb(f"adm:delok:{p[2]}", "adm:list"))
        elif sub == "delok":
            aid = int(p[2])
            if aid in db()["admins"]:
                db()["admins"].remove(aid)
                save()
            await view_admins(update)
        elif sub == "backup":
            tmp = None
            try:
                tmp = make_backup()
                name = f"magical_org_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db"
                await q.message.reply_document(document=tmp.read_bytes(), filename=name,
                                               caption=BACKUP_CAPTION, parse_mode=ParseMode.HTML)
            except Exception as ex:
                await q.message.reply_text(f"❌ {esc(str(ex))}", parse_mode=ParseMode.HTML)
            finally:
                if tmp:
                    tmp.unlink(missing_ok=True)
        elif sub == "purge":
            n = sum(1 for t in trades()
                    if t["status"] == "cancelled" or (t["status"] == "closed" and t["reported"]))
            await show(update, f"🧹 <b>پاکسازی تاریخچه</b>\n\n{n} معامله‌ی کنسل‌شده/گزارش‌شده حذف می‌شود. "
                               "معاملات فعال و گزارش‌نشده دست‌نخورده می‌مانند.",
                       confirm_kb("adm:purgeok", "adm:home"))
        elif sub == "purgeok":
            before = len(trades())
            db()["trades"] = [t for t in trades()
                              if not (t["status"] == "cancelled" or (t["status"] == "closed" and t["reported"]))]
            save()
            await show(update, f"✅ {before - len(trades())} معامله پاک شد.",
                       M([[B("⬅️ بازگشت", "adm:home")]]))
        elif sub == "restore":
            await reset_flow(ud)
            ud["state"] = "restore_wait"
            await show(update, "♻️ <b>ریستور بکاپ</b>\n\n"
                               "فایل بکاپ (<code>.db</code>) را همین‌جا به‌صورت <b>فایل</b> بفرست.\n"
                               "قبل از جایگزینی، از دیتای فعلی خودکار بکاپ گرفته می‌شود.", cancel_kb())
        elif sub == "restoreok":
            f = ud.pop("restore_file", None)
            ud.pop("state", None)
            if not f or not Path(f).exists():
                await show(update, "❌ فایل بکاپ پیدا نشد، دوباره بفرست.", M([[B("⬅️ بازگشت", "adm:home")]]))
                return
            try:
                async with LOCK:
                    before = restore_backup(Path(f))
                _reschedule(context.job_queue)
                _reschedule_auto(context.job_queue)
            except Exception as ex:
                log.exception("restore failed")
                Path(f).unlink(missing_ok=True)
                await show(update, f"❌ ریستور ناموفق: <code>{esc(str(ex))}</code>",
                           M([[B("⬅️ بازگشت", "adm:home")]]))
                return
            pend, opn, _ = _counts()
            await show(update, "✅ <b>ریستور انجام شد</b>\n\n<blockquote>"
                               f"📺 کانال‌ها: <b>{len(channels())}</b>\n"
                               f"📋 معاملات: <b>{len(trades())}</b> (فعال: {pend + opn})\n"
                               f"🛡 ادمین‌ها: <b>{len(db()['admins'])}</b></blockquote>\n"
                               "قیمت‌ها از همین الان دوباره چک می‌شوند.", M([[B("⬅️ بازگشت", "adm:home")]]))
            try:
                await q.message.reply_document(document=before.read_bytes(), filename=before.name,
                                               caption="💾 <b>دیتای قبل از ریستور</b> (برای احتیاط)\n\n" + RESTORE_HOWTO,
                                               parse_mode=ParseMode.HTML)
            except Exception:
                pass
        elif sub == "restart":
            await show(update, "🔄 <b>ریستارت بات</b>\n\nبات چند ثانیه آفلاین می‌شود و سرویس systemd دوباره آن را بالا می‌آورد.",
                       confirm_kb("adm:restartok", "adm:home", "✅ ریستارت"))
        elif sub == "restartok":
            await show(update, "🔄 در حال ریستارت… چند ثانیه‌ی دیگر /start را بزن.")
            asyncio.get_running_loop().call_later(1.0, os.kill, os.getpid(), signal.SIGTERM)

    # ---------- channels ----------
    elif a == "ch":
        if p[1] == "add":
            ud["state"] = "ch_chat"
            await show(update, "➕ <b>افزودن کانال</b>\n\n"
                               f"۱) بات (@{esc(BOT.username or '')}) را به کانال اضافه کن و <b>ادمین</b> کن (دسترسی «ارسال پست»).\n"
                               "۲) آیدی کانال را بفرست:\n"
                               "<code>@username</code> یا <code>-100…</code> یا لینک t.me", cancel_kb())
        elif p[1] == "test":
            ch = get_channel(p[2])
            if not ch:
                await view_channels(update)
                return
            try:
                info = await verify_channel(str(ch["chat"]))
                msg = ("✅ بات در این کانال ادمین است و می‌تواند پست بگذارد."
                       if info["can_post"] else "❌ بات دسترسی «ارسال پست» ندارد.")
            except Exception as ex:
                msg = f"❌ <code>{esc(str(ex))}</code>"
            await q.message.reply_text(f"🔌 {esc(ch['title'])}\n{msg}", parse_mode=ParseMode.HTML)
        elif p[1] == "del":
            ch = get_channel(p[2])
            if not ch:
                await view_channels(update)
                return
            if active_trades(p[2]):
                await q.message.reply_text("⚠️ این کانال معامله‌ی فعال دارد؛ اول آن‌ها را ببند یا کنسل کن.")
                return
            await show(update, f"🗑 کانال «{esc(ch['title'])}» حذف شود؟",
                       confirm_kb(f"ch:delok:{p[2]}", "menu:channels"))
        elif p[1] == "delok":
            if not active_trades(p[2]):
                delete_channel(p[2])
            await view_channels(update)

    # ---------- signals ----------
    elif a == "sig":
        if p[1] == "cancel":
            ud.pop("draft", None)
            await show(update, "✖️ لغو شد.", back_kb())
            return
        draft = ud.pop("draft", None)
        ch = get_channel(p[2])
        if not draft or not ch:
            await show(update, "⌛️ پیش‌نویس منقضی شده؛ سیگنال را دوباره بفرست.", back_kb())
            return
        async with LOCK:
            t = create_trade(draft, ch)
            try:
                t["pending_msg_id"] = await _post(ch, trading.pending_text(t))
                save()
            except Exception as ex:
                remove_trade(t["id"])
                await show(update, f"❌ ارسال ناموفق: <code>{esc(str(ex))}</code>", back_kb())
                return
        await show(update, f"✅ معامله‌ی <b>#{t['id']}</b> در «{esc(ch['title'])}» پست شد و زیر نظر است.",
                   M([[B("📋 معاملات فعال", "menu:trades"), B("⬅️ بازگشت", "menu:home")]]))

    # ---------- trades ----------
    elif a == "tr":
        tid = int(p[2])
        kind = p[3] if len(p) > 3 else "a"
        t = get_trade(tid)
        async with LOCK:
            ch = t and get_channel(t["channel"])
            if not t or not ch:
                await view_trades(update, 0, kind)
                return
            try:
                if p[1] == "cancel" and t["status"] == "pending":
                    await _post(ch, trading.cancel_text(t), t["pending_msg_id"])
                    t["status"] = "cancelled"
                elif p[1] == "be" and t["status"] == "open":
                    if t["be_active"]:
                        return
                    cur = await trading.get_price(t["pair"])
                    if cur is None:
                        await q.message.reply_text("❌ قیمت در دسترس نیست.")
                        return
                    risk = abs(t["entry"] - t["sl"])
                    d = 1 if t["side"] == "LONG" else -1
                    if d * (cur - t["entry"]) / risk <= 0:
                        await q.message.reply_text("قیمت هنوز بالاتر از Entry نیست؛ بریک‌اون ممکن نیست.")
                        return
                    await _post(ch, trading.be_set_text(t), t["open_msg_id"])
                    t["be_active"] = True
                elif p[1] == "close" and t["status"] == "open":
                    cur = await trading.get_price(t["pair"])
                    if cur is None:
                        await q.message.reply_text("❌ قیمت در دسترس نیست.")
                        return
                    risk = abs(t["entry"] - t["sl"])
                    d = 1 if t["side"] == "LONG" else -1
                    t["result_r"] = round(d * (cur - t["entry"]) / risk, 2)
                    t["outcome"] = "manual"
                    await _post(ch, trading.final_text(t), t["open_msg_id"])
                    t["status"] = "closed"
                    t["closed_at"] = datetime.now(timezone.utc).isoformat()
                save()
            except Exception as ex:
                await q.message.reply_text(f"❌ {esc(str(ex))}", parse_mode=ParseMode.HTML)
                return
        await view_trades(update, 0, kind)

    # ---------- settings ----------
    elif a == "set":
        if p[1] == "rewtoggle":
            settings()["reward_on"] = not settings()["reward_on"]
            save()
            await view_settings(update)
            return
        if p[1] == "imgtoggle":
            settings()["img_on"] = not settings()["img_on"]
            save()
            await view_settings(update)
            return
        if p[1] == "imgtf":
            tfs = chart.TIMEFRAMES
            cur_tf = settings().get("img_tf", "15m")
            settings()["img_tf"] = tfs[(tfs.index(cur_tf) + 1) % len(tfs)] if cur_tf in tfs else "15m"
            save()
            await view_settings(update)
            return
        if p[1] == "imgtest":
            price = await trading.get_price("BTCUSDT")
            if price is None:
                await q.message.reply_text("❌ قیمت BTC دریافت نشد.")
                return
            demo = {"pair": "BTCUSDT", "symbol": "BTC", "side": "LONG", "entry": price,
                    "sl": round(price * 0.995, 1), "tp": round(price * 1.01, 1), "no": 0}
            png = await chart.snapshot(await trading._sess(), demo,
                                       settings().get("img_tf", "15m"), price, BOT_NAME)
            if not png:
                await q.message.reply_text("❌ ساخت عکس ناموفق بود (کندل‌ها دریافت نشد).")
                return
            await q.message.reply_photo(png, caption="👁 نمونه‌ی عکس ورود (BTC LONG، 1:2)")
            return
        ud["state"] = {"rewards": "set_rewards", "be": "set_be", "poll": "set_poll"}[p[1]]
        prompts = {
            "rewards": "🏆 <b>فاصله‌ی ریوارد</b>\n\n"
                       "فقط یک عدد بفرست؛ هر چند R یک‌بار ریوارد پست شود:\n\n"
                       "<code>1</code> ← 1R، 2R، 3R …\n"
                       "<code>2</code> ← 2R، 4R، 6R …\n"
                       "<code>0.5</code> ← 0.5R، 1R، 1.5R …\n\n"
                       "برای خاموش کردن کامل، از دکمه‌ی «خاموش کردن ریوارد» در تنظیمات استفاده کن.",
            "be": "🛡 بعد از رسیدن به کدام ریوارد SL به Entry منتقل شود؟ مثلاً <code>1</code>\n"
                  "برای غیرفعال‌سازی: <code>0</code>",
            "poll": "⏱ هر چند ثانیه قیمت چک شود؟ (2 تا 60)",
        }
        await show(update, prompts[p[1]], cancel_kb())

    # ---------- report ----------
    elif a == "sum":
        key = p[2]
        ch = get_channel(key)
        async with LOCK:
            rows = [t for t in trades()
                    if t["channel"] == key and t["status"] == "closed" and not t["reported"]]
            if not ch or not rows:
                await show(update, "ℹ️ معامله‌ی بسته‌شده‌ی گزارش‌نشده‌ای نیست.", back_kb())
                return
            try:
                await _post(ch, trading.summary_text(rows))
            except Exception as ex:
                await show(update, f"❌ <code>{esc(str(ex))}</code>", back_kb())
                return
            for t in rows:
                t["reported"] = True
            save()
        await show(update, f"✅ گزارش {len(rows)} معامله ارسال شد.", back_kb())


# ====================== Price monitor ======================

async def _process(t, cur):
    ch = get_channel(t["channel"])
    if not ch:
        return
    prev = t.get("last_price") or cur

    if t["status"] == "pending":
        e = t["entry"]
        if (prev - e) * (cur - e) > 0:
            t["last_price"] = cur
            return
        t["open_msg_id"] = await _post_open(ch, t, cur)
        t["status"] = "open"
        t["opened_at"] = datetime.now(timezone.utc).isoformat()
        save()

    risk = abs(t["entry"] - t["sl"])
    d = 1 if t["side"] == "LONG" else -1
    r = d * (cur - t["entry"]) / risk
    steps = [s for s in t["steps"] if s < t["rr"]] if settings()["reward_on"] else []

    if r >= t["rr"]:
        for s in steps:
            if s not in t["rewards_hit"]:
                t["rewards_hit"].append(s)
        t["result_r"] = t["rr"]
        t["outcome"] = "tp"
        await _post(ch, trading.final_text(t), t["open_msg_id"])
        t["status"] = "closed"
        t["closed_at"] = datetime.now(timezone.utc).isoformat()
    else:
        for s in steps:
            if r >= s and s not in t["rewards_hit"]:
                await _post(ch, trading.reward_text(t, s), t["open_msg_id"])
                t["rewards_hit"].append(s)
                save()
        if t["be_after"] and not t["be_active"] and r >= t["be_after"]:
            t["be_active"] = True
            await _post(ch, trading.be_set_text(t), t["open_msg_id"])
            save()
        floor = 0.0 if t["be_active"] else -1.0
        if r <= floor:
            t["result_r"] = 0.0 if t["be_active"] else -1.0
            t["outcome"] = "be" if t["be_active"] else "sl"
            await _post(ch, trading.final_text(t), t["open_msg_id"])
            t["status"] = "closed"
            t["closed_at"] = datetime.now(timezone.utc).isoformat()
    t["last_price"] = cur
    save()


async def monitor(context: ContextTypes.DEFAULT_TYPE):
    global LAST_TICK
    LAST_TICK = time.time()
    active = active_trades()
    if not active:
        return
    cache = {}
    for t in active:
        try:
            pair = t["pair"]
            if pair not in cache:
                cache[pair] = await trading.get_price(pair)
            if cache[pair] is None:
                continue
            async with LOCK:
                if t["status"] in ("pending", "open"):
                    await _process(t, cache[pair])
        except Exception:
            log.exception("monitor error on trade %s", t.get("id"))


# ====================== Startup ======================

async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("unhandled error", exc_info=context.error)


async def post_init(app: Application):
    global BOT
    BOT = app.bot
    try:
        me = await app.bot.get_me()
        if me.first_name != BOT_NAME:
            await app.bot.set_my_name(BOT_NAME)
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        await app.bot.set_my_commands([
            BotCommand("menu", "منوی اصلی"),
            BotCommand("start", "پنل مدیریت"),
            BotCommand("help", "راهنما"),
        ])
    except Exception as ex:
        log.warning("bot profile setup failed: %s", ex)
    _reschedule(app.job_queue)
    _reschedule_auto(app.job_queue)
    _catchup_auto(app.job_queue)


def main():
    if not BOT_TOKEN or not ADMIN_IDS:
        raise SystemExit("Set BOT_TOKEN and ADMIN_IDS in the .env file.")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("menu", cmd_menu, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("help", cmd_help, filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, on_doc))
    app.add_error_handler(on_error)
    db()  # open the database (and import data.json on first run)
    try:
        app.run_polling()
    finally:
        try:
            save()
            get_conn().close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
