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
from telegram import ReplyKeyboardMarkup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup as M, InputMediaPhoto, ReplyParameters, Update
from telegram import __version__ as PTB_VERSION
try:
    from telegram import CopyTextButton  # python-telegram-bot >= 21.7
except ImportError:  # older library: the copy button is simply left out
    CopyTextButton = None
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut
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
                 "backup_on": False, "backup_every_days": 7, "backup_last_ts": "",
                 "img_events": {"pending": False, "entry": False, "reward": False, "tp": False,
                                "sl": False, "cancel": False},
                 "img_labels": False,
                 "near_on": False, "near_pct": 30, "near_basis": "sl",
                 "tp_cancel_on": False, "tp_cancel_mode": "delete", "opp_cancel_on": True,
                 "img_tf": "15m",
                 "owners_extra": [], "see_all": [], "names": {}},
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
    if "img_events" not in st and "img_on" in st:  # migrate old single "entry image" switch
        st["img_events"] = {"entry": bool(st.pop("img_on"))}
    if st and st.get("near_basis") != "sl":  # near % now = share of the Entry->SL distance (1R)
        if not 1 <= float(st.get("near_pct", 30)) <= 100:
            st["near_pct"] = 30
        st["near_basis"] = "sl"
    for k, v in DEFAULTS.items():
        d.setdefault(k, copy.deepcopy(v))
    for k, v in DEFAULTS["settings"].items():
        d["settings"].setdefault(k, copy.deepcopy(v))
    for k, v in DEFAULTS["settings"]["img_events"].items():
        d["settings"]["img_events"].setdefault(k, v)


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


def mine(t, uid) -> bool:
    """Does trade `t` belong to admin `uid`? Old trades (saved before owners existed,
    no "owner" field) belong to the bot owners."""
    if sees_all(uid):
        return True
    o = t.get("owner")
    return (o == uid) if o is not None else is_owner(uid)


def my_trades(uid) -> list:
    return [t for t in trades() if mine(t, uid)]


def _uid(update):
    u = update.effective_user
    return u.id if u else None


def active_trades(channel_key: str = None, uid=None) -> list:
    """uid=None -> every admin's trades (price monitor, channel checks); uid=<id> -> only theirs."""
    return [t for t in trades() if t["status"] in ("waiting", "pending", "open")
            and (channel_key is None or t["channel"] == channel_key)
            and (uid is None or mine(t, uid))]


def _ref_price(t):
    """Market price when the signal was sent (reference for the near-entry distance)."""
    return t.get("signal_price") or t.get("price") or t.get("last_price") or t["entry"]


def near_band(t) -> float:
    """Distance from Entry (price units) at which a waiting signal gets posted:
    near_pct % of the stop-loss distance |Entry - SL| (1R).
    e.g. Entry 86,930 · SL 86,280 → 650 · 30% → 195 → post within 195 of Entry."""
    return abs(t["entry"] - t["sl"]) * float(settings().get("near_pct", 30)) / 100


def near_trigger(t) -> float:
    """The price at which a waiting signal gets posted (on the market side of Entry)."""
    ref = _ref_price(t)
    return t["entry"] + (near_band(t) if ref >= t["entry"] else -near_band(t))


def pct(v) -> str:
    return f"{float(v):g}"


def is_near(t, price) -> bool:
    return abs(price - t["entry"]) <= near_band(t)


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


def create_trade(draft: dict, channel: dict, owner=None) -> dict:
    d = db()
    d["trade_counter"] += 1
    channel["counter"] += 1
    s = settings()
    t = {
        "id": d["trade_counter"],
        "no": channel["counter"],
        "channel": channel["key"],
        "owner": owner,
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
        "signal_price": draft["price"],  # market price at signal time (near-entry reference)
    }
    for k in ("pair", "symbol", "side", "entry", "tp", "sl", "rr", "order"):
        t[k] = draft[k]
    t["cancel"] = draft.get("cancel")
    t["riskfree"] = draft.get("riskfree")
    d["trades"].append(t)
    save()
    return t


def remove_trade(tid: int):
    db()["trades"] = [t for t in trades() if t["id"] != tid]
    save()


# ====================== Access control ======================

def owner_ids() -> list:
    """Fixed owners (.env) first, then admins promoted to owner from the bot."""
    extra = [i for i in settings().get("owners_extra", []) if i not in ADMIN_IDS]
    return sorted(ADMIN_IDS) + extra


def is_owner(uid: int) -> bool:
    return uid in ADMIN_IDS or uid in settings().get("owners_extra", [])


def sees_all(uid) -> bool:
    """Owner who switched on «see & manage everyone's trades»."""
    return is_owner(uid) and uid in settings().get("see_all", [])


def remember_user(u):
    """Keep name / @username of everyone who uses the bot, so the team list is readable."""
    if not u:
        return
    n = " ".join(x for x in (u.first_name, u.last_name) if x) or ""
    rec = {"n": n, "u": u.username or ""}
    names = settings().setdefault("names", {})
    if names.get(str(u.id)) != rec:
        names[str(u.id)] = rec
        save()


def who(uid: int) -> str:
    r = settings().get("names", {}).get(str(uid)) or {}
    name = esc(r.get("n") or "") or "کاربر"
    return name + (f"  <i>@{esc(r['u'])}</i>" if r.get("u") else "")


def who_name(uid) -> str:
    """Name only (no @username) of whoever set a trade; '' if unknown / old trade."""
    if uid is None:
        return ""
    r = settings().get("names", {}).get(str(uid)) or {}
    return esc(r.get("n") or "") or (esc("@" + r["u"]) if r.get("u") else "کاربر")


def is_admin(uid: int) -> bool:
    return is_owner(uid) or uid in db()["admins"]


