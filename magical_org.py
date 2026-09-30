import asyncio
import copy
import json
import os
import uuid
import html
import logging
import re
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, Update
from telegram.constants import ParseMode
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
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DATA_FILE = os.getenv("DATA_FILE", "data.json")


# ====================== Storage (data.json) ======================

DEFAULTS = {
    "channels": [],
    "trades": [],
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


# ====================== Bot ======================

LOCK = asyncio.Lock()
esc = html.escape

HELP = (
    "سیگنال را این‌طوری بفرست:\n\n"
    "<code>btc\nInt81000\nTp87000\nSl80000</code>\n\n"
    "بات پیش‌نمایش می‌سازد، تو کانال را انتخاب می‌کنی و از طریق سشن همان کانال پست می‌شود."
)


def admin_only(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if not u or u.id not in ADMIN_IDS:
            return
        return await fn(update, context)
    return wrapper


async def show(update: Update, text: str, kb=None):
    kw = dict(text=text, parse_mode=ParseMode.HTML, reply_markup=kb)
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(**kw)
            return
        except Exception:
            await q.message.reply_text(**kw)
    else:
        await update.effective_message.reply_text(**kw)


def home_kb():
    return M([
        [B("📋 معاملات فعال", "menu:trades"), B("📊 پست گزارش", "menu:sum")],
        [B("📺 کانال‌ها", "menu:channels"), B("👤 سشن‌ها", "menu:sessions")],
        [B("⚙️ تنظیمات", "menu:settings")],
    ])


def back_kb():
    return M([[B("🏠 منو", "menu:home")]])


async def view_home(update):
    await show(update, "🏠 <b>پنل ادمین</b>\n\n" + HELP, home_kb())


async def view_sessions(update):
    sess = get_manager().list_sessions()
    lines = ["👤 <b>سشن‌ها</b>\n"]
    kb = []
    for s in sess:
        used = [c["title"] for c in channels() if c["session"] == s["name"]]
        lines.append(f"• <code>{esc(s['name'])}</code> — {esc(session_display_name(s))}"
                     + (f"\n   کانال: {esc(', '.join(used))}" if used else ""))
        kb.append([B(f"🔌 تست {s['name']}", f"ses:test:{s['name']}"),
                   B("🗑 حذف", f"ses:del:{s['name']}")])
    if not sess:
        lines.append("هنوز سشنی نداری.")
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
        lines.append("هنوز کانالی اضافه نشده.")
    kb.append([B("➕ افزودن کانال", "ch:add")])
    kb.append([B("🏠 منو", "menu:home")])
    await show(update, "\n".join(lines), M(kb))


async def view_trades(update):
    active = [t for t in trades() if t["status"] in ("pending", "open")]
    if not active:
        await show(update, "معامله‌ی فعالی نیست.", back_kb())
        return
    lines = ["📋 <b>معاملات فعال</b>\n"]
    kb = []
    for t in active:
        st = "🟡 Pending" if t["status"] == "pending" else "🟢 Open"
        hit = ""
        if t["rewards_hit"]:
            hit = " | 🏆 " + ",".join(fmt_step(s) for s in sorted(t["rewards_hit"]))
        lines.append(f"#{t['id']} {esc(t['symbol'])} {t['side']} — {st}{hit}\n"
                     f"   Entry {fmt_price(t['entry'])} | TP {fmt_price(t['tp'])} | SL {fmt_price(t['sl'])}")
        if t["status"] == "pending":
            kb.append([B(f"❌ کنسل #{t['id']}", f"tr:cancel:{t['id']}")])
        else:
            kb.append([B(f"🔒 بستن دستی #{t['id']}", f"tr:close:{t['id']}")])
    kb.append([B("🏠 منو", "menu:home")])
    await show(update, "\n".join(lines), M(kb))


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
    await show(update, "📊 گزارش معاملات بسته‌شده (گزارش‌نشده) را برای کدام کانال بفرستم؟", M(kb))


def _reschedule(job_queue):
    for j in job_queue.get_jobs_by_name("monitor"):
        j.schedule_removal()
    job_queue.run_repeating(monitor, interval=settings()["poll_seconds"], first=3, name="monitor")


@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await view_home(update)


@admin_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data.pop("new_ses", None)
    if d:
        await poster.login_cancel(d["name"])
    context.user_data.clear()
    await update.message.reply_text("لغو شد.", reply_markup=home_kb())


async def _try_delete(update):
    try:
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
    await update.message.reply_text(f"✅ سشن <code>{esc(d['name'])}</code> وصل شد "
                                    f"({esc(me.first_name or '')}).", parse_mode=ParseMode.HTML,
                                    reply_markup=home_kb())


async def handle_state(update, context, st, text):
    ud = context.user_data
    msg = update.message
    if st == "ses_name":
        mgr = get_manager()
        if not re.fullmatch(r"[A-Za-z0-9_]{2,24}", text):
            await msg.reply_text("فقط حروف انگلیسی/عدد/_ (۲ تا ۲۴ کاراکتر).")
            return
        if text in mgr.sessions or (mgr.session_dir / f"{text}.session").exists():
            await msg.reply_text("این نام قبلاً استفاده شده.")
            return
        ud["new_ses"] = {"name": text}
        ud["state"] = "ses_api_id"
        await msg.reply_text("api_id اکانت را بفرست (از my.telegram.org):")
    elif st == "ses_api_id":
        if not text.isdigit():
            await msg.reply_text("api_id فقط عدد است.")
            return
        ud["new_ses"]["api_id"] = int(text)
        ud["state"] = "ses_api_hash"
        await msg.reply_text("api_hash را بفرست:")
    elif st == "ses_api_hash":
        await _try_delete(update)
        if not re.fullmatch(r"[0-9a-fA-F]{32}", text):
            await msg.reply_text("api_hash باید ۳۲ کاراکتر هگز باشد.")
            return
        ud["new_ses"]["api_hash"] = text
        ud["state"] = "ses_phone"
        await msg.reply_text("شماره‌ی اکانت با کد کشور (مثل +491234567890):")
    elif st == "ses_phone":
        phone = "+" + re.sub(r"\D", "", text)
        if len(phone) < 8:
            await msg.reply_text("شماره نامعتبر است.")
            return
        if get_manager().is_phone_registered(phone):
            await msg.reply_text("این شماره قبلاً ثبت شده.")
            return
        ud["new_ses"]["phone"] = phone
        ud["state"] = "ses_proxy"
        await msg.reply_text("پراکسی لازم است؟ به شکل <code>socks5://user:pass@host:port</code> بفرست، "
                             "یا <code>-</code> برای بدون پراکسی.", parse_mode=ParseMode.HTML)
    elif st == "ses_proxy":
        d = ud["new_ses"]
        if text != "-":
            p = _parse_proxy(text)
            if not p:
                await msg.reply_text("فرمت پراکسی درست نیست.")
                return
            pname = f"px_{d['name']}"
            r = get_proxy_manager().add_proxy(pname, p[0], p[1], p[2], p[3], p[4])
            if not r["success"]:
                get_proxy_manager().delete_proxy(pname)
                get_proxy_manager().add_proxy(pname, p[0], p[1], p[2], p[3], p[4])
            d["proxy"] = pname
        try:
            await poster.login_start(d["name"], d["api_id"], d["api_hash"], d["phone"], d.get("proxy", ""))
        except Exception as ex:
            await poster.login_cancel(d["name"])
            ud.pop("new_ses", None)
            ud.pop("state", None)
            await msg.reply_text(f"ارسال کد ناموفق بود: {esc(str(ex))}", parse_mode=ParseMode.HTML,
                                 reply_markup=home_kb())
            return
        ud["state"] = "ses_code"
        await msg.reply_text("کد تلگرام را بفرست. تلگرام کدی که عیناً در چت فرستاده شود را باطل می‌کند؛ "
                             "پس با فاصله بفرست، مثل: <code>1 2 3 4 5</code>", parse_mode=ParseMode.HTML)
    elif st == "ses_code":
        await _try_delete(update)
        code = re.sub(r"\D", "", text)
        try:
            res = await poster.login_code(ud["new_ses"]["name"], code)
        except Exception as ex:
            await msg.reply_text(f"کد رد شد: {esc(str(ex))}\nدوباره بفرست یا /cancel", parse_mode=ParseMode.HTML)
            return
        if res == "password":
            ud["state"] = "ses_pw"
            await msg.reply_text("رمز تأیید دو مرحله‌ای را بفرست:")
        else:
            await _finish_session(update, context)
    elif st == "ses_pw":
        await _try_delete(update)
        try:
            await poster.login_password(ud["new_ses"]["name"], text)
        except Exception as ex:
            await msg.reply_text(f"رمز رد شد: {esc(str(ex))}\nدوباره بفرست یا /cancel", parse_mode=ParseMode.HTML)
            return
        await _finish_session(update, context)
    elif st == "ch_chat":
        ud["new_ch"] = {"raw": text}
        ud["state"] = None
        sess = get_manager().list_sessions()
        if not sess:
            await msg.reply_text("اول یک سشن اضافه کن.", reply_markup=home_kb())
            return
        kb = [[B(session_display_name(s), f"chs:{s['name']}")] for s in sess]
        await msg.reply_text("کدام سشن در این کانال پست بگذارد؟ (باید ادمینِ کانال با دسترسی پست باشد)",
                             reply_markup=M(kb))
    elif st == "set_rewards":
        try:
            steps = sorted({float(x) for x in re.split(r"[,\s،]+", text) if x})
            if any(x <= 0 for x in steps):
                raise ValueError
        except ValueError:
            await msg.reply_text("مثل این بفرست: 1,2,3 یا 0.5,1,2")
            return
        settings()["reward_steps"] = steps
        save()
        ud.pop("state")
        await msg.reply_text("✅ ذخیره شد (روی معاملات بعدی اعمال می‌شود).", reply_markup=back_kb())
    elif st == "set_be":
        try:
            v = float(text)
            if v < 0:
                raise ValueError
        except ValueError:
            await msg.reply_text("یک عدد بفرست (0 = غیرفعال).")
            return
        settings()["be_after"] = v
        save()
        ud.pop("state")
        await msg.reply_text("✅ ذخیره شد.", reply_markup=back_kb())
    elif st == "set_poll":
        if not text.isdigit() or not 2 <= int(text) <= 60:
            await msg.reply_text("عددی بین 2 تا 60 بفرست.")
            return
        settings()["poll_seconds"] = int(text)
        save()
        ud.pop("state")
        _reschedule(context.job_queue)
        await msg.reply_text("✅ ذخیره شد.", reply_markup=back_kb())


async def handle_signal(update, context, text):
    msg = update.message
    parsed = trading.parse_signal(text)
    if isinstance(parsed, str):
        await msg.reply_text(parsed + "\n\n" + HELP, parse_mode=ParseMode.HTML)
        return
    if not channels():
        await msg.reply_text("اول یک کانال اضافه کن.", reply_markup=home_kb())
        return
    price = await trading.get_price(parsed["pair"])
    if price is None:
        await msg.reply_text(f"قیمت {esc(parsed['pair'])} از بایننس/بای‌بیت پیدا نشد؛ نماد را چک کن.")
        return
    parsed["price"] = price
    context.user_data["draft"] = parsed
    preview = trading.pending_text(parsed)
    kb = [[B(f"📤 پست در {c['title']}", f"sig:post:{c['key']}")] for c in channels()]
    kb.append([B("✖️ لغو", "sig:cancel")])
    await msg.reply_text(
        f"👀 <b>پیش‌نمایش</b>\n\n{preview}\n\n"
        f"💵 قیمت لحظه‌ای {esc(parsed['symbol'])}: <b>{fmt_price(price)}</b>",
        parse_mode=ParseMode.HTML, reply_markup=M(kb))


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


@admin_only
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    p = q.data.split(":")
    a = p[0]
    ud = context.user_data

    if a == "menu":
        ud.pop("state", None)
        await {"home": view_home, "sessions": view_sessions, "channels": view_channels,
               "trades": view_trades, "settings": view_settings, "sum": view_sum}[p[1]](update)

    elif a == "ses":
        if p[1] == "add":
            ud["state"] = "ses_name"
            await show(update, "یک نام انگلیسی برای سشن بفرست (مثلاً acc1):")
        elif p[1] == "test":
            try:
                me = await poster.whoami(p[2])
                await q.message.reply_text(f"✅ وصل است: {esc(me.first_name or '')} @{esc(me.username or '-')}",
                                           parse_mode=ParseMode.HTML)
            except Exception as ex:
                await q.message.reply_text(f"❌ {esc(str(ex))}", parse_mode=ParseMode.HTML)
        elif p[1] == "del":
            name = p[2]
            if any(c["session"] == name for c in channels()):
                await q.message.reply_text("این سشن به یک کانال وصل است؛ اول کانال را حذف کن.")
                return
            await poster.drop_client(name)
            get_manager().delete_session(name)
            pm = get_proxy_manager()
            if pm.get_proxy(f"px_{name}"):
                pm.delete_proxy(f"px_{name}")
            await view_sessions(update)

    elif a == "ch":
        if p[1] == "add":
            ud["state"] = "ch_chat"
            await show(update, "آیدی کانال را بفرست (@username یا -100...):")
        elif p[1] == "del":
            delete_channel(p[2])
            await view_channels(update)

    elif a == "chs":
        raw = ud.get("new_ch", {}).get("raw")
        if not raw:
            return
        try:
            info = await poster.verify_channel(p[1], raw)
        except Exception as ex:
            await show(update, f"❌ کانال پیدا نشد: {esc(str(ex))}\n"
                               "سشن باید عضو/ادمین کانال باشد.", back_kb())
            return
        if not info["can_post"]:
            await show(update, "❌ این سشن در کانال ادمین با دسترسی «ارسال پست» نیست.", back_kb())
            return
        ud.pop("new_ch", None)
        add_channel(info["chat"], info["title"], p[1])
        await view_channels(update)

    elif a == "sig":
        if p[1] == "cancel":
            ud.pop("draft", None)
            await show(update, "لغو شد.", back_kb())
            return
        draft = ud.pop("draft", None)
        ch = get_channel(p[2])
        if not draft or not ch:
            await show(update, "پیش‌نویس منقضی شده؛ دوباره بفرست.", back_kb())
            return
        async with LOCK:
            t = create_trade(draft, ch)
            try:
                t["pending_msg_id"] = await _post(ch, trading.pending_text(t))
                save()
            except Exception as ex:
                remove_trade(t["id"])
                await show(update, f"❌ ارسال ناموفق: {esc(str(ex))}", back_kb())
                return
        await show(update, f"✅ معامله‌ی #{t['id']} در «{esc(ch['title'])}» پست شد و زیر نظر است.", back_kb())

    elif a == "tr":
        tid = int(p[2])
        async with LOCK:
            t = get_trade(tid)
            ch = t and get_channel(t["channel"])
            if not t or not ch:
                await view_trades(update)
                return
            try:
                if p[1] == "cancel" and t["status"] == "pending":
                    await _post(ch, trading.cancel_text(t), t["pending_msg_id"])
                    t["status"] = "cancelled"
                elif p[1] == "close" and t["status"] == "open":
                    cur = await trading.get_price(t["pair"])
                    if cur is None:
                        await q.message.reply_text("قیمت در دسترس نیست.")
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

    elif a == "set":
        ud["state"] = {"rewards": "set_rewards", "be": "set_be", "poll": "set_poll"}[p[1]]
        prompts = {
            "rewards": "پله‌های ریوارد را با کاما بفرست. مثلاً <code>1,2,3</code> "
                       "(به معنی: وقتی قیمت 1R، 2R، 3R در سود رفت پست بگذار).",
            "be": "بعد از رسیدن به کدام ریوارد SL به Entry منتقل شود؟ مثلاً <code>1</code>. برای غیرفعال: <code>0</code>",
            "poll": "هر چند ثانیه قیمت چک شود؟ (2 تا 60)",
        }
        await show(update, prompts[p[1]])

    elif a == "sum":
        key = p[2]
        ch = get_channel(key)
        async with LOCK:
            rows = [t for t in trades()
                    if t["channel"] == key and t["status"] == "closed" and not t["reported"]]
            if not ch or not rows:
                await show(update, "معامله‌ی بسته‌شده‌ی گزارش‌نشده‌ای نیست.", back_kb())
                return
            try:
                await _post(ch, trading.summary_text(rows))
            except Exception as ex:
                await show(update, f"❌ {esc(str(ex))}", back_kb())
                return
            for t in rows:
                t["reported"] = True
            save()
        await show(update, f"✅ گزارش {len(rows)} معامله ارسال شد.", back_kb())


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
    active = [t for t in trades() if t["status"] in ("pending", "open")]
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


BOT_NAME = "magical_org"


async def post_init(app: Application):
    try:
        me = await app.bot.get_me()
        if me.first_name != BOT_NAME:
            await app.bot.set_my_name(BOT_NAME)
    except Exception as ex:
        log.warning("could not set bot name: %s", ex)
    _reschedule(app.job_queue)


def main():
    if not BOT_TOKEN or not ADMIN_IDS:
        raise SystemExit("BOT_TOKEN و ADMIN_IDS را در فایل .env تنظیم کن.")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "menu"], cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()