"""magical_org — Telegram signal poster (admin panel, inline-only UI).

Commands: /start and /help only. Everything else is driven by inline buttons.
"""
import asyncio
import copy
import html
import json
import logging
import os
import re
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
from telegram import BotCommand
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, Update
from telegram import __version__ as PTB_VERSION
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import poster
import trading
from trading import fmt_price, fmt_r, fmt_rr, fmt_step
from session.proxy_manager import get_proxy_manager
from session.session_manager import get_manager, session_display_name

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Owners come from .env. Extra admins (managed from the bot) live in data.json.
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DATA_FILE = os.getenv("DATA_FILE", "data.json")
BOT_NAME = "magical_org"
START_TS = time.time()
LAST_TICK = 0.0


# ====================== Storage (data.json) ======================

DEFAULTS = {
    "channels": [],
    "trades": [],
    "admins": [],
    "trade_counter": 0,
    "settings": {"reward_steps": [1, 2, 3], "be_after": 0, "poll_seconds": 5},
}

_data = None


def db() -> dict:
    global _data
    if _data is None:
        loaded = {}
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        _data = loaded
        for k, v in DEFAULTS.items():
            _data.setdefault(k, copy.deepcopy(v))
        for k, v in DEFAULTS["settings"].items():
            _data["settings"].setdefault(k, v)
    return _data


def save():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def settings() -> dict:
    return db()["settings"]


def channels() -> list:
    return db()["channels"]


def get_channel(key: str):
    return next((c for c in channels() if c["key"] == key), None)


def add_channel(chat, title: str, session: str) -> dict:
    ch = {"key": uuid.uuid4().hex[:6], "chat": chat, "title": title,
          "session": session, "counter": 0}
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
        "steps": sorted(float(x) for x in s["reward_steps"]),
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
    for k in ("pair", "symbol", "side", "entry", "tp", "sl", "rr"):
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

SIGNAL_FORMAT = "<code>btc\nInt81000\nTp87000\nSl80000</code>"

