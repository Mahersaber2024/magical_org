import asyncio
import logging
import random
import re
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from session.session_manager import get_manager
from session.proxy_manager import get_proxy_manager

logger = logging.getLogger(__name__)

_clients = {}
_pending = {}
_locks = {}


def _path(name: str) -> str:
    return str(Path(get_manager().session_dir) / name)


def _proxy(proxy_name: str):
    return get_proxy_manager().get_telethon_proxy(proxy_name) if proxy_name else None


async def get_client(name: str) -> TelegramClient:
    lock = _locks.setdefault(name, asyncio.Lock())
    async with lock:
        c = _clients.get(name)
        if c and c.is_connected():
            return c
        info = get_manager().get_session(name)
        if not info:
            raise RuntimeError(f"Session '{name}' not found")
        c = TelegramClient(_path(name), int(info["api_id"]), info["api_hash"],
                           proxy=_proxy(info.get("proxy", "")))
        await c.connect()
        if not await c.is_user_authorized():
            await c.disconnect()
            raise RuntimeError(f"Session '{name}' is not authorized anymore")
        _clients[name] = c
        return c


async def drop_client(name: str):
    c = _clients.pop(name, None)
    if c:
        try:
            await c.disconnect()
        except Exception:
            pass


async def login_start(name, api_id, api_hash, phone, proxy_name=""):
    c = TelegramClient(_path(name), int(api_id), api_hash, proxy=_proxy(proxy_name))
    await c.connect()
    try:
        sent = await c.send_code_request(phone)
    except Exception:
        await c.disconnect()
        raise
    _pending[name] = {"client": c, "phone": phone, "hash": sent.phone_code_hash}


async def login_code(name, code):
    p = _pending[name]
    try:
        await p["client"].sign_in(phone=p["phone"], code=code, phone_code_hash=p["hash"])
    except SessionPasswordNeededError:
        return "password"
    return "ok"


async def login_password(name, password):
    await _pending[name]["client"].sign_in(password=password)


async def login_finish(name):
    p = _pending.pop(name)
    me = await p["client"].get_me()
    _clients[name] = p["client"]
    return me


async def login_cancel(name):
    p = _pending.pop(name, None)
    if p:
        try:
            await p["client"].disconnect()
        except Exception:
            pass
    for suffix in (".session", ".session-journal"):
        f = Path(_path(name) + suffix)
        if f.exists():
            f.unlink()


async def _resolve(client, chat):
    try:
        return await client.get_entity(chat)
    except ValueError:
        await client.get_dialogs()
        return await client.get_entity(chat)


def _norm_chat(raw: str):
    raw = raw.strip()
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    m = re.search(r"t\.me/([A-Za-z0-9_]+)", raw)
    name = m.group(1) if m else raw.lstrip("@")
    return "@" + name


async def verify_channel(session: str, raw: str) -> dict:
    c = await get_client(session)
    chat = _norm_chat(raw)
    ent = await _resolve(c, chat)
    perms = await c.get_permissions(ent, "me")
    ok = bool(perms.is_creator or (perms.is_admin and getattr(perms, "post_messages", False)))
    return {"chat": chat, "title": getattr(ent, "title", str(chat)), "can_post": ok}


async def send(session: str, chat, text: str, reply_to=None) -> int:
    c = await get_client(session)
    ent = await _resolve(c, chat)
    await asyncio.sleep(random.uniform(1.0, 3.0))
    m = await c.send_message(ent, text, parse_mode="html", reply_to=reply_to, link_preview=False)
    return m.id


async def whoami(session: str):
    c = await get_client(session)
    return await c.get_me()
