# session/proxy_manager.py
# NO COMMENTS: every comment and docstring was intentionally removed from this file. Do not add any comments or docstrings in future edits.

import os
import json
import time
import socket
import asyncio
import logging
import stat
from typing import List, Dict, Optional
from pathlib import Path

from cryptography.fernet import Fernet
import socks

logger = logging.getLogger(__name__)

_PROXY_TYPE_MAP = {
    "socks5": 2,
    "socks4": 1,
    "http": 3,
}


class ProxyManager:

    def __init__(self, session_dir: str = "session", **kwargs):
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(exist_ok=True)
        self.proxies_file = self.session_dir / "proxies.encrypted"
        self.key_file = self.session_dir / ".proxy_key"
        self.proxies: Dict[str, Dict] = {}
        self.cipher = None
        self._load_or_create_key()
        self._load_config()
        self._secure_all_files()


    def _secure_file(self, file_path: Path):
        try:
            if file_path.exists():
                os.chmod(file_path, stat.S_IRUSR | stat.S_IWUSR)
        except Exception as e:
            logger.warning(f"Could not secure {file_path}: {e}")

    def _secure_all_files(self):
        if self.proxies_file.exists():
            self._secure_file(self.proxies_file)
        if self.key_file.exists():
            self._secure_file(self.key_file)


    def _load_or_create_key(self):
        if self.key_file.exists():
            try:
                with open(self.key_file, "rb") as f:
                    key = f.read()
                self.cipher = Fernet(key)
                self._secure_file(self.key_file)
                return
            except Exception as e:
                logger.error(f"Error loading proxy key, generating a new one: {e}")
        self._create_new_key()

    def _create_new_key(self):
        key = Fernet.generate_key()
        with open(self.key_file, "wb") as f:
            f.write(key)
        self._secure_file(self.key_file)
        self.cipher = Fernet(key)
        logger.info("✅ Proxy encryption key generated.")


    def _load_config(self):
        if self.proxies_file.exists():
            try:
                with open(self.proxies_file, "r") as f:
                    encrypted_data = f.read()
                decrypted = self.cipher.decrypt(encrypted_data.encode())
                self.proxies = json.loads(decrypted.decode())
                self._secure_file(self.proxies_file)
            except Exception as e:
                logger.error(f"Error loading proxies: {e}")
                self.proxies = {}
        else:
            self.proxies = {}

    def _save_config(self):
        try:
            json_str = json.dumps(self.proxies, indent=2, ensure_ascii=False)
            encrypted = self.cipher.encrypt(json_str.encode())
            with open(self.proxies_file, "w") as f:
                f.write(encrypted.decode())
            self._secure_file(self.proxies_file)
        except Exception as e:
            logger.error(f"Error saving proxies: {e}")


    def list_proxies(self) -> List[Dict]:
        result = []
        for name, data in self.proxies.items():
            entry = {"name": name}
            entry.update(data)


            entry.setdefault("enabled", True)
            entry.setdefault("visible", True)
            result.append(entry)
        return sorted(result, key=lambda p: p["name"].lower())

    def get_proxy(self, name: str) -> Optional[Dict]:
        if name not in self.proxies:
            return None
        info = self.proxies[name].copy()
        info["name"] = name
        info.setdefault("enabled", True)
        info.setdefault("visible", True)
        return info

    def is_enabled(self, name: str) -> bool:
        info = self.proxies.get(name)
        if not info:
            return False
        return info.get("enabled", True)

    def set_enabled(self, name: str, enabled: bool) -> Dict:
        if name not in self.proxies:
            return {"success": False, "message": f"Proxy '{name}' not found"}
        self.proxies[name]["enabled"] = enabled
        self._save_config()
        return {"success": True, "message": f"Proxy '{name}' {'enabled' if enabled else 'disabled'}"}

    def is_visible(self, name: str) -> bool:
        info = self.proxies.get(name)
        if not info:
            return False
        return info.get("visible", True)

    def set_visible(self, name: str, visible: bool) -> Dict:
        if name not in self.proxies:
            return {"success": False, "message": f"Proxy '{name}' not found"}
        self.proxies[name]["visible"] = visible
        self._save_config()
        return {"success": True,
                "message": f"Proxy '{name}' is now "
                           f"{'visible to users' if visible else 'hidden from users'}"}

    def add_proxy(self, name: str, type_: str, host: str, port: int,
                   username: str = "", password: str = "") -> Dict:
        if not name:
            return {"success": False, "message": "Proxy name cannot be empty"}
        if name in self.proxies:
            return {"success": False, "message": f"Proxy '{name}' already exists"}
        if type_ not in _PROXY_TYPE_MAP:
            return {"success": False, "message": f"Unknown proxy type '{type_}'"}
        self.proxies[name] = {
            "type": type_,
            "host": host,
            "port": int(port),
            "username": username or "",
            "password": password or "",
            "enabled": True,
        }
        self._save_config()
        return {"success": True, "message": f"Proxy '{name}' added successfully"}

    def delete_proxy(self, name: str) -> Dict:
        if name not in self.proxies:
            return {"success": False, "message": f"Proxy '{name}' not found"}
        del self.proxies[name]
        self._save_config()
        return {"success": True, "message": f"Proxy '{name}' deleted"}

    def is_in_use(self, name: str, session_manager=None) -> List[str]:
        if session_manager is None:
            return []
        return [
            s["name"] for s in session_manager.list_sessions()
            if s.get("proxy") == name
        ]


    def get_telethon_proxy(self, name: str):
        if not name:
            return None
        info = self.get_proxy(name)
        if not info:
            logger.warning(f"Proxy '{name}' not found - connecting directly instead")
            return None
        proxy_type = _PROXY_TYPE_MAP.get(info["type"])
        if proxy_type is None:
            logger.warning(f"Unknown proxy type '{info['type']}' for '{name}' - connecting directly")
            return None
        return (
            proxy_type,
            info["host"],
            int(info["port"]),
            True,
            info.get("username") or None,
            info.get("password") or None,
        )


    def test_proxy(self, name: str, timeout: float = 10.0,
                    target_host: str = "149.154.167.50", target_port: int = 443) -> Dict:
        info = self.get_proxy(name)
        if not info:
            return {"success": False, "message": f"Proxy '{name}' not found"}

        proxy_type = _PROXY_TYPE_MAP.get(info["type"])
        if proxy_type is None:
            return {"success": False, "message": f"Unknown proxy type '{info['type']}'"}

        sock = socks.socksocket()
        sock.set_proxy(
            proxy_type,
            info["host"],
            int(info["port"]),
            rdns=True,
            username=info.get("username") or None,
            password=info.get("password") or None,
        )
        sock.settimeout(timeout)

        start = time.monotonic()
        try:
            sock.connect((target_host, target_port))
            elapsed_ms = (time.monotonic() - start) * 1000
            return {
                "success": True,
                "message": f"✅ Proxy is alive - connected in {elapsed_ms:.0f} ms",
                "latency_ms": round(elapsed_ms, 1),
            }
        except socket.timeout:
            return {
                "success": False,
                "message": f"⏱ Timed out after {timeout:.0f}s trying to connect through the proxy",
            }
        except (socks.ProxyConnectionError, ConnectionRefusedError) as e:
            return {"success": False, "message": f"✕ Could not reach the proxy server: {e}"}
        except socks.GeneralProxyError as e:
            return {"success": False, "message": f"✕ Proxy rejected the connection (bad auth/type?): {e}"}
        except Exception as e:
            return {"success": False, "message": f"✕ Proxy test failed: {e}"}
        finally:
            try:
                sock.close()
            except Exception:
                pass

    async def test_proxy_async(self, name: str, timeout: float = 10.0) -> Dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self.test_proxy(name, timeout))


_proxy_manager: Optional[ProxyManager] = None


def get_proxy_manager(session_dir: str = "session") -> ProxyManager:
    global _proxy_manager
    if _proxy_manager is None:
        _proxy_manager = ProxyManager(session_dir=session_dir)
    return _proxy_manager