HELP = (
    "📖 <b>راهنما</b>\n\n"
    "<b>ارسال سیگنال</b>\n"
    "پیام را با این قالب بفرست:\n\n"
    f"{SIGNAL_FORMAT}\n\n"
    "• <code>Int</code> = Entry ، <code>Tp</code> = Take Profit ، <code>Sl</code> = Stop Loss\n"
    "• جهت (LONG/SHORT) و Risk/Reward خودکار محاسبه می‌شود.\n"
    "• پیش‌نمایش می‌آید؛ کانال را انتخاب کن تا از طریق سشن همان کانال پست شود.\n\n"
    "<b>چرخه‌ی معامله</b>\n"
    "🟡 Pending ← 🟢 Position Opened ← 🏆 Reward ها ← 🎯 TP / 🛑 SL\n"
    "همه‌ی مراحل به‌صورت ریپلای روی پست اصلی ارسال می‌شوند.\n\n"
    "<b>مدیریت</b>\n"
    "تمام بخش‌ها (کانال‌ها، سشن‌ها، تنظیمات، گزارش، ادمین) از دکمه‌های زیر در دسترس‌اند."
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


def home_kb():
    return M([
        [B("📋 معاملات فعال", "menu:trades"), B("📈 آمار", "menu:stats")],
        [B("📊 پست گزارش", "menu:sum"), B("📺 کانال‌ها", "menu:channels")],
        [B("👤 سشن‌ها", "menu:sessions"), B("⚙️ تنظیمات", "menu:settings")],
        [B("🛡 پنل ادمین", "adm:home"), B("❓ راهنما", "menu:help")],
    ])


def back_kb():
    return M([[B("🏠 منو", "menu:home")]])


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
    d = ud.pop("new_ses", None)
    if d:
        try:
            await poster.login_cancel(d["name"])
        except Exception:
            pass
        if d.get("proxy"):
            try:
                get_proxy_manager().delete_proxy(d["proxy"])
            except Exception:
                pass
    for k in ("state", "new_ch", "draft"):
        ud.pop(k, None)


# ====================== Views ======================

async def view_home(update):
    pend, opn, unrep = _counts()
    text = (
        f"🏠 <b>{BOT_NAME}</b> · پنل مدیریت\n"
        "━━━━━━━━━━━━━━\n"
        f"📋 معاملات فعال: <b>{pend + opn}</b>  (🟡 {pend} · 🟢 {opn})\n"
        f"📊 گزارش‌نشده: <b>{unrep}</b>\n"
        f"📺 کانال‌ها: <b>{len(channels())}</b>   👤 سشن‌ها: <b>{len(get_manager().list_sessions())}</b>\n"
        "━━━━━━━━━━━━━━\n"
        "برای ارسال سیگنال، پیام را در این قالب بفرست:\n\n"
        f"{SIGNAL_FORMAT}"
    )
    await show(update, text, home_kb())


async def view_help(update):
    await show(update, HELP, back_kb())


async def view_sessions(update):
    sess = get_manager().list_sessions()
    lines = ["👤 <b>سشن‌ها</b>\n"]
    kb = []
    for s in sess:
        used = [c["title"] for c in channels() if c["session"] == s["name"]]
        lines.append(f"• <code>{esc(s['name'])}</code> — {esc(session_display_name(s))}"
                     + (f"\n   📺 {esc(', '.join(used))}" if used else ""))
        kb.append([B(f"🔌 تست {s['name']}", f"ses:test:{s['name']}"),
                   B("🗑 حذف", f"ses:del:{s['name']}")])
    if not sess:
        lines.append("هنوز سشنی اضافه نشده است.")
    kb.append([B("➕ افزودن سشن", "ses:add")])
    kb.append([B("🏠 منو", "menu:home")])
    await show(update, "\n".join(lines), M(kb))


async def view_channels(update):
    chans = channels()
    lines = ["📺 <b>کانال‌ها</b>\n"]
    kb = []
    for c in chans:
        lines.append(f"• {esc(c['title'])} <code>{esc(str(c['chat']))}</code> ← سشن <code>{esc(c['session'])}</code>")
        kb.append([B(f"🗑 حذف {c['title']}", f"ch:del:{c['key']}")])
    if not chans:
        lines.append("هنوز کانالی اضافه نشده است.")
    kb.append([B("➕ افزودن کانال", "ch:add")])
    kb.append([B("🏠 منو", "menu:home")])
    await show(update, "\n".join(lines), M(kb))


async def view_trades(update, page: int = 0):
    active = active_trades()
    if not active:
        await show(update, "📋 <b>معاملات فعال</b>\n\nمعامله‌ی فعالی وجود ندارد.",
                   M([[B("🔄 بروزرسانی", "trp:0"), B("🏠 منو", "menu:home")]]))
        return
    per = 5
    pages = (len(active) + per - 1) // per
    page = max(0, min(page, pages - 1))
    chunk = active[page * per:(page + 1) * per]
    pairs = list({t["pair"] for t in chunk})
    res = await asyncio.gather(*(trading.get_price(p) for p in pairs))
    prices = dict(zip(pairs, res))

    lines = [f"📋 <b>معاملات فعال</b> ({len(active)})\n"]
    kb = []
    for t in chunk:
        st = "🟡 Pending" if t["status"] == "pending" else "🟢 Open"
        hit = ""
        if t["rewards_hit"]:
            hit = " | 🏆 " + ",".join(fmt_step(s) for s in sorted(t["rewards_hit"]))
        cur = prices.get(t["pair"])
        now = ""
        if cur is not None:
            now = f"\n   💵 الان: {fmt_price(cur)}"
            if t["status"] == "open":
                risk = abs(t["entry"] - t["sl"])
                d = 1 if t["side"] == "LONG" else -1
                now += f" ({fmt_r(d * (cur - t['entry']) / risk)})"
        lines.append(f"<b>#{t['id']}</b> {esc(t['symbol'])} {t['side']} — {st}{hit}\n"
                     f"   Entry {fmt_price(t['entry'])} | TP {fmt_price(t['tp'])} | SL {fmt_price(t['sl'])}{now}")
        if t["status"] == "pending":
            kb.append([B(f"❌ کنسل #{t['id']}", f"tr:cancel:{t['id']}")])
        else:
            kb.append([B(f"🔒 بستن دستی #{t['id']}", f"tr:close:{t['id']}")])
    if pages > 1:
        kb.append([B("⬅️", f"trp:{max(page - 1, 0)}"), B(f"{page + 1}/{pages}", "noop"),
                   B("➡️", f"trp:{min(page + 1, pages - 1)}")])
    kb.append([B("🔄 بروزرسانی", f"trp:{page}"), B("🏠 منو", "menu:home")])
    await show(update, "\n\n".join(lines[:1]) + "\n" + "\n\n".join(lines[1:]), M(kb))


async def view_stats(update):
    closed = [t for t in trades() if t["status"] == "closed" and t["result_r"] is not None]
    if not closed:
        await show(update, "📈 <b>آمار</b>\n\nهنوز معامله‌ی بسته‌شده‌ای وجود ندارد.", back_kb())
        return
    rs = [t["result_r"] for t in closed]
    wins = sum(1 for r in rs if r > 0.005)
    losses = sum(1 for r in rs if r < -0.005)
    be = len(rs) - wins - losses
    total = sum(rs)
    lines = [
        "📈 <b>آمار کلی</b>",
        "━━━━━━━━━━━━━━",
        f"📦 کل معاملات بسته‌شده: <b>{len(rs)}</b>",
        f"💎 مجموع نتیجه: <b>{fmt_r(total)}</b>  (میانگین {fmt_r(total / len(rs))})",
        f"✅ {wins}  |  ❌ {losses}  |  ⚪️ {be}",
        f"🎯 Win Rate: <b>{round(wins / len(rs) * 100)}%</b>",
        f"🥇 بهترین: {fmt_r(max(rs))}   🥀 بدترین: {fmt_r(min(rs))}",
    ]
    per_ch = []
    for c in channels():
        rows = [t["result_r"] for t in closed if t["channel"] == c["key"]]
        if rows:
            per_ch.append(f"• {esc(c['title'])}: {len(rows)} معامله · <b>{fmt_r(sum(rows))}</b>")
    if per_ch:
        lines += ["", "📺 <b>به تفکیک کانال</b>"] + per_ch
    await show(update, "\n".join(lines), back_kb())


async def view_settings(update):
    s = settings()
    steps = ", ".join(fmt_step(x) for x in s["reward_steps"]) or "—"
    be = fmt_step(s["be_after"]) if s["be_after"] else "غیرفعال"
    text = (
        "⚙️ <b>تنظیمات</b>\n\n"
        f"🏆 پله‌های ریوارد: <b>{steps}</b>\n"
        f"🛡 انتقال SL به Entry بعد از: <b>{be}</b>\n"
        f"⏱ فاصله‌ی چک قیمت: <b>{s['poll_seconds']} ثانیه</b>"
    )
    kb = M([
        [B("🏆 پله‌های ریوارد", "set:rewards")],
        [B("🛡 Break-even", "set:be"), B("⏱ فاصله‌ی چک", "set:poll")],
        [B("🏠 منو", "menu:home")],
    ])
    await show(update, text, kb)


async def view_sum(update):
    kb = []
    for c in channels():
        n = sum(1 for t in trades()
                if t["channel"] == c["key"] and t["status"] == "closed" and not t["reported"])
        kb.append([B(f"📤 {c['title']} ({n} معامله)", f"sum:go:{c['key']}")])
    kb.append([B("🏠 منو", "menu:home")])
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
        rows.append([B("🔄 ریستارت بات", "adm:restart")])
    rows.append([B("🏠 منو", "menu:home")])
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
    kb.append([B("⬅️ پنل ادمین", "adm:home"), B("🏠 منو", "menu:home")])
    await show(update, "\n".join(lines), M(kb))


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
        "🖥 <b>وضعیت سیستم</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"⏱ Uptime: <b>{uptime_str(time.time() - START_TS)}</b>\n"
        f"🐍 Python {sys.version_info.major}.{sys.version_info.minor} · PTB {PTB_VERSION}\n"
        f"📡 API قیمت: {api}\n"
        f"🔁 آخرین چک قیمت: {tick}  (هر {settings()['poll_seconds']} ثانیه)\n"
        "━━━━━━━━━━━━━━\n"
        f"📋 فعال: {pend + opn} (🟡 {pend} · 🟢 {opn})\n"
        f"📦 بسته‌شده: {closed} (گزارش‌نشده {unrep}) · ❌ کنسل: {cancelled}\n"
        f"📺 کانال‌ها: {len(channels())}   👤 سشن‌ها: {len(get_manager().list_sessions())}"
    )
    await show(update, text, M([[B("🔄 بروزرسانی", "adm:status"), B("⬅️ پنل ادمین", "adm:home")]]))