def admin_only(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if not u or not is_admin(u.id):
            return
        remember_user(u)
        return await fn(update, context)
    return wrapper


# ====================== UI helpers ======================

LOCK = asyncio.Lock()
esc = html.escape

# <pre> block (monospace, one block). The home screen also has a copy button for all lines.
SIGNAL_FORMAT = "<pre>BTC\nInt\nTp\nSl\nCancel\nRiskFree</pre>"
SIGNAL_TEXT = "BTC\nInt\nTp\nSl\nCancel\nRiskFree"  # exactly what the copy button puts on the clipboard
SIGNAL_EXAMPLE = "<pre>btc\nInt81000\nTp87000\nSl80000\nCancel84000\nRiskFree83000</pre>"

HELP = (
    "📖 <b>راهنما</b>\n\n"
    "<b>ارسال سیگنال</b>\n"
    "قالب را با یک لمس کپی کن، عددها را بنویس و بفرست:\n"
    f"{SIGNAL_FORMAT}\n"
    "نمونه:\n"
    f"{SIGNAL_EXAMPLE}\n"
    "• <code>Int</code> = Entry ، <code>Tp</code> = Take Profit ، <code>Sl</code> = Stop Loss\n"
    "• <code>Cancel</code> اختیاری است (خالی = غیرفعال): اگر پندینگ فعال نشده و قیمت از Cancel رد شود، پندینگ کنسل می‌شود.\n"
    "• <code>RiskFree</code> اختیاری است (خالی = غیرفعال): وقتی پوزیشن باز است و قیمت به RiskFree برسد، "
    "SL به Entry منتقل می‌شود (ریسک‌فری) و در کانال اعلام می‌شود. اگر تنظیم شود، جایگزین Break-even بر اساس R است. باید بین Entry و TP باشد.\n"
    "• جهت (LONG/SHORT) و Risk/Reward خودکار محاسبه می‌شود.\n"
    "• پیش‌نمایش می‌آید؛ کانال را انتخاب کن تا خود بات در آن کانال پست بگذارد.\n• بات باید در کانال <b>ادمین</b> با دسترسی «ارسال پست» باشد.\n\n"
    "<b>چرخه‌ی معامله</b>\n"
    "Pending ← Position Opened ← Reward ها ← TP / SL\n"
    "همه‌ی مراحل به‌صورت ریپلای روی پست اصلی ارسال می‌شوند.\n\n"
    "<b>ویرایش</b>\n"
    "در لیست پندینگ/پوزیشن روی نام معامله (مثلاً <code>#12 BTC ↑</code>) بزن و «📋 کپی قالب» را بزن؛ "
    "عدد را عوض کن و بفرست؛ بات خودش می‌فهمد چه چیزی عوض شده.\n"
    "پندینگ: Int / Tp / Sl / Cancel / RiskFree (Cancel0 / RiskFree0 = حذف) ، "
    "پوزیشن باز: Tp / Sl / RiskFree (تا وقتی ریسک‌فری نشده). تغییر Entry / TP / SL در کانال اعلام می‌شود؛ "
    "تغییر Cancel و RiskFree فقط برای ادمین ذخیره می‌شود.\n\n"
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


def home_kb(uid=None):
    copy_row = ([[InlineKeyboardButton("📋 کپی قالب سیگنال", copy_text=CopyTextButton(text=SIGNAL_TEXT))]]
                if CopyTextButton else [])
    return M(copy_row + [
        [B("📈 عملکرد", "menu:stats"), B("📺 کانال‌ها", "menu:channels")],
        [B("⚙️ تنظیمات", "menu:settings")] + ([B("🛡 پنل ادمین", "adm:home")] if uid is not None and is_owner(uid) else []),
        [B("❓ راهنما", "menu:help")],
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


def _counts(uid=None):
    tr = trades() if uid is None else my_trades(uid)
    pend = sum(1 for t in tr if t["status"] in ("waiting", "pending"))
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
    pend, opn, unrep = _counts(_uid(update))
    text = (
        f"<b>{BOT_NAME}</b> · پنل مدیریت\n\n"
        "<blockquote>"
        f"📋 معاملات فعال: <b>{pend + opn}</b>  ({pend} Pending · {opn} Open)\n"
        f"📊 گزارش‌نشده: <b>{unrep}</b>\n"
        f"📺 کانال‌ها: <b>{len(channels())}</b>"
        "</blockquote>"
    )
    await show(update, text, home_kb(_uid(update)))


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
    "p": ("◷ پندینگ‌ها", "پندینگی وجود ندارد.", ("waiting", "pending")),
    "o": ("● پوزیشن‌های فعال", "پوزیشن بازی وجود ندارد.", ("open",)),
    "a": ("معاملات فعال", "معامله‌ی فعالی وجود ندارد.", ("waiting", "pending", "open")),
}


async def view_trades(update, page: int = 0, kind: str = "a", notice: str = ""):
    kind = kind if kind in TRADE_VIEWS else "a"
    title, empty_msg, statuses = TRADE_VIEWS[kind]
    active = [t for t in active_trades(uid=_uid(update)) if t["status"] in statuses]
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
            risk = trading.risk_of(t)
            d = 1 if t["side"] == "LONG" else -1
            r = d * (cur - t["entry"]) / risk

        # header + status
        waiting = t["status"] == "waiting"
        status = "Open" if is_open else (
            f"⌖ Waiting · @{fmt_price(near_trigger(t))}" if waiting else "Pending")
        if is_open and r is not None:
            status += f"  ·  <b>{fmt_r(r)}</b>"
        chn = get_channel(t["channel"])
        setter = who_name(t.get("owner"))
        chn_name = esc(chn['title']) if chn else '—'
        head = (f"<b>#{t['id']}  {esc(t['symbol'])} {t['side']}</b>  ·  {status}"
                f"  |  {setter + ' → ' if setter else ''}{chn_name}")

        # body
        body = [f"Entry {fmt_price(t['entry'])}   TP {fmt_price(t['tp'])}   SL {fmt_price(t['sl'])}"]
        if not is_open and t.get("cancel") is not None:
            body[0] += f"   Cancel {fmt_price(t['cancel'])}"
        if t.get("riskfree") is not None and not t.get("be_active"):
            body[0] += f"   RF {fmt_price(t['riskfree'])}"
        extra = []
        if t["rewards_hit"]:
            extra.append("Rewards " + " ".join(fmt_step(s) for s in sorted(t["rewards_hit"])))
        if t.get("be_active"):
            extra.append("BE")
        if extra:
            body.append("   ·   ".join(extra))
        blocks.append(head + "\n" + "\n".join(body))

        # buttons: [name] [Send Pic] [break-even] [close]   /   [name] [Send Pic] [cancel]
        name = edit_button(t, f"#{t['id']} {t['symbol']} {arrow}", kind)
        pic = B("Send Pic", f"tr:pic:{t['id']}:{kind}")
        if is_open:
            be = (B("BE ✓", "noop") if t.get("be_active")
                  else B("Break-even", f"tr:be:{t['id']}:{kind}"))
            kb.append([name, pic, be, B("Close", f"tr:close:{t['id']}:{kind}")])
        elif waiting:  # not in the channel yet: no picture, cancel silently
            kb.append([name, B("Cancel", f"tr:cancel:{t['id']}:{kind}")])
        else:
            kb.append([name, pic, B("Cancel", f"tr:cancel:{t['id']}:{kind}")])
    if pages > 1:
        kb.append([B("‹", f"trp:{max(page - 1, 0)}:{kind}"), B(f"{page + 1}/{pages}", "noop"),
                   B("›", f"trp:{min(page + 1, pages - 1)}:{kind}")])
    kb.append([B("بروزرسانی", f"trp:{page}:{kind}")])
    # one current-price line at the bottom instead of "Now" under every trade
    seen = []
    for t in chunk:
        if t["pair"] not in [x[0] for x in seen]:
            seen.append((t["pair"], t["symbol"]))
    if len(seen) == 1:
        cp = prices.get(seen[0][0])
        cp_line = f"Current price: <b>{fmt_price(cp) if cp is not None else '—'}</b>"
    else:
        cp_line = "Current price:  " + "   ·   ".join(
            f"{esc(sym)} <b>{fmt_price(prices[pr]) if prices.get(pr) is not None else '—'}</b>"
            for pr, sym in seen)
    text = ("\n" + sep + "\n").join(blocks) + "\n" + sep + "\n" + cp_line
    if notice:
        text = notice + "\n\n" + text
    await show(update, text, M(kb))


def _unreported(key: str, uid=None) -> list:
    return [t for t in (trades() if uid is None else my_trades(uid))
            if t["channel"] == key and t["status"] == "closed" and not t["reported"]]


def _report_kb(uid=None) -> list:
    """One-tap report buttons: one per channel with unreported closed trades (+ all)."""
    rows, total = [], 0
    for c in channels():
        n = len(_unreported(c["key"], uid))
        if n:
            total += n
            rows.append([B(f"📤 ارسال گزارش · {c['title']} ({n})", f"sum:go:{c['key']}")])
    if len(rows) > 1:
        rows.append([B(f"📤 ارسال به همه‌ی کانال‌ها ({total})", "sum:all")])
    return rows


async def view_stats(update, notice: str = ""):
    uid = _uid(update)
    closed = [t for t in my_trades(uid) if t["status"] == "closed" and t["result_r"] is not None]
    if not closed:
        pend, opn, _ = _counts(uid)
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
    rep_rows = _report_kb(uid)
    if not rep_rows:
        lines += ["", "📤 همه‌ی معاملات بسته‌شده گزارش شده‌اند."]
    if notice:
        lines = [notice, ""] + lines
    await show(update, "\n".join(lines), M(rep_rows + [[B("⬅️ بازگشت", "menu:home")]]))


IMG_EVENTS = [("pending", "پندینگ"), ("entry", "ورود"), ("reward", "ریوارد"), ("tp", "TP"),
              ("sl", "استاپ"), ("cancel", "کنسل")]
ON, OFF = "✓", "✗"  # monochrome ticks for toggle buttons


def _img_brief(s: dict) -> str:
    on = [label for k, label in IMG_EVENTS if s["img_events"].get(k)]
    if not on:
        return "خاموش"
    return ("همه" if len(on) == len(IMG_EVENTS) else "، ".join(on)) + f" · {s['img_tf']}"


async def view_settings(update):
    s = settings()
    rw = f"هر {fmt_step(s['reward_every'])}" if s["reward_on"] else "خاموش"
    be = f"بعد از {fmt_step(s['be_after'])}" if s["be_after"] else "خاموش"
    text = (
        "⚙ <b>تنظیمات</b>\n\n"
        "<blockquote>"
        f"◇ ریوارد  ·  <b>{rw}</b>\n"
        f"⇅ SL به Entry  ·  <b>{be}</b>\n"
        f"◷ گزارش خودکار  ·  <b>{_auto_brief(s)}</b>\n"
        f"◷ تنظیمات پندینگ  ·  <b>{_pend_brief(s)}</b>\n"
        f"▣ عکس پست‌ها  ·  <b>{_img_brief(s)}</b>"
        "</blockquote>"
    )
    kb = M([
        [B(f"{ON if s['reward_on'] else OFF} پست ریوارد", "set:rewtoggle"),
         B("◇ فاصله‌ی ریوارد", "set:rewards")],
        [B("⇅ Break-even", "set:be"), B("◷ گزارش خودکار", "menu:auto")],
        [B("◷ تنظیمات پندینگ", "set:pend"), B("▣ عکس پست‌ها", "set:img")],
    ] + ([[B(f"👁 دیدن و مدیریت معاملات همه: {'روشن ✓' if sees_all(update.effective_user.id) else 'خاموش ✗'}",
             "set:seeall")]] if is_owner(update.effective_user.id) else []) + [
        [B("‹ بازگشت", "menu:home")],
    ])
    await show(update, text, kb)


def _pend_brief(s: dict) -> str:
    parts = []
    if s.get("near_on"):
        parts.append(f"نزدیک ورود {pct(s['near_pct'])}%")
    if s.get("tp_cancel_on"):
        parts.append("کنسل با TP")
    if s.get("opp_cancel_on", True):
        parts.append("کنسل خلاف جهت")
    return " · ".join(parts) if parts else "خاموش"


async def view_pend(update):
    s = settings()
    del_mode = s.get("tp_cancel_mode", "delete") == "delete"
    near = (pct(s['near_pct']) + '% فاصله‌ی SL') if s.get('near_on') else 'خاموش'
    tpc = (("حذف پست" if del_mode else "پست کنسل") if s.get("tp_cancel_on") else "خاموش")
    opp = "روشن" if s.get("opp_cancel_on", True) else "خاموش"
    text = (
        "◷ <b>تنظیمات پندینگ</b>\n\n"
        "<blockquote>"
        f"⌖ ارسال نزدیک ورود  ·  <b>{near}</b>\n"
        f"⊘ کنسل با رسیدن به TP  ·  <b>{tpc}</b>\n"
        f"⇄ کنسل پندینگ خلاف جهت  ·  <b>{opp}</b>"
        "</blockquote>\n"
        "<i>کنسل با TP: اگر قیمت بدون فعال‌کردن Entry به TP برسد، پندینگ کنسل می‌شود.\n"
        "خلاف جهت: وقتی پندینگ Buy یک نماد فعال شود، پندینگ‌های Sell همان نماد در همان کانال کنسل می‌شوند (و برعکس).\n"
        "حذف پست = پیام پندینگ از کانال پاک می‌شود · پست کنسل = ریپلای کنسل زیرش می‌آید</i>"
    )
    kb = M([
        [B(f"{ON if s.get('near_on') else OFF} ارسال نزدیک ورود", "set:neartoggle"),
         B("⌖ درصد نزدیکی", "set:near")],
        [B(f"{ON if s.get('tp_cancel_on') else OFF} کنسل با TP", "set:tpctoggle"),
         B("⊘ حالت: " + ("حذف پست" if del_mode else "پست کنسل"), "set:tpcmode")],
        [B(f"{ON if s.get('opp_cancel_on', True) else OFF} کنسل پندینگ خلاف جهت", "set:opptoggle")],
        [B("‹ بازگشت", "menu:settings")],
    ])
    await show(update, text, kb)


async def view_img(update):
    s = settings()
    ev = s["img_events"]
    text = (
        "▣ <b>عکس پست‌ها</b>\n\n"
        "هر مرحله‌ای که تیک بخورد، پستش در کانال همراه با چارت "
        "(کندل‌ها + باکس Long/Short) فرستاده می‌شود؛ بقیه فقط متن.\n\n"
        "<blockquote>"
        f"▣ فعال  ·  <b>{_img_brief(s)}</b>\n"
        f"◷ تایم‌فریم چارت  ·  <b>{s['img_tf']}</b>\n"
        f"▭ برچسب‌های باکس  ·  <b>{'روشن' if s.get('img_labels') else 'خاموش'}</b>"
        "</blockquote>\n"
        "<i>برچسب‌ها = Target / Stop / Entry و R/R روی خود باکس</i>"
    )
    btn = [B(f"{ON if ev.get(k) else OFF} {label}", f"set:imgev:{k}") for k, label in IMG_EVENTS]
    all_on = all(ev.get(k) for k, _ in IMG_EVENTS)
    kb = M([
        btn[0:2], btn[2:4], btn[4:6],
        [B(f"{ON if all_on else OFF} همه", "set:imgall"),
         B(f"{ON if s.get('img_labels') else OFF} برچسب‌های باکس", "set:imglabels")],
        [B(f"◷ تایم‌فریم: {s['img_tf']}", "set:imgtf"), B("◎ نمونه‌ی عکس", "set:imgtest")],
        [B("‹ بازگشت", "menu:settings")],
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


# ---------- Admin panel ----------

async def view_admin(update):
    uid = update.effective_user.id
    owner = is_owner(uid)
    text = (
        "🛡 <b>پنل ادمین</b>\n\n"
        "<blockquote>"
        f"◉ نقش  ·  <b>{'مالک' if owner else 'ادمین'}</b>\n"
        f"# آیدی  ·  <code>{uid}</code>\n"
        f"◎ مالکان  ·  <b>{len(owner_ids())}</b>     ◌ ادمین‌ها  ·  <b>{len([a for a in db()['admins'] if not is_owner(a)])}</b>"
        "</blockquote>"
    )
    if owner:
        text += f"\n\n<blockquote>⟳ چک قیمت  ·  <b>هر {settings()['poll_seconds']} ثانیه</b></blockquote>"
    rows = [[B("◎ ادمین‌ها", "adm:list"), B("▤ وضعیت سیستم", "adm:status")]]
    if owner:
        rows.append([B("▦ بکاپ و ریستور", "adm:bk"), B("⟳ فاصله‌ی چک قیمت", "adm:poll")])
        rows.append([B("⌫ پاکسازی تاریخچه", "adm:purge"), B("↻ ریستارت بات", "adm:restart")])
    rows.append([B("‹ بازگشت", "menu:home")])
    await show(update, text, M(rows))


def _safety_copies() -> int:
    d = BASE_DIR / "backups"
    return len(list(d.glob("before_restore_*.db"))) if d.exists() else 0


async def view_backup(update):
    s = settings()
    if s.get("backup_on"):
        last = s.get("backup_last_ts") or "هنوز اجرا نشده"
        backup_status = f"روشن · هر {s.get('backup_every_days', 7)} روز · آخرین ارسال: {last}"
    else:
        backup_status = "خاموش"
    text = (
        "▦ <b>بکاپ و ریستور</b>\n\n"
        "<blockquote>"
        f"▤ حجم دیتابیس  ·  <b>{_db_size()}</b>\n"
        f"◷ نسخه‌های قبل از ریستور  ·  <b>{_safety_copies()}</b>\n"
        f"⟳ بکاپ خودکار  ·  <b>{backup_status}</b>"
        "</blockquote>\n\n"
        "⇩ <b>گرفتن بکاپ</b>: یک فایل <code>.db</code> از کانال‌ها، معاملات، تاریخچه و تنظیمات.\n"
        "⇧ <b>ریستور</b>: همان فایل را برگردان؛ قبلش از دیتای فعلی خودکار نسخه گرفته می‌شود.\n"
        "⟳ بکاپ خودکار برای مالکان، داخل گفت‌وگوی خصوصی بات ارسال می‌شود."
    )
    kb = M([
            [B("⇩ گرفتن بکاپ", "adm:backup"), B("⇧ ریستور بکاپ", "adm:restore")],
            [B(("🔕 خاموش کردن" if s.get("backup_on") else "🔔 روشن کردن"), "adm:autobackup"),
             B(f"⟳ فاصله: هر {s.get('backup_every_days', 7)} روز", "adm:backupdays")],
            [B("‹ پنل ادمین", "adm:home")]])
    await show(update, text, kb)


async def view_admins(update):
    uid = update.effective_user.id
    owner = is_owner(uid)
    owners = owner_ids()
    plain = [i for i in db()["admins"] if not is_owner(i)]
    me = "  ◂ شما"

    def row(i, last):
        return f"{'┗' if last else '┣'} {who(i)}" + (me if i == uid else "") + f"\n{'   ' if last else '┃ '} <code>{i}</code>"

    lines = ["👥 <b>تیم</b>", ""]
    lines.append(f"👑 <b>مالکان</b>  ·  {len(owners)}")
    lines.append("<blockquote>" + "\n".join(
        row(i, k == len(owners) - 1) + ("  🔒" if i in ADMIN_IDS else "")
        for k, i in enumerate(owners)) + "</blockquote>")
    lines.append(f"🛡 <b>ادمین‌ها</b>  ·  {len(plain)}")
    if plain:
        lines.append("<blockquote>" + "\n".join(
            row(i, k == len(plain) - 1) for k, i in enumerate(plain)) + "</blockquote>")
    else:
        lines.append("<blockquote>هنوز ادمینی اضافه نشده.</blockquote>")
    lines.append("<i>🔒 = مالک اصلی (در فایل .env)، از داخل بات قابل تغییر نیست.</i>")
    kb = []
    if owner:
        lines.append("<i>برای تغییر نقش یا حذف، روی اسم هر نفر بزن.</i>")
        people = [(i, "👑") for i in owners] + [(i, "🛡") for i in plain]
        btns = [B(f"{ic} {_plain_name(i)}", f"adm:u:{i}") for i, ic in people]
        kb += [btns[k:k + 2] for k in range(0, len(btns), 2)]
        kb.append([B("➕ افزودن ادمین", "adm:add")])
        on = uid in settings().get("see_all", [])
        kb.append([B(f"👁 دیدن و مدیریت معاملات همه:  {'روشن ✓' if on else 'خاموش ✗'}", "set:seeall:a")])
    kb.append([B("‹ بازگشت", "adm:home")])
    await show(update, "\n".join(lines), M(kb))


def _plain_name(i: int) -> str:
    r = settings().get("names", {}).get(str(i)) or {}
    return (r.get("n") or (f"@{r['u']}" if r.get("u") else "") or str(i))[:22]


async def view_admin_user(update, aid: int, notice: str = ""):
    uid = update.effective_user.id
    if not is_admin(aid):
        await view_admins(update)
        return
    fixed = aid in ADMIN_IDS
    owner = is_owner(aid)
    n_act = len([t for t in active_trades() if t.get("owner") == aid])
    text = (f"{'👑' if owner else '🛡'} <b>{who(aid)}</b>\n\n<blockquote>"
            f"# آیدی  ·  <code>{aid}</code>\n"
            f"◉ نقش  ·  <b>{'مالک' if owner else 'ادمین'}</b>{' (اصلی 🔒)' if fixed else ''}\n"
            f"📋 معاملات فعال  ·  <b>{n_act}</b></blockquote>")
    if notice:
        text = notice + "\n\n" + text
    kb = []
    if aid == uid:
        text += "\n\n<i>این خودِ شماست؛ نقش خودتان را از اینجا نمی‌توانید عوض کنید.</i>"
    elif fixed:
        text += "\n\n<i>مالک اصلی در فایل .env تعریف شده و از داخل بات قابل تغییر نیست.</i>"
    elif owner:
        kb.append([B("⬇️ تبدیل به ادمین (آنمالک)", f"adm:demo:{aid}")])
    else:
        kb.append([B("👑 ارتقا به مالک", f"adm:promo:{aid}")])
        kb.append([B("🗑 حذف از ادمین‌ها", f"adm:del:{aid}")])
    kb.append([B("‹ تیم", "adm:list")])
    await show(update, text, M(kb))


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


def _reschedule_backup(job_queue):
    for j in job_queue.get_jobs_by_name("autobackup"):
        j.schedule_removal()
    if settings().get("backup_on"):
        job_queue.run_repeating(auto_backup, interval=86400, first=20, name="autobackup")


async def auto_backup(context: ContextTypes.DEFAULT_TYPE):
    s = settings()
    if not s.get("backup_on"):
        return
    last = s.get("backup_last_ts")
    if last:
        try:
            elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last)
            if elapsed < timedelta(days=int(s.get("backup_every_days", 7))):
                return
        except (TypeError, ValueError):
            pass
    owners = owner_ids()
    tmp = None
    sent = 0
    try:
        tmp = make_backup()
        payload = tmp.read_bytes()
        name = f"magical_org_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db"
        for owner in owners:
            try:
                await context.bot.send_document(
                    chat_id=owner, document=payload, filename=name,
                    caption=BACKUP_CAPTION, parse_mode=ParseMode.HTML)
                sent += 1
            except Exception:
                log.exception("automatic backup failed for owner %s", owner)
        if sent == len(owners) and sent:
            s["backup_last_ts"] = datetime.now(timezone.utc).isoformat()
            save()
    except Exception:
        log.exception("automatic backup creation failed")
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


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


async def _post_photo(ch, png: bytes, caption: str, reply_to=None, markup=None):
    rp = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
    m = await BOT.send_photo(chat_id=ch["chat"], photo=png, caption=caption,
                             parse_mode=ParseMode.HTML, reply_parameters=rp, reply_markup=markup,
                             read_timeout=60, write_timeout=60, connect_timeout=20, pool_timeout=20)
    return m.message_id


def _final_ev(t) -> str:
    """Image category of a result post: TP, or stop (SL / break-even / manual close in loss)."""
    if t["outcome"] == "tp" or (t["outcome"] in ("manual", "sl") and (t.get("result_r") or 0) > 0):
        return "tp"
    return "sl"


def _badge(t, ev: str, step=None):
    o = t.get("outcome")
    if ev == "pending":
        _, label = trading._pending_kind(t)
        return (label.upper(), "#2962FF" if t.get("order") == "limit" else "#F7A600")
    if ev == "entry":
        return ("OPENED", chart.UP)
    if ev == "reward":
        return (f"REWARD {fmt_step(step)}", chart.UP)
    if ev == "cancel":
        return ("CANCELLED", "#787B86")
    if o == "tp":
        return (f"TAKE PROFIT {fmt_r(t['result_r'])}", chart.UP)
    if o == "be":
        return ("BREAK-EVEN 0R", "#787B86")
    if o == "manual":
        return (f"CLOSED {fmt_r(t['result_r'])}", chart.UP if t["result_r"] > 0 else chart.DOWN)
    if (t.get("result_r") or 0) > 0.005:  # stop moved into profit
        return (f"STOP IN PROFIT {fmt_r(t['result_r'])}", chart.UP)
    return (f"STOP LOSS {fmt_r(t['result_r'])}", chart.DOWN)


PENDING_AHEAD = 2  # pending charts: R/R box starts this many candles after the last candle


def _start_time(t, ev: str):
    raw = t.get("opened_at") or (None if ev in ("entry", "pending")
                                 else t.get("posted_at") or t.get("created_at"))
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


async def _send_pic(ch, t, cur):
    """Manual 'Send Pic': chart of the trade right now, as a reply to its post in the channel."""
    s = settings()
    if t["status"] == "open":
        risk = trading.risk_of(t)
        r = (1 if t["side"] == "LONG" else -1) * (cur - t["entry"]) / risk
        badge = (f"IN PROFIT {fmt_r(r)}", chart.UP) if r > 0.005 else (
            (f"IN LOSS {fmt_r(r)}", chart.DOWN) if r < -0.005 else ("AT ENTRY 0R", "#787B86"))
        caption = f"📸 Position {t['no']}  ·  <b>{fmt_r(r)}</b>  ·  Now {fmt_price(cur)}"
        ev, reply_to = "entry", t.get("open_msg_id") or t.get("pending_msg_id")
    else:
        badge = _badge(t, "pending")
        caption = f"📸 {esc(t['symbol'])} {t['side']}  ·  Pending  ·  Now {fmt_price(cur)}"
        ev, reply_to = "cancel", t.get("pending_msg_id")
    ahead = 0 if t["status"] == "open" else PENDING_AHEAD  # pending: box ahead of price
    png = await chart.snapshot(await trading._sess(), t, s.get("img_tf", "15m"), cur, BOT_NAME,
                               _start_time(t, ev), badge, s.get("img_labels", False), ahead)
    if not png:
        raise RuntimeError("کندل‌ها دریافت نشد")
    # glass button back to the position's first post (same as every other stage post)
    markup = None
    root = t.get("root_msg_id") or t.get("pending_msg_id") or t.get("open_msg_id")
    if root:
        try:
            base = await _chan_base(ch)
            markup = M([[InlineKeyboardButton("پست اصلی پوزیشن", url=f"{base}/{root}")]])
        except Exception:
            log.exception("send pic back button failed for trade %s", t.get("id"))
    return await _post_photo(ch, png, caption, reply_to, markup)


# ---------- glass link buttons between a position's channel posts ----------
# The first post of a position (pending, or entry if there was none) is the "root".
# Root gets a URL button to every later post (Entry, Rewards, BE, TP/SL, Cancel);
# every later post gets "↩ پست اصلی پوزیشن" pointing back to the root.

async def _chan_base(ch) -> str:
    if "link_base" not in ch:
        uname = None
        try:
            uname = (await BOT.get_chat(ch["chat"])).username
        except Exception:
            log.exception("get_chat failed for %s", ch.get("chat"))
        if uname:
            ch["link_base"] = f"https://t.me/{uname}"
        else:
            cid = str(ch["chat"])
            ch["link_base"] = "https://t.me/c/" + (cid[4:] if cid.startswith("-100") else cid.lstrip("-"))
        save()
    return ch["link_base"]


def _post_label(t, ev: str, step=None) -> str:
    if ev == "entry":
        return "ورود"
    if ev == "reward":
        return f"ریوارد {fmt_step(step)}"
    if ev == "be_set":
        return "SL به Entry"
    if ev == "cancel":
        return "کنسل"
    if ev == "edit":
        return "ویرایش"
    o = t.get("outcome")
    if o == "tp":
        return "TP"
    if o == "be":
        return "بریک‌اون"
    if o == "sl":
        return "استاپ"
    if o == "manual":
        return f"بسته شد {fmt_r(t.get('result_r') or 0)}"
    return "نتیجه"


async def _link_post(ch, t, ev: str, mid, step=None):
    """Record a channel post of trade t and refresh the link buttons. Never raises."""
    if not mid:
        return
    try:
        posts = t.setdefault("posts", [])
        if ev in ("pending",) or (ev == "entry" and not t.get("pending_msg_id")):
            t["root_msg_id"] = mid
            save()
            return
        root = t.get("root_msg_id") or t.get("pending_msg_id") or t.get("open_msg_id")
        if not root or root == mid:
            return
        posts.append({"id": mid, "label": _post_label(t, ev, step)})
        save()
        base = await _chan_base(ch)
        back = M([[InlineKeyboardButton("پست اصلی پوزیشن", url=f"{base}/{root}")]])
        try:
            await BOT.edit_message_reply_markup(chat_id=ch["chat"], message_id=mid, reply_markup=back)
        except BadRequest as ex:
            log.warning("back button failed (trade %s): %s", t.get("id"), ex)
        btns = [InlineKeyboardButton(x["label"], url=f"{base}/{x['id']}") for x in posts]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        try:
            await BOT.edit_message_reply_markup(chat_id=ch["chat"], message_id=root,
                                                reply_markup=M(rows))
        except BadRequest as ex:
            if "not modified" not in str(ex).lower():
                log.warning("root buttons failed (trade %s): %s", t.get("id"), ex)
    except Exception:
        log.exception("link buttons failed for trade %s", t.get("id"))


async def _post_event(ch, t, ev: str, text: str, reply_to=None, price=None, step=None):
    mid = await _post_event_raw(ch, t, ev, text, reply_to, price, step)
    await _link_post(ch, t, ev, mid, step)
    return mid


async def _post_event_raw(ch, t, ev: str, text: str, reply_to=None, price=None, step=None):
    """Channel post for a trade stage. If that stage is ticked in ▣ عکس پست‌ها, it is sent
    as a chart photo with the text as caption. Text fallback happens ONLY if the photo was
    certainly not delivered (chart error / Telegram rejected it). On a timeout / network error
    the photo may already be in the channel, so we never send the text again (no duplicates)."""
    s = settings()
    if s["img_events"].get(ev):
        png = None
        try:
            png = await chart.snapshot(await trading._sess(), t, s.get("img_tf", "15m"), price,
                                       BOT_NAME, _start_time(t, ev), _badge(t, ev, step),
                                       s.get("img_labels", False),
                                       PENDING_AHEAD if ev == "pending" else 0)
        except Exception:
            log.exception("%s chart failed for trade %s, sending text", ev, t.get("id"))
        if png:
            try:
                return await _post_photo(ch, png, text, reply_to)
            except (TimedOut, NetworkError):
                log.exception("%s photo send uncertain for trade %s (maybe delivered); "
                              "NOT sending text to avoid a duplicate", ev, t.get("id"))
                return None
            except Exception:
                log.exception("%s photo rejected for trade %s, sending text", ev, t.get("id"))
    return await _post(ch, text, reply_to)


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
    remember_user(u)
    await reset_flow(context.user_data)
    # Always (re)send the ◷ پندینگ / ● پوزیشن / 🏠 منو keyboard: Telegram drops it when the chat is
    # cleared or on another device, and only a message can bring it back.
    await say(update, "👇 پندینگ · پوزیشن · منو", main_kb())
    await view_home(update)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or not is_admin(u.id):
        await _deny(update)
        return
    await say(update, HELP, home_kb(u.id))


# ====================== Edit pending / open position (draft from the name button) ======================
# Tapping «#12 BTC ↑» in the trade list puts a ready draft in the message box:
#     @bot #12 BTC Int81000 Tp87000 Sl80000      (open position: only Tp / Sl)
# The admin changes the numbers and sends it; the bot finds what changed by itself.
# waiting  → edited silently (not in the channel yet)
    # pending  → Entry / TP / SL rewrite the original post + a reply lists the changes
# open     → a reply says whether the stop got smaller / bigger (or risk-free / in profit) and
#            whether the target was extended / reduced. 1R stays the original risk.

EDIT_LABEL = {"entry": "Entry", "tp": "TP", "sl": "SL", "cancel": "Cancel", "riskfree": "RiskFree"}
ACTIVE = ("waiting", "pending", "open")
NOTIFY_FIELDS = ("entry", "tp", "sl")  # only these edits touch or announce channel posts


def edit_draft(t) -> str:
    """Same layout as a new signal (open position: no Int line, Entry is locked):
    BTC
    Int84,120.64
    Tp76,765.22
    Sl86,838.95"""
    lines = [t["symbol"]]
    if t["status"] != "open":
        lines.append(f"Int{fmt_price(t['entry'])}")
    lines += [f"Tp{fmt_price(t['tp'])}", f"Sl{fmt_price(t['sl'])}"]
    if t["status"] != "open":
        lines.append(f"Cancel{fmt_price(t['cancel'])}" if t.get("cancel") is not None else "Cancel")
    if not t.get("be_active"):  # once risk-free, the level has done its job
        lines.append(f"RiskFree{fmt_price(t['riskfree'])}" if t.get("riskfree") is not None else "RiskFree")
    return "\n".join(lines)


def edit_button(t, label: str, kind: str):
    """Name button of a trade: sends its template with a 📋 copy button."""
    return B(label, f"ed:open:{t['id']}:{kind}")


_MENTION = re.compile(r"^\s*@(\w+)\s*")
_EDIT_VAL = re.compile(r"(?<![A-Za-z])(risk\s*-?\s*free|rf|int|entry|ent|en|tp|sl|stop|cancel|cnl|cn)"
                       r"\s*[:=]?\s*(\d[\d.,'’_]*)", re.I)
_EDIT_KEY = {"int": "entry", "entry": "entry", "ent": "entry", "en": "entry",
             "tp": "tp", "sl": "sl", "stop": "sl", "cancel": "cancel", "cnl": "cancel", "cn": "cancel",
             "riskfree": "riskfree", "rf": "riskfree"}
PIN_SECONDS = 600  # fallback mode: how long a tapped trade waits for its edited draft


def parse_edit(text: str):
    """(symbol, {field: value}) from «[@bot] BTC Int… Tp… Sl…» (lines or one line), or None."""
    text = _MENTION.sub("", text.translate(trading._DIGITS).translate(trading._JUNK), count=1)
    parts = text.split(None, 1)
    if not parts or _EDIT_VAL.match(parts[0]):
        return None
    vals = {}
    for k, v in _EDIT_VAL.findall(parts[1] if len(parts) > 1 else ""):
        try:
            vals[_EDIT_KEY[re.sub(r"[\s-]", "", k.lower())]] = trading._num(v)
        except ValueError:
            continue
    return (parts[0], vals) if vals else None


def _is_draft(text: str) -> bool:
    """Text put in the input box by a name button starts with «@<this bot>»."""
    m = _MENTION.match(text)
    if not m:
        return False
    me = (getattr(BOT, "username", None) or "").lower() if BOT else ""
    return not me or m.group(1).lower() == me


def _edit_candidates(symbol: str, uid=None) -> list:
    pair, base = trading.normalize_pair(symbol)
    return [t for t in active_trades(uid=uid) if t["pair"] == pair or t["symbol"].upper() == (base or "")]


def _best_match(cands, vals):
    """Several active trades of one symbol: the one whose unchanged values match best."""
    def score(t):
        return sum(1 for k, v in vals.items() if abs(v - (t.get(k) or 0)) < 1e-9)
    ranked = sorted(cands, key=score, reverse=True)
    if len(ranked) == 1 or score(ranked[0]) > score(ranked[1]):
        return ranked[0]
    return None


def _edit_values(t, new) -> dict:
    return {k: float(new.get(k, t[k])) for k in ("entry", "tp", "sl")}


def _edit_rr(t, v) -> float:
    risk = trading.risk_of(t) if t["status"] == "open" else (abs(v["entry"] - v["sl"]) or 1e-12)
    return round(abs(v["tp"] - v["entry"]) / risk, 2)


def _edit_check(t, new, cur=None):
    """Error text, or None if the new values are valid for this trade."""
    err = _edit_check_levels(t, new, cur)
    if err:
        return err
    v = _edit_values(t, new)
    rf = new["riskfree"] if "riskfree" in new else t.get("riskfree")
    if rf and not t.get("be_active") and ("riskfree" in new or "tp" in new or "entry" in new):
        if err := trading.riskfree_check(t["side"], v["entry"], v["tp"], rf):
            return err
        if "riskfree" in new and t["status"] == "open" and cur is not None and \
                trading.riskfree_reached({**t, "riskfree": rf}, cur):
            return (f"قیمت فعلی ({fmt_price(cur)}) از این RiskFree رد شده؛ "
                    f"برای ریسک‌فری کردن فوری دکمه‌ی Break-even را بزن.")
    return None


def _edit_check_levels(t, new, cur=None):
    v = _edit_values(t, new)
    en, tp, sl = v["entry"], v["tp"], v["sl"]
    long_ = t["side"] == "LONG"
    if t["status"] == "open":
        if "entry" in new:
            return "Entry پوزیشن باز قابل ویرایش نیست؛ فقط TP و SL."
        if long_ and not tp > en:
            return "در LONG باید TP بالاتر از Entry باشد."
        if not long_ and not tp < en:
            return "در SHORT باید TP پایین‌تر از Entry باشد."
        if cur is not None:
            now = fmt_price(cur)
            if long_ and not sl < cur:
                return f"در LONG باید SL پایین‌تر از قیمت فعلی ({now}) باشد."
            if not long_ and not sl > cur:
                return f"در SHORT باید SL بالاتر از قیمت فعلی ({now}) باشد."
            if long_ and not tp > cur:
                return f"در LONG باید TP بالاتر از قیمت فعلی ({now}) باشد."
            if not long_ and not tp < cur:
                return f"در SHORT باید TP پایین‌تر از قیمت فعلی ({now}) باشد."
        return None
    if long_ and not tp > en > sl:
        return "ترتیب LONG باید SL < Entry < TP باشد."
    if not long_ and not tp < en < sl:
        return "ترتیب SHORT باید TP < Entry < SL باشد."
    if "entry" in new or "cancel" in new:
        c = new["cancel"] if "cancel" in new else t.get("cancel")
        if c:  # 0 / None = no Cancel level
            if cur is None:
                return "قیمت لحظه‌ای پیدا نشد؛ Cancel قابل بررسی نیست. دوباره امتحان کن."
            return trading.cancel_check(en, c, cur)
    return None


async def _root_kb(ch, t):
    """The link buttons of the root post (so editing its text does not drop them)."""
    posts = t.get("posts") or []
    if not posts:
        return None
    base = await _chan_base(ch)
    btns = [InlineKeyboardButton(x["label"], url=f"{base}/{x['id']}") for x in posts]
    return M([btns[i:i + 3] for i in range(0, len(btns), 3)])


async def _edit_root_post(ch, t, cur) -> bool:
    """Rewrite the pending post with the corrected values (text, or chart + caption)."""
    mid = t.get("pending_msg_id")
    if not mid:
        return False
    when = None
    try:
        raw = t.get("posted_at") or t.get("created_at")
        when = datetime.fromisoformat(raw).astimezone(TZ) if raw else None
    except ValueError:
        pass
    text = trading.pending_text(t, when)
    kb = await _root_kb(ch, t)
    try:
        await BOT.edit_message_text(chat_id=ch["chat"], message_id=mid, text=text,
                                    parse_mode=ParseMode.HTML, reply_markup=kb,
                                    disable_web_page_preview=True)
        return True
    except BadRequest as ex:
        m = str(ex).lower()
        if "not modified" in m:
            return True
        if "no text" not in m:
            log.warning("edit pending post failed (trade %s): %s", t.get("id"), ex)
            return False
    # photo post: redraw the chart with the new levels, else just fix the caption
    try:
        s = settings()
        png = await chart.snapshot(await trading._sess(), t, s.get("img_tf", "15m"), cur, BOT_NAME,
                                   None, _badge(t, "pending"), s.get("img_labels", False),
                                   PENDING_AHEAD)
        if png:
            await BOT.edit_message_media(
                chat_id=ch["chat"], message_id=mid, reply_markup=kb,
                media=InputMediaPhoto(png, caption=text, parse_mode=ParseMode.HTML))
            return True
    except Exception:
        log.exception("edit pending chart failed for trade %s", t.get("id"))
    try:
        await BOT.edit_message_caption(chat_id=ch["chat"], message_id=mid, caption=text,
                                       parse_mode=ParseMode.HTML, reply_markup=kb)
        return True
    except BadRequest as ex:
        return "not modified" in str(ex).lower()


async def _apply_edit(ch, t, new, cur) -> str:
    """Write the new values, then tell the channel. Returns a notice for the admin."""
    was = t["status"]
    if "cancel" in new and not new["cancel"]:
        new = {**new, "cancel": None}  # Cancel0 = remove the Cancel level
    if "riskfree" in new and not new["riskfree"]:
        new = {**new, "riskfree": None}  # RiskFree0 = remove the RiskFree level
    changes = [[f, t.get(f), new[f]] for f in ("entry", "tp", "sl", "cancel", "riskfree") if f in new]
    old_rr = t["rr"]
    old_stop = None
    if was == "open":
        if not t.get("risk0"):
            t["risk0"] = abs(t["entry"] - t["sl"])  # positions opened before this update
        old_stop = trading.stop_r(t)
        if t.get("be_active") and old_stop < 0:  # BE had moved the stop to Entry
            old_stop = 0.0
            for c in changes:
                if c[0] == "sl":
                    c[1] = t["entry"]
        for f, _, n in changes:
            t[f] = n
        if "sl" in new:
            t["be_active"] = trading.stop_r(t) >= -0.005
    else:
        for f, _, n in changes:
            t[f] = n
        if "entry" in new and cur is not None:
            t["order"] = trading.order_type(t["side"], t["entry"], cur)
    t["rr"] = _edit_rr(t, _edit_values(t, {}))
    t["steps"] = reward_steps(settings()["reward_every"], t["rr"])
    t.setdefault("edits", []).append({"at": datetime.now(timezone.utc).isoformat(),
                                      "status": was, "changes": changes})
    save()
    what = " · ".join(f"{EDIT_LABEL[f]} {trading._fp(o)} → {trading._fp(n)}" for f, o, n in changes)
    if was == "waiting":
        return f"✓ #{t['id']} ویرایش شد ({what}). هنوز در کانال پست نشده بود."
    # Only Entry / TP / SL changes are announced in the channel.
    # Cancel / RiskFree edits are saved silently.
    notify = [c for c in changes if c[0] in NOTIFY_FIELDS]
    try:
        extra = ""
        if was == "pending":
            if notify:
                fixed = await _edit_root_post(ch, t, cur)
                mid = await _post(ch, trading.edit_pending_text(t, notify, old_rr), t.get("pending_msg_id"))
                await _link_post(ch, t, "edit", mid)
                if not fixed:
                    extra = "\n⚠️ پست اصلی قابل ویرایش نبود؛ فقط ریپلای اصلاحیه رفت."
        elif notify:
            mid = await _post(ch, trading.edit_open_text(t, notify, old_rr, old_stop),
                              t.get("open_msg_id") or t.get("pending_msg_id"))
            await _link_post(ch, t, "edit", mid)
        if not notify:
            return f"✓ #{t['id']} ویرایش شد ({what}). در کانال اعلام نشد (فقط Entry / TP / SL اعلام می‌شوند)."
    except Exception as ex:
        log.exception("edit post failed for trade %s", t.get("id"))
        return f"⚠️ #{t['id']} ذخیره شد ({what}) ولی اعلام در کانال ناموفق بود: <code>{esc(str(ex))}</code>"
    return f"✓ #{t['id']} ویرایش و در «{esc(ch['title'])}» اعلام شد ({what}).{extra}"



EDIT_CANCEL = "✖️ لغو ویرایش"


def edit_kb() -> ReplyKeyboardMarkup:
    """While an edit waits for its numbers, the main keyboard is replaced by ✖️ cancel only."""
    return ReplyKeyboardMarkup([[EDIT_CANCEL]],
                               resize_keyboard=True, is_persistent=True)


async def end_edit(update, ud, text: str):
    """Leave edit mode and put the normal keyboard back."""
    ud.pop("edit_pin", None)
    ud.pop("edit_vals", None)
    await say(update, text, main_kb())


RETRY = "\nدرستش کن و دوباره بفرست، یا «✖️ لغو ویرایش» را بزن."


async def try_edit(update, context, text: str) -> bool:
    """True if the message was an edited template (and was handled)."""
    ud = context.user_data
    draft = _is_draft(text)
    pin = ud.get("edit_pin")
    if pin and time.time() - pin[1] > PIN_SECONDS:
        ud.pop("edit_pin", None)
        pin = None
    if not draft and not pin:
        return False
    parsed = parse_edit(text)
    if not parsed:
        await say(update, "❌ قالب را نشناختم. نمونه:\n<pre>BTC\nInt84,120.64\n"
                          "Tp76,765.22\nSl86,838.95</pre>" + RETRY)
        return True
    symbol, vals = parsed
    cands = _edit_candidates(symbol, _uid(update))
    if pin:
        pt = get_trade(pin[0])
        if pt in cands:
            cands = [pt]
        elif not draft:
            return False  # a new signal for another symbol: not an edit
    await reset_flow(ud)
    if not cands:
        await say(update, f"ℹ️ معامله‌ی فعالی برای <b>{esc(symbol.upper())}</b> پیدا نشد." + RETRY)
        return True
    t = _best_match(cands, vals)
    if t is None:  # same symbol twice and nothing tells them apart: ask
        ud["edit_vals"] = vals
        kb = [[B(f"#{c['id']} {c['symbol']} {c['side']} · {c['status'].capitalize()}", f"ed:pick:{c['id']}")]
              for c in cands]
        await say(update, "کدام معامله را ویرایش کنم؟", M(kb))
        return True
    await handle_edit(update, context, t["id"], vals)
    return True


async def handle_edit(update, context, tid: int, vals: dict):
    """An edited template came back: work out what changed and apply it right away.
    On a mistake the edit stays open (fix and resend); on success the normal keyboard returns."""
    ud = context.user_data
    t = get_trade(tid)
    if not t or t["status"] not in ACTIVE or not mine(t, _uid(update)):
        await end_edit(update, ud, f"ℹ️ معامله‌ی #{tid} دیگر فعال نیست.")
        return
    kind = "o" if t["status"] == "open" else "p"
    async with LOCK:
        ch = get_channel(t["channel"])
        if not ch or t["status"] not in ACTIVE:
            await end_edit(update, ud, f"ℹ️ معامله‌ی #{tid} دیگر فعال نیست.")
            return
        if t["status"] == "open":  # position: only Tp / Sl / RiskFree, Int / Cancel lines are ignored
            vals = {k: v for k, v in vals.items() if k not in ("entry", "cancel")}
        if t.get("be_active"):  # already risk-free: the RiskFree line has nothing left to do
            vals.pop("riskfree", None)
        new = {k: v for k, v in vals.items() if abs(v - (t.get(k) or 0)) > 1e-12}
        if not new:
            await say(update, "ℹ️ هیچ عددی عوض نشده بود." + RETRY)
            return
        cur = await trading.get_price(t["pair"])
        err = _edit_check(t, new, cur)
        if err:
            await say(update, f"❌ {esc(err)}" + RETRY)
            return
        notice = await _apply_edit(ch, t, new, cur)
    await end_edit(update, ud, notice)
    await view_trades(update, 0, kind)


async def on_edit_cb(update, context, p):
    """Name button: the trade's template with a one-tap copy button.
    Also «ed:pick:<id>» when an edited draft matched two trades of the same symbol."""
    ud = context.user_data
    if len(p) > 2 and p[1] == "pick":
        vals = ud.pop("edit_vals", None)
        if not vals:
            await view_trades(update, 0, "a", "⌛️ منقضی شد؛ دوباره از روی نام معامله ویرایش کن.")
            return
        await handle_edit(update, context, int(p[2]), vals)
        return
    t = get_trade(int(p[2])) if len(p) > 2 and p[2].isdigit() else None
    kind = p[3] if len(p) > 3 else "a"
    if not t or t["status"] not in ACTIVE or not mine(t, _uid(update)):
        await view_trades(update, 0, kind, "ℹ️ این معامله دیگر فعال نیست.")
        return
    d = edit_draft(t)
    ud["edit_pin"] = (t["id"], time.time())  # the next message with this symbol edits this trade
    only = ("  ·  فقط Tp و Sl" + ("" if t.get("be_active") else " و RiskFree")) if t["status"] == "open" else ""
    text = f"✏️ <b>#{t['id']} {esc(t['symbol'])}</b>{only}"
    if CopyTextButton:
        kb = M([[InlineKeyboardButton("📋 کپی قالب", copy_text=CopyTextButton(text=d))]])
    else:  # very old python-telegram-bot: show the template to copy by hand
        kb = None
        text += f"\n<pre>{esc(d)}</pre>"
    await say(update, text, kb)
    await say(update, "کپی کن، عدد را عوض کن و بفرست.", edit_kb())  # ✖️ cancel in the main keyboard


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
        if is_admin(uid):
            await say(update, "ℹ️ این کاربر از قبل ادمین است.", cancel_kb())
            return
        db()["admins"].append(uid)
        save()
        ud.pop("state", None)
        await say(update, f"✅ کاربر <code>{uid}</code> به ادمین‌ها اضافه شد.",
                  M([[B("👑 ارتقا به مالک", f"adm:promo:{uid}")], [B("⬅️ بازگشت", "adm:list")]]))
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
    elif st == "set_near":
        try:
            v = float(text.replace("%", "").strip())
            if not 1 <= v <= 100:
                raise ValueError
        except ValueError:
            await say(update, "❌ یک عدد بین 1 تا 100 بفرست (مثلاً <code>30</code>).",
                      M([[B("✕ انصراف", "set:pend")]]))
            return
        settings()["near_pct"] = v
        save()
        ud.pop("state")
        await say(update, f"✓ ارسال نزدیک ورود: <b>{pct(v)}%</b> فاصله‌ی SL تا Entry",
                  M([[B("‹ تنظیمات پندینگ", "set:pend")]]))
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
            await say(update, "❌ عددی بین 2 تا 60 بفرست.", M([[B("✕ انصراف", "adm:home")]]))
            return
        settings()["poll_seconds"] = int(text)
        save()
        ud.pop("state")
        _reschedule(context.job_queue)
        await say(update, f"✓ چک قیمت روی <b>هر {text} ثانیه</b> تنظیم شد.",
                  M([[B("‹ پنل ادمین", "adm:home")]]))
    elif st == "set_backup_days":
        raw = text.translate(trading._DIGITS).strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 90:
            await say(update, "❌ عددی بین 1 تا 90 بفرست.", M([[B("✕ انصراف", "adm:bk")]]))
            return
        days = int(raw)
        settings()["backup_every_days"] = days
        save()
        ud.pop("state", None)
        _reschedule_backup(context.job_queue)
        await say(update, f"✅ بکاپ خودکار روی <b>هر {days} روز</b> تنظیم شد.",
                  M([[B("‹ بکاپ و ریستور", "adm:bk")]]))


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
    if err := trading.cancel_check(parsed["entry"], parsed.get("cancel"), price):
        await say(update, f"❌ {esc(err)}", back_kb())
        return
    parsed["price"] = price
    parsed["order"] = trading.order_type(parsed["side"], parsed["entry"], price)
    context.user_data["draft"] = parsed
    await say(update, *_preview(parsed))


def _preview(parsed):
    """Signal preview text + keyboard. With ⌖ near-entry on, a per-signal ⚡ toggle lets
    this one signal skip the wait and post immediately."""
    price = parsed["price"]
    btns = [B(f"📤 {c['title']}", f"sig:post:{c['key']}") for c in channels()]
    kb = [btns[i:i + 2] for i in range(0, len(btns), 2)]  # two columns
    note = ""
    if settings().get("near_on"):
        band = near_band(parsed)
        if is_near(parsed, price):
            note = "\n⌖ قیمت همین الان نزدیک Entry است؛ فوری پست می‌شود."
        else:
            now = parsed.get("send_now", False)
            if now:
                note = "\n⚡ <b>ارسال فوری</b>: این سیگنال همین الان پست می‌شود (بدون انتظار)."
            else:
                note = (f"\n⌖ <b>ارسال نزدیک ورود</b>: وقتی قیمت به "
                        f"<b>{fmt_price(near_trigger(parsed))}</b> برسد پست می‌شود "
                        f"({pct(settings()['near_pct'])}% فاصله‌ی SL تا Entry · "
                        f"{fmt_price(band)} تا Entry).")
            kb.append([B(f"{ON if now else OFF} ⚡ ارسال فوری همین پست", "sig:now")])
    kb.append([B("✖️ لغو", "sig:cancel")])
    text = (f"👀 <b>پیش‌نمایش</b>\n\n{trading.pending_text(parsed)}\n\n"
            f"💵 قیمت لحظه‌ای {esc(parsed['symbol'])}: <b>{fmt_price(price)}</b>{note}")
    return text, M(kb)


@admin_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == MENU_LABEL:  # Telegram-keyboard button: cancel pending input, open the main menu
        await reset_flow(context.user_data)
        await view_home(update)
        return
    if text == EDIT_CANCEL:  # ✖️ in the main keyboard while an edit is open
        await reset_flow(context.user_data)
        await end_edit(update, context.user_data,
                       "✖️ ویرایش لغو شد." if context.user_data.get("edit_pin") else "ℹ️ ویرایشی در جریان نبود.")
        return
    if text in (PEND_LABEL, POS_LABEL):  # persistent-keyboard shortcuts
        await reset_flow(context.user_data)
        await view_trades(update, 0, "p" if text == PEND_LABEL else "o")
        return
    if await try_edit(update, context, text):  # edited draft from a trade's name button
        return
    st = context.user_data.get("state")
    if st:
        await handle_state(update, context, st, text)
    else:
        await handle_signal(update, context, text)


RESTORE_HOWTO = (
    "⇧ <b>نحوه‌ی ریستور</b>\n"
    "۱) /start ← 🛡 پنل ادمین ← ▦ بکاپ و ریستور ← ⇧ ریستور بکاپ\n"
    "۲) همین فایل را بدون تغییر به‌صورت <b>فایل</b> برای بات بفرست\n"
    "۳) خلاصه را چک کن و «⇧ بله، ریستور کن» را بزن\n"
    "<i>از دیتای فعلی خودکار یک نسخه گرفته می‌شود. فقط مالک می‌تواند ریستور کند.</i>"
)
BACKUP_CAPTION = ("💾 <b>بکاپ دیتابیس</b> (SQLite)\n"
                  "کانال‌ها، معاملات، تاریخچه و تنظیمات\n\n" + RESTORE_HOWTO)

MAX_BACKUP_MB = 20  # Telegram bots can download files up to 20 MB


@admin_only
async def on_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    if ud.get("state") != "restore_wait" or not is_owner(update.effective_user.id):
        await say(update, "📎 برای ریستور، از 🛡 پنل ادمین ← ▦ بکاپ و ریستور ← ⇧ ریستور بکاپ شروع کن.", home_kb(update.effective_user.id))
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
    await say(update, "⇧ <b>بکاپ معتبر است</b>\n\n<blockquote>"
                      f"📄 {esc(doc.file_name or 'backup.db')}\n"
                      f"📺 کانال‌ها: <b>{info['channels']}</b>\n"
                      f"📋 معاملات: <b>{info['trades']}</b> (فعال: {info['active']})\n"
                      f"🛡 ادمین‌ها: <b>{info['admins']}</b></blockquote>\n"
                      "⚠️ دیتای فعلی با این بکاپ <b>جایگزین</b> می‌شود (یک نسخه از دیتای فعلی نگه داشته می‌شود). ادامه؟",
              confirm_kb("adm:restoreok", "adm:bk", "⇧ بله، ریستور کن"))


# ====================== Callbacks ======================

@admin_only
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    p = q.data.split(":")
    a = p[0]
    ud = context.user_data
    uid = update.effective_user.id

    if a == "adm" and len(p) > 1 and not is_owner(uid):
        await q.answer("⛔ این بخش فقط برای مالک است.", show_alert=True)
        return
    await q.answer()

    if a == "noop":
        return

    if a == "menu":
        await reset_flow(ud)
        views = {"home": view_home, "channels": view_channels,
                 "trades": view_trades, "settings": view_settings,
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
        elif sub == "bk":
            await reset_flow(ud)
            await view_backup(update)
        elif sub == "autobackup":
            settings()["backup_on"] = not settings().get("backup_on", False)
            save()
            _reschedule_backup(context.job_queue)
            await view_backup(update)
        elif sub == "backupdays":
            ud["state"] = "set_backup_days"
            await show(update, "⟳ <b>فاصله‌ی بکاپ خودکار</b>\n\n"
                               "هر چند روز یک فایل بکاپ SQLite برای مالکان در گفت‌وگوی خصوصی بات ارسال شود؟\n"
                               "یک عدد بین <b>1</b> تا <b>90</b> بفرست.",
                       M([[B("✕ انصراف", "adm:bk")]]))
        elif sub == "poll":
            ud["state"] = "set_poll"
            await show(update, "⟳ <b>فاصله‌ی چک قیمت</b>\n\n"
                               "بات هر چند ثانیه یک‌بار قیمت معاملات فعال را از Binance/Bybit می‌گیرد "
                               "تا ورود، ریوارد، TP و SL را تشخیص دهد.\n\n"
                               "<blockquote>کمتر = دقیق‌تر (ویک‌های کوتاه هم دیده می‌شوند) ولی درخواست بیشتر\n"
                               "بیشتر = سبک‌تر ولی ممکن است ویک کوتاه از دست برود</blockquote>\n\n"
                               f"الان: <b>هر {settings()['poll_seconds']} ثانیه</b>\n"
                               "یک عدد بین <b>2</b> تا <b>60</b> بفرست (پیشنهاد: 5).",
                       M([[B("✕ انصراف", "adm:home")]]))
        elif sub == "add":
            ud["state"] = "adm_add"
            await show(update, "➕ <b>افزودن ادمین</b>\n\nآیدی عددی تلگرام کاربر را بفرست.\n"
                               "(کاربر می‌تواند با /start آیدی خودش را ببیند.)", cancel_kb())
        elif sub == "u":
            await view_admin_user(update, int(p[2]))
        elif sub == "promo":
            await show(update, f"👑 <b>{who(int(p[2]))}</b> مالک شود؟\n\n"
                               "مالک می‌تواند ادمین اضافه/حذف کند، بکاپ بگیرد، تاریخچه را پاک کند و بات را ریستارت کند.",
                       confirm_kb(f"adm:promok:{p[2]}", f"adm:u:{p[2]}", "👑 بله، مالک شود"))
        elif sub == "promok":
            aid = int(p[2])
            ex = settings().setdefault("owners_extra", [])
            if aid in db()["admins"] and aid not in ex and aid not in ADMIN_IDS:
                ex.append(aid)
                save()
            await view_admin_user(update, aid, "✅ به مالک ارتقا یافت.")
        elif sub == "demo":
            await show(update, f"⬇️ <b>{who(int(p[2]))}</b> دوباره ادمین معمولی شود؟\n\n"
                               "دسترسی مالک و «دیدن معاملات همه» از او گرفته می‌شود.",
                       confirm_kb(f"adm:demok:{p[2]}", f"adm:u:{p[2]}", "⬇️ بله، آنمالک شود"))
        elif sub == "demok":
            aid = int(p[2])
            if aid not in ADMIN_IDS and aid != uid:
                st_ = settings()
                st_["owners_extra"] = [i for i in st_.get("owners_extra", []) if i != aid]
                st_["see_all"] = [i for i in st_.get("see_all", []) if i != aid]
                save()
            await view_admin_user(update, aid, "✅ به ادمین معمولی تبدیل شد.")
        elif sub == "del":
            await show(update, f"🗑 <b>{who(int(p[2]))}</b> از ادمین‌ها حذف شود؟",
                       confirm_kb(f"adm:delok:{p[2]}", f"adm:u:{p[2]}"))
        elif sub == "delok":
            aid = int(p[2])
            if aid in db()["admins"] and not is_owner(aid):
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
            await show(update, "⇧ <b>ریستور بکاپ</b>\n\n"
                               "فایل بکاپ (<code>.db</code>) را همین‌جا به‌صورت <b>فایل</b> بفرست.\n"
                               "قبل از جایگزینی، از دیتای فعلی خودکار بکاپ گرفته می‌شود.",
                       M([[B("✕ انصراف", "adm:bk")]]))
        elif sub == "restoreok":
            f = ud.pop("restore_file", None)
            ud.pop("state", None)
            if not f or not Path(f).exists():
                await show(update, "❌ فایل بکاپ پیدا نشد، دوباره بفرست.", M([[B("‹ بکاپ و ریستور", "adm:bk")]]))
                return
            try:
                async with LOCK:
                    before = restore_backup(Path(f))
                _reschedule(context.job_queue)
                _reschedule_auto(context.job_queue)
                _reschedule_backup(context.job_queue)
            except Exception as ex:
                log.exception("restore failed")
                Path(f).unlink(missing_ok=True)
                await show(update, f"❌ ریستور ناموفق: <code>{esc(str(ex))}</code>",
                           M([[B("‹ بکاپ و ریستور", "adm:bk")]]))
                return
            pend, opn, _ = _counts()
            await show(update, "✅ <b>ریستور انجام شد</b>\n\n<blockquote>"
                               f"📺 کانال‌ها: <b>{len(channels())}</b>\n"
                               f"📋 معاملات: <b>{len(trades())}</b> (فعال: {pend + opn})\n"
                               f"🛡 ادمین‌ها: <b>{len(db()['admins'])}</b></blockquote>\n"
                               "قیمت‌ها از همین الان دوباره چک می‌شوند.", M([[B("‹ بکاپ و ریستور", "adm:bk")]]))
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
        if p[1] == "now":
            d_ = ud.get("draft")
            if not d_:
                await show(update, "⌛️ پیش‌نویس منقضی شده؛ سیگنال را دوباره بفرست.", back_kb())
                return
            d_["send_now"] = not d_.get("send_now", False)
            await show(update, *_preview(d_))
            return
        draft = ud.pop("draft", None)
        ch = get_channel(p[2])
        if not draft or not ch:
            await show(update, "⌛️ پیش‌نویس منقضی شده؛ سیگنال را دوباره بفرست.", back_kb())
            return
        async with LOCK:
            t = create_trade(draft, ch, uid)
            if (settings().get("near_on") and not draft.get("send_now")
                    and not is_near(t, draft["price"])):
                t["status"] = "waiting"
                save()
                await show(update, f"⌖ معامله‌ی <b>#{t['id']}</b> منتظر است؛ وقتی قیمت به "
                                   f"<b>{fmt_price(near_trigger(t))}</b> رسید در «{esc(ch['title'])}» پست می‌شود.",
                           M([[B("◷ پندینگ‌ها", "trp:0:p"), B("⬅️ بازگشت", "menu:home")]]))
                return
            try:
                t["pending_msg_id"] = await _post_event(ch, t, "pending", trading.pending_text(t),
                                                        None, t.get("last_price"))
                save()
            except Exception as ex:
                remove_trade(t["id"])
                await show(update, f"❌ ارسال ناموفق: <code>{esc(str(ex))}</code>", back_kb())
                return
        await show(update, f"✅ معامله‌ی <b>#{t['id']}</b> در «{esc(ch['title'])}» پست شد و زیر نظر است.",
                   M([[B("◷ پندینگ‌ها", "trp:0:p"), B("⬅️ بازگشت", "menu:home")]]))

    # ---------- edit pending / open position ----------
    elif a == "ed":
        await on_edit_cb(update, context, p)

    # ---------- trades ----------
    elif a == "tr":
        tid = int(p[2])
        kind = p[3] if len(p) > 3 else "a"
        t = get_trade(tid)
        async with LOCK:
            ch = t and get_channel(t["channel"])
            if not t or not ch or not mine(t, uid):
                await view_trades(update, 0, kind)
                return
            if p[1] == "pic" and t["status"] == "waiting":
                await view_trades(update, 0, kind, "ℹ️ این سیگنال هنوز در کانال پست نشده است.")
                return
            if p[1] == "pic":
                cur = await trading.get_price(t["pair"])
                if cur is None:
                    await view_trades(update, 0, kind, "❌ قیمت در دسترس نیست.")
                    return
                try:
                    await _send_pic(ch, t, cur)
                    notice = f"✓ عکس #{t['id']} در «{esc(ch['title'])}» ارسال شد."
                except Exception as ex:
                    log.exception("send pic failed for trade %s", t["id"])
                    notice = f"❌ ارسال عکس ناموفق: <code>{esc(str(ex))}</code>"
                await view_trades(update, 0, kind, notice)
                return
            try:
                if p[1] == "cancel" and t["status"] == "waiting":
                    t["status"] = "cancelled"  # never posted, nothing to reply to
                elif p[1] == "cancel" and t["status"] == "pending":
                    await _post_event(ch, t, "cancel", trading.cancel_text(t), t["pending_msg_id"])
                    t["status"] = "cancelled"
                elif p[1] == "be" and t["status"] == "open":
                    if t["be_active"]:
                        return
                    cur = await trading.get_price(t["pair"])
                    if cur is None:
                        await q.message.reply_text("❌ قیمت در دسترس نیست.")
                        return
                    risk = trading.risk_of(t)
                    d = 1 if t["side"] == "LONG" else -1
                    if d * (cur - t["entry"]) / risk <= 0:
                        where = "بالاتر" if t["side"] == "LONG" else "پایین‌تر"
                        await q.message.reply_text(
                            f"پوزیشن هنوز در سود نیست (قیمت فعلی {fmt_price(cur)} هنوز {where} از "
                            f"Entry {fmt_price(t['entry'])} نرفته)؛ بریک‌اون ممکن نیست.")
                        return
                    await _link_post(ch, t, "be_set", await _post(ch, trading.be_set_text(t), t["open_msg_id"]))
                    t["be_active"] = True
                elif p[1] == "close" and t["status"] == "open":
                    cur = await trading.get_price(t["pair"])
                    if cur is None:
                        await q.message.reply_text("❌ قیمت در دسترس نیست.")
                        return
                    risk = trading.risk_of(t)
                    d = 1 if t["side"] == "LONG" else -1
                    t["result_r"] = round(d * (cur - t["entry"]) / risk, 2)
                    t["outcome"] = "manual"
                    await _post_event(ch, t, _final_ev(t), trading.final_text(t), t["open_msg_id"], cur)
                    t["status"] = "closed"
                    t["closed_at"] = datetime.now(timezone.utc).isoformat()
                save()
            except Exception as ex:
                await q.message.reply_text(f"❌ {esc(str(ex))}", parse_mode=ParseMode.HTML)
                return
        await view_trades(update, 0, kind)

    # ---------- settings ----------
    elif a == "set":
        if p[1] == "seeall":
            if not is_owner(uid):
                await q.answer("⛔ فقط مالک", show_alert=True)
                return
            sa = settings().setdefault("see_all", [])
            if uid in sa:
                sa.remove(uid)
            else:
                sa.append(uid)
            save()
            await (view_admins if len(p) > 2 and p[2] == "a" else view_settings)(update)
            return
        if p[1] == "rewtoggle":
            settings()["reward_on"] = not settings()["reward_on"]
            save()
            await view_settings(update)
            return
        if p[1] == "img":
            await view_img(update)
            return
        if p[1] == "imgev":
            ev = settings()["img_events"]
            if p[2] in ev:
                ev[p[2]] = not ev[p[2]]
                save()
            await view_img(update)
            return
        if p[1] == "pend":
            await view_pend(update)
            return
        if p[1] == "neartoggle":
            settings()["near_on"] = not settings().get("near_on", False)
            save()
            await view_pend(update)
            return
        if p[1] == "tpctoggle":
            settings()["tp_cancel_on"] = not settings().get("tp_cancel_on", False)
            save()
            await view_pend(update)
            return
        if p[1] == "opptoggle":
            settings()["opp_cancel_on"] = not settings().get("opp_cancel_on", True)
            save()
            await view_pend(update)
            return
        if p[1] == "tpcmode":
            st_ = settings()
            st_["tp_cancel_mode"] = "post" if st_.get("tp_cancel_mode", "delete") == "delete" else "delete"
            save()
            await view_pend(update)
            return
        if p[1] == "near":
            ud["state"] = "set_near"
            await show(update, "⌖ <b>ارسال نزدیک ورود</b>\n\n"
                               "سیگنال فوراً در کانال پست نمی‌شود؛ بات صبر می‌کند تا قیمت به Entry نزدیک شود.\n"
                               "فاصله = چند درصد از <b>فاصله‌ی SL تا Entry</b> (ریسک پوزیشن، 1R).\n\n"
                               "<blockquote>مثال LONG: Entry 86,930 · SL 86,280 → فاصله 650\n"
                               "30 ← فاصله 195 ← پست وقتی قیمت به 87,125 برسد</blockquote>\n\n"
                               f"الان: <b>{pct(settings()['near_pct'])}%</b>\n"
                               "یک عدد بین <b>1</b> تا <b>100</b> بفرست.",
                       M([[B("✕ انصراف", "set:pend")]]))
            return
        if p[1] == "imglabels":
            settings()["img_labels"] = not settings().get("img_labels", False)
            save()
            await view_img(update)
            return
        if p[1] == "imgall":
            ev = settings()["img_events"]
            val = not all(ev.get(k) for k, _ in IMG_EVENTS)
            for k, _ in IMG_EVENTS:
                ev[k] = val
            save()
            await view_img(update)
            return
        if p[1] == "imgtf":
            tfs = chart.TIMEFRAMES
            cur_tf = settings().get("img_tf", "15m")
            settings()["img_tf"] = tfs[(tfs.index(cur_tf) + 1) % len(tfs)] if cur_tf in tfs else "15m"
            save()
            await view_img(update)
            return
        if p[1] == "imgtest":
            price = await trading.get_price("BTCUSDT")
            if price is None:
                await q.message.reply_text("❌ قیمت BTC دریافت نشد.")
                return
            demo = {"pair": "BTCUSDT", "symbol": "BTC", "side": "LONG", "entry": price,
                    "sl": round(price * 0.995, 1), "tp": round(price * 1.01, 1), "no": 0}
            png = await chart.snapshot(await trading._sess(), demo,
                                       settings().get("img_tf", "15m"), price, BOT_NAME,
                                       badge=("OPENED", chart.UP),
                                       labels=settings().get("img_labels", False))
            if not png:
                await q.message.reply_text("❌ ساخت عکس ناموفق بود (کندل‌ها دریافت نشد).")
                return
            await q.message.reply_photo(png, caption="👁 نمونه‌ی عکس ورود (BTC LONG، 1:2)")
            return
        if p[1] not in ("rewards", "be"):
            return
        ud["state"] = {"rewards": "set_rewards", "be": "set_be"}[p[1]]
        prompts = {
            "rewards": "🏆 <b>فاصله‌ی ریوارد</b>\n\n"
                       "فقط یک عدد بفرست؛ هر چند R یک‌بار ریوارد پست شود:\n\n"
                       "<code>1</code> ← 1R، 2R، 3R …\n"
                       "<code>2</code> ← 2R، 4R، 6R …\n"
                       "<code>0.5</code> ← 0.5R، 1R، 1.5R …\n\n"
                       "برای خاموش کردن کامل، از دکمه‌ی «خاموش کردن ریوارد» در تنظیمات استفاده کن.",
            "be": "🛡 بعد از رسیدن به کدام ریوارد SL به Entry منتقل شود؟ مثلاً <code>1</code>\n"
                  "برای غیرفعال‌سازی: <code>0</code>",
        }
        await show(update, prompts[p[1]], cancel_kb())

    # ---------- report ----------
    elif a == "sum":
        keys = [c["key"] for c in channels()] if p[1] == "all" else [p[2]]
        sent, failed = 0, []
        async with LOCK:
            for key in keys:
                ch = get_channel(key)
                rows = _unreported(key, uid)
                if not ch or not rows:
                    continue
                try:
                    await _post(ch, trading.summary_text(rows))
                except Exception as ex:
                    failed.append(f"{esc(ch['title'])}: <code>{esc(str(ex))}</code>")
                    continue
                for t in rows:
                    t["reported"] = True
                sent += len(rows)
            save()
        if failed:
            notice = "❌ ارسال ناموفق:\n" + "\n".join(failed)
        elif sent:
            notice = f"✅ گزارش {sent} معامله ارسال شد."
        else:
            notice = "ℹ️ معامله‌ی گزارش‌نشده‌ای نبود."
        await view_stats(update, notice)


# ====================== Price monitor ======================

def _tp_reached(t, price) -> bool:
    return price >= t["tp"] if t["side"] == "LONG" else price <= t["tp"]


async def _cancel_on_tp(ch, t, outcome: str = "tp_cancel"):
    """Drop a pending that will not be filled (price hit TP first, or the opposite side of
    the same symbol was activated): delete its post or reply a cancel, per the ⊘ mode."""
    msg = t.get("pending_msg_id")
    if msg:
        deleted = False
        if settings().get("tp_cancel_mode", "delete") == "delete":
            try:
                deleted = bool(await BOT.delete_message(chat_id=ch["chat"], message_id=msg))
            except Exception:
                log.exception("delete pending msg failed for trade %s", t.get("id"))
        if not deleted:
            await _post_event(ch, t, "cancel", trading.cancel_text(t), msg)
    t["status"] = "cancelled"
    t["outcome"] = outcome
    t["closed_at"] = datetime.now(timezone.utc).isoformat()
    save()


async def _cancel_on_level(ch, t, cur):
    """Price passed the signal's Cancel level before Entry was hit: cancel the pending
    (reply «Cancelled · Price passed Cancel …» under the post; never-posted ones are dropped)."""
    if t.get("pending_msg_id"):
        await _post_event(ch, t, "cancel", trading.cancel_level_text(t), t["pending_msg_id"], cur)
    t["status"] = "cancelled"
    t["outcome"] = "level_cancel"
    t["closed_at"] = datetime.now(timezone.utc).isoformat()
    t["last_price"] = cur
    save()


async def _cancel_opposite(t):
    """A position just opened: cancel every waiting / pending signal of the same symbol in the
    same channel on the OTHER side (Buy activated → pending Sells cancelled, and vice versa)."""
    if not settings().get("opp_cancel_on", True):
        return
    for o in trades():
        if (o is t or o["status"] not in ("waiting", "pending") or o["pair"] != t["pair"]
                or o["channel"] != t["channel"] or o["side"] == t["side"]
                or o.get("owner") != t.get("owner")):
            continue
        try:
            if o["status"] == "waiting":  # never posted: just drop it
                o["status"] = "cancelled"
                o["outcome"] = "opp_cancel"
                o["closed_at"] = datetime.now(timezone.utc).isoformat()
                save()
            else:
                ch = get_channel(o["channel"])
                if ch:
                    await _cancel_on_tp(ch, o, "opp_cancel")
        except Exception:
            log.exception("opposite cancel failed for trade %s", o.get("id"))


async def _process(t, cur):
    ch = get_channel(t["channel"])
    if not ch:
        return
    prev = t.get("last_price") or cur

    if t["status"] == "waiting":
        e = t["entry"]
        crossed = (prev - e) * (cur - e) <= 0
        if not crossed and trading.cancel_reached(t, cur):
            await _cancel_on_level(ch, t, cur)  # never posted: just drop it
            return
        if not crossed and settings().get("tp_cancel_on") and _tp_reached(t, cur):
            await _cancel_on_tp(ch, t)  # never posted: just drop it
            return
        if not (crossed or is_near(t, cur)):
            t["last_price"] = cur
            return
        # price came close to Entry: post the pending signal now (order type from today's price)
        t["order"] = trading.order_type(t["side"], e, prev)
        t["pending_msg_id"] = await _post_event(ch, t, "pending", trading.pending_text(t), None, cur)
        t["status"] = "pending"
        t["posted_at"] = datetime.now(timezone.utc).isoformat()
        save()
        # falls through: if price already crossed Entry, the position opens in the same tick

    if t["status"] == "pending":
        e = t["entry"]
        if (prev - e) * (cur - e) > 0:
            if trading.cancel_reached(t, cur):
                await _cancel_on_level(ch, t, cur)
                return
            if settings().get("tp_cancel_on") and _tp_reached(t, cur):
                await _cancel_on_tp(ch, t)
                return
            t["last_price"] = cur
            return
        t["open_msg_id"] = await _post_event(ch, t, "entry", trading.open_text(t),
                                             t["pending_msg_id"], cur)
        t["status"] = "open"
        t["opened_at"] = datetime.now(timezone.utc).isoformat()
        t["risk0"] = abs(t["entry"] - t["sl"])  # 1R is fixed from here on (SL may be edited)
        save()
        await _cancel_opposite(t)  # e.g. Buy activated → pending Sell of this symbol cancelled

    risk = trading.risk_of(t)
    d = 1 if t["side"] == "LONG" else -1
    r = d * (cur - t["entry"]) / risk
    steps = [s for s in t["steps"] if s < t["rr"]] if settings()["reward_on"] else []

    if r >= t["rr"]:
        for s in steps:
            if s not in t["rewards_hit"]:
                t["rewards_hit"].append(s)
        t["result_r"] = t["rr"]
        t["outcome"] = "tp"
        await _post_event(ch, t, "tp", trading.final_text(t), t["open_msg_id"], cur)
        t["status"] = "closed"
        t["closed_at"] = datetime.now(timezone.utc).isoformat()
    else:
        for s in steps:
            if r >= s and s not in t["rewards_hit"]:
                await _post_event(ch, t, "reward", trading.reward_text(t, s), t["open_msg_id"],
                                  cur, s)
                t["rewards_hit"].append(s)
                save()
        if not t["be_active"] and trading.riskfree_reached(t, cur):
            t["be_active"] = True  # signal's RiskFree level reached: stop moves to Entry
            await _link_post(ch, t, "be_set", await _post(ch, trading.riskfree_text(t), t["open_msg_id"]))
            save()
        # An explicit RiskFree level set on this signal has priority over the
        # global R-based break-even setting. Wait for that exact price instead.
        if (t["be_after"] and t.get("riskfree") is None
                and not t["be_active"] and r >= t["be_after"]):
            t["be_active"] = True
            await _link_post(ch, t, "be_set", await _post(ch, trading.be_set_text(t), t["open_msg_id"]))
            save()
        # stop level in R: -1R by default, anywhere if SL was edited; BE lifts it to at least 0R
        sl_r = trading.stop_r(t)
        floor = max(sl_r, 0.0) if t["be_active"] else sl_r
        if r <= floor:
            t["result_r"] = round(floor, 2)
            t["outcome"] = "be" if abs(floor) < 0.005 else "sl"
            await _post_event(ch, t, _final_ev(t), trading.final_text(t), t["open_msg_id"], cur)
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
                if t["status"] in ("waiting", "pending", "open"):
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
        # Command list and menu button are managed manually via @BotFather; the bot does not touch them.
    except Exception as ex:
        log.warning("bot profile setup failed: %s", ex)
    _reschedule(app.job_queue)
    _reschedule_auto(app.job_queue)
    _reschedule_backup(app.job_queue)
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