def _reschedule(job_queue):
    for j in job_queue.get_jobs_by_name("monitor"):
        j.schedule_removal()
    job_queue.run_repeating(monitor, interval=settings()["poll_seconds"], first=3, name="monitor")


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
    await view_home(update)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or not is_admin(u.id):
        await _deny(update)
        return
    await say(update, HELP, home_kb())


# ====================== Input flows ======================

async def _try_delete(update):
    try:
        if update.message:
            await update.message.delete()
    except Exception:
        pass


def _parse_proxy(raw: str):
    u = urlparse(raw.strip())
    scheme = u.scheme.lower()
    if scheme in ("socks5h",):
        scheme = "socks5"
    if scheme not in ("socks5", "socks4", "http") or not u.hostname or not u.port:
        return None
    return scheme, u.hostname, u.port, unquote(u.username or ""), unquote(u.password or "")


async def _finish_session(update, context):
    d = context.user_data.pop("new_ses")
    context.user_data.pop("state", None)
    me = await poster.login_finish(d["name"])
    mgr = get_manager()
    mgr.add_session(d["name"], d["phone"], d["api_id"], d["api_hash"], d.get("proxy", ""))
    mgr.set_session_identity(d["name"], me.first_name or "", me.username or "")
    await say(update, f"✅ سشن <code>{esc(d['name'])}</code> با موفقیت وصل شد ({esc(me.first_name or '')}).",
              M([[B("👤 سشن‌ها", "menu:sessions"), B("🏠 منو", "menu:home")]]))


async def _proxy_step(update, context, raw: str):
    ud = context.user_data
    d = ud["new_ses"]
    if raw != "-":
        p = _parse_proxy(raw)
        if not p:
            await say(update, "❌ فرمت پراکسی درست نیست.\nمثال: <code>socks5://user:pass@host:port</code>",
                      _proxy_kb())
            return
        pname = f"px_{d['name']}"
        pm = get_proxy_manager()
        r = pm.add_proxy(pname, p[0], p[1], p[2], p[3], p[4])
        if not r["success"]:
            pm.delete_proxy(pname)
            pm.add_proxy(pname, p[0], p[1], p[2], p[3], p[4])
        d["proxy"] = pname
    try:
        await poster.login_start(d["name"], d["api_id"], d["api_hash"], d["phone"], d.get("proxy", ""))
    except Exception as ex:
        await reset_flow(ud)
        await say(update, f"❌ ارسال کد ناموفق بود:\n<code>{esc(str(ex))}</code>", home_kb())
        return
    ud["state"] = "ses_code"
    await say(update,
              "🔐 <b>(۶/۶)</b> کد تلگرام را بفرست.\n"
              "تلگرام کدی را که عیناً در چت فرستاده شود باطل می‌کند؛ پس با فاصله بفرست:\n"
              "<code>1 2 3 4 5</code>", cancel_kb())


def _proxy_kb():
    return M([[B("⏭ بدون پراکسی", "ses:noproxy")], [B("✖️ انصراف", "menu:home")]])


async def handle_state(update, context, st, text):
    ud = context.user_data
    if st == "ses_name":
        mgr = get_manager()
        if not re.fullmatch(r"[A-Za-z0-9_]{2,24}", text):
            await say(update, "❌ فقط حروف انگلیسی، عدد و _ (۲ تا ۲۴ کاراکتر).", cancel_kb())
            return
        if text in mgr.sessions or (mgr.session_dir / f"{text}.session").exists():
            await say(update, "❌ این نام قبلاً استفاده شده است.", cancel_kb())
            return
        ud["new_ses"] = {"name": text}
        ud["state"] = "ses_api_id"
        await say(update, "🔑 <b>(۲/۶)</b> <code>api_id</code> اکانت را بفرست (از my.telegram.org):", cancel_kb())
    elif st == "ses_api_id":
        if not text.isdigit():
            await say(update, "❌ api_id فقط عدد است.", cancel_kb())
            return
        ud["new_ses"]["api_id"] = int(text)
        ud["state"] = "ses_api_hash"
        await say(update, "🔑 <b>(۳/۶)</b> <code>api_hash</code> را بفرست (پیام بعد از دریافت پاک می‌شود):", cancel_kb())
    elif st == "ses_api_hash":
        await _try_delete(update)
        if not re.fullmatch(r"[0-9a-fA-F]{32}", text):
            await say(update, "❌ api_hash باید ۳۲ کاراکتر هگز باشد.", cancel_kb())
            return
        ud["new_ses"]["api_hash"] = text
        ud["state"] = "ses_phone"
        await say(update, "📱 <b>(۴/۶)</b> شماره‌ی اکانت با کد کشور:\n<code>+491234567890</code>", cancel_kb())
    elif st == "ses_phone":
        phone = "+" + re.sub(r"\D", "", text)
        if len(phone) < 8:
            await say(update, "❌ شماره نامعتبر است.", cancel_kb())
            return
        if get_manager().is_phone_registered(phone):
            await say(update, "❌ این شماره قبلاً ثبت شده است.", cancel_kb())
            return
        ud["new_ses"]["phone"] = phone
        ud["state"] = "ses_proxy"
        await say(update, "🌐 <b>(۵/۶)</b> پراکسی (اختیاری):\n"
                          "<code>socks5://user:pass@host:port</code>\n"
                          "یا دکمه‌ی «بدون پراکسی» را بزن.", _proxy_kb())
    elif st == "ses_proxy":
        await _proxy_step(update, context, text)
    elif st == "ses_code":
        await _try_delete(update)
        code = re.sub(r"\D", "", text)
        try:
            res = await poster.login_code(ud["new_ses"]["name"], code)
        except Exception as ex:
            await say(update, f"❌ کد رد شد: <code>{esc(str(ex))}</code>\nدوباره بفرست یا انصراف بزن.", cancel_kb())
            return
        if res == "password":
            ud["state"] = "ses_pw"
            await say(update, "🔒 رمز تأیید دو مرحله‌ای را بفرست (پیام پاک می‌شود):", cancel_kb())
        else:
            await _finish_session(update, context)
    elif st == "ses_pw":
        await _try_delete(update)
        try:
            await poster.login_password(ud["new_ses"]["name"], text)
        except Exception as ex:
            await say(update, f"❌ رمز رد شد: <code>{esc(str(ex))}</code>\nدوباره بفرست یا انصراف بزن.", cancel_kb())
            return
        await _finish_session(update, context)
    elif st == "ch_chat":
        sess = get_manager().list_sessions()
        if not sess:
            ud.pop("state", None)
            await say(update, "❌ اول یک سشن اضافه کن.", M([[B("👤 سشن‌ها", "menu:sessions")]]))
            return
        ud["new_ch"] = {"raw": text}
        ud["state"] = None
        kb = [[B(session_display_name(s), f"chs:{s['name']}")] for s in sess]
        kb.append([B("✖️ انصراف", "menu:home")])
        await say(update, "کدام سشن در این کانال پست بگذارد؟\n"
                          "(باید ادمینِ کانال با دسترسی «ارسال پست» باشد)", M(kb))
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
                  M([[B("👥 ادمین‌ها", "adm:list"), B("🏠 منو", "menu:home")]]))
    elif st == "set_rewards":
        try:
            steps = sorted({float(x) for x in re.split(r"[,\s،]+", text) if x})
            if not steps or any(x <= 0 for x in steps):
                raise ValueError
        except ValueError:
            await say(update, "❌ مثل این بفرست: <code>1,2,3</code> یا <code>0.5,1,2</code>", cancel_kb())
            return
        settings()["reward_steps"] = steps
        save()
        ud.pop("state")
        await say(update, "✅ ذخیره شد (روی معاملات بعدی اعمال می‌شود).",
                  M([[B("⚙️ تنظیمات", "menu:settings"), B("🏠 منو", "menu:home")]]))
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
        await say(update, "✅ ذخیره شد.", M([[B("⚙️ تنظیمات", "menu:settings"), B("🏠 منو", "menu:home")]]))
    elif st == "set_poll":
        if not text.isdigit() or not 2 <= int(text) <= 60:
            await say(update, "❌ عددی بین 2 تا 60 بفرست.", cancel_kb())
            return
        settings()["poll_seconds"] = int(text)
        save()
        ud.pop("state")
        _reschedule(context.job_queue)
        await say(update, "✅ ذخیره شد.", M([[B("⚙️ تنظیمات", "menu:settings"), B("🏠 منو", "menu:home")]]))


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
    context.user_data["draft"] = parsed
    kb = [[B(f"📤 پست در {c['title']}", f"sig:post:{c['key']}")] for c in channels()]
    kb.append([B("✖️ لغو", "sig:cancel")])
    await say(update,
              f"👀 <b>پیش‌نمایش</b>\n\n{trading.pending_text(parsed)}\n\n"
              f"💵 قیمت لحظه‌ای {esc(parsed['symbol'])}: <b>{fmt_price(price)}</b>", M(kb))


@admin_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    st = context.user_data.get("state")
    if st:
        await handle_state(update, context, st, text)
    else:
        await handle_signal(update, context, text)


async def _post(ch, text, reply_to=None):
    return await poster.send(ch["session"], ch["chat"], text, reply_to)


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
        views = {"home": view_home, "sessions": view_sessions, "channels": view_channels,
                 "trades": view_trades, "settings": view_settings, "sum": view_sum,
                 "stats": view_stats, "help": view_help}
        await views.get(p[1], view_home)(update)

    elif a == "trp":
        await view_trades(update, int(p[1]))

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
            try:
                data = Path(DATA_FILE).read_bytes()
                name = f"magical_org_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
                await q.message.reply_document(document=data, filename=name,
                                               caption="💾 بکاپ data.json\n(سشن‌ها و کلیدها شامل نمی‌شوند)")
            except Exception as ex:
                await q.message.reply_text(f"❌ {esc(str(ex))}", parse_mode=ParseMode.HTML)
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
                       M([[B("⬅️ پنل ادمین", "adm:home"), B("🏠 منو", "menu:home")]]))
        elif sub == "restart":
            await show(update, "🔄 <b>ریستارت بات</b>\n\nبات چند ثانیه آفلاین می‌شود و سرویس systemd دوباره آن را بالا می‌آورد.",
                       confirm_kb("adm:restartok", "adm:home", "✅ ریستارت"))
        elif sub == "restartok":
            await show(update, "🔄 در حال ریستارت… چند ثانیه‌ی دیگر /start را بزن.")
            asyncio.get_running_loop().call_later(1.0, os.kill, os.getpid(), signal.SIGTERM)

    # ---------- sessions ----------
    elif a == "ses":
        if p[1] == "add":
            ud["state"] = "ses_name"
            await show(update, "➕ <b>افزودن سشن</b>\n\n📝 <b>(۱/۶)</b> یک نام انگلیسی برای سشن بفرست (مثلاً <code>acc1</code>):",
                       cancel_kb())
        elif p[1] == "noproxy":
            if ud.get("state") == "ses_proxy" and ud.get("new_ses"):
                await _proxy_step(update, context, "-")
        elif p[1] == "test":
            try:
                me = await poster.whoami(p[2])
                await q.message.reply_text(f"✅ وصل است: {esc(me.first_name or '')} @{esc(me.username or '-')}",
                                           parse_mode=ParseMode.HTML)
            except Exception as ex:
                await q.message.reply_text(f"❌ {esc(str(ex))}", parse_mode=ParseMode.HTML)
        elif p[1] == "del":
            if any(c["session"] == p[2] for c in channels()):
                await q.message.reply_text("⚠️ این سشن به یک کانال وصل است؛ اول کانال را حذف کن.")
                return
            await show(update, f"🗑 سشن <code>{esc(p[2])}</code> حذف شود؟ (فایل سشن پاک می‌شود)",
                       confirm_kb(f"ses:delok:{p[2]}", "menu:sessions"))
        elif p[1] == "delok":
            name = p[2]
            if any(c["session"] == name for c in channels()):
                await view_sessions(update)
                return
            await poster.drop_client(name)
            get_manager().delete_session(name)
            pm = get_proxy_manager()
            if pm.get_proxy(f"px_{name}"):
                pm.delete_proxy(f"px_{name}")
            await view_sessions(update)

    # ---------- channels ----------
    elif a == "ch":
        if p[1] == "add":
            ud["state"] = "ch_chat"
            await show(update, "➕ <b>افزودن کانال</b>\n\nآیدی کانال را بفرست:\n"
                               "<code>@username</code> یا <code>-100…</code> یا لینک t.me", cancel_kb())
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

    elif a == "chs":
        raw = ud.get("new_ch", {}).get("raw")
        if not raw:
            return
        try:
            info = await poster.verify_channel(p[1], raw)
        except Exception as ex:
            await show(update, f"❌ کانال پیدا نشد: <code>{esc(str(ex))}</code>\n"
                               "سشن باید عضو/ادمین کانال باشد.", back_kb())
            return
        if not info["can_post"]:
            await show(update, "❌ این سشن در کانال ادمین با دسترسی «ارسال پست» نیست.", back_kb())
            return
        ud.pop("new_ch", None)
        add_channel(info["chat"], info["title"], p[1])
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
                   M([[B("📋 معاملات فعال", "menu:trades"), B("🏠 منو", "menu:home")]]))

    # ---------- trades ----------
    elif a == "tr":
        tid = int(p[2])
        t = get_trade(tid)
        if p[1] in ("cancel", "close"):
            if not t or t["status"] not in ("pending", "open"):
                await view_trades(update)
                return
            what = "کنسل" if p[1] == "cancel" else "به‌صورت دستی بسته"
            await show(update, f"⚠️ معامله‌ی <b>#{tid}</b> ({esc(t['symbol'])} {t['side']}) {what} شود؟\n"
                               "پیام مربوطه در کانال ارسال می‌شود.",
                       confirm_kb(f"tr:{p[1]}ok:{tid}", "trp:0"))
            return
        async with LOCK:
            ch = t and get_channel(t["channel"])
            if not t or not ch:
                await view_trades(update)
                return
            try:
                if p[1] == "cancelok" and t["status"] == "pending":
                    await _post(ch, trading.cancel_text(t), t["pending_msg_id"])
                    t["status"] = "cancelled"
                elif p[1] == "closeok" and t["status"] == "open":
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
        await view_trades(update)

    # ---------- settings ----------
    elif a == "set":
        ud["state"] = {"rewards": "set_rewards", "be": "set_be", "poll": "set_poll"}[p[1]]
        prompts = {
            "rewards": "🏆 پله‌های ریوارد را با کاما بفرست. مثلاً <code>1,2,3</code>\n"
                       "(وقتی قیمت به 1R، 2R، 3R در سود رسید پست می‌گذارد.)",
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
        t["open_msg_id"] = await _post(ch, trading.open_text(t), t["pending_msg_id"])
        t["status"] = "open"
        t["opened_at"] = datetime.now(timezone.utc).isoformat()
        save()

    risk = abs(t["entry"] - t["sl"])
    d = 1 if t["side"] == "LONG" else -1
    r = d * (cur - t["entry"]) / risk
    steps = [s for s in t["steps"] if s < t["rr"]]

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
                if t["be_after"] and s >= t["be_after"]:
                    t["be_active"] = True
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
    try:
        me = await app.bot.get_me()
        if me.first_name != BOT_NAME:
            await app.bot.set_my_name(BOT_NAME)
        await app.bot.set_my_commands([
            BotCommand("start", "پنل مدیریت"),
            BotCommand("help", "راهنما"),
        ])
    except Exception as ex:
        log.warning("bot profile setup failed: %s", ex)
    _reschedule(app.job_queue)


def main():
    if not BOT_TOKEN or not ADMIN_IDS:
        raise SystemExit("Set BOT_TOKEN and ADMIN_IDS in the .env file.")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("help", cmd_help, filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
