# session/session_manager.py
# NO COMMENTS: every comment and docstring was intentionally removed from this file. Do not add any comments or docstrings in future edits.

import os
import json
import glob
import logging
import shutil
import stat
from typing import List, Dict, Optional
from pathlib import Path
from datetime import datetime

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


TASK_CHOICES = [
    ("extract", "Extract Members"),
    ("monitor", "Monitor Groups"),
    ("offline_adder", "Offline Adder"),
    ("scan_bots", "Scan Bots"),
    ("msender", "Message Sender"),
    ("engagement", "Post Engagement"),
    ("psync", "Private Users"),
    ("send_drafts", "Send Drafts"),
    ("blkus", "Blocked Check"),
    ("ai_chat", "AI Group Chat"),
    ("channel_post", "Channel Poster"),
]
TASK_LABELS = dict(TASK_CHOICES)


def session_display_name(s: Dict) -> str:
    username = (s.get('account_username') or '').strip()
    first_name = (s.get('account_first_name') or '').strip()
    phone = (s.get('phone') or '').strip()
    who = f"@{username}" if username else first_name
    if who and phone:
        return f"{who} · {phone}"
    if who:
        return who
    if phone:
        return phone
    return s.get('name', 'Unknown')


class SecureSessionManager:
    def __init__(self, session_dir: str = 'session', **kwargs):

        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(exist_ok=True)
        self.sessions_file = self.session_dir / 'sessions.encrypted'
        self.key_file = self.session_dir / '.session_key'
        self.sessions = {}
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
        if self.sessions_file.exists():
            self._secure_file(self.sessions_file)
        if self.key_file.exists():
            self._secure_file(self.key_file)
        for file in self.session_dir.glob("*.session"):
            self._secure_file(file)


    def _load_or_create_key(self):
        if self.key_file.exists():
            try:
                with open(self.key_file, 'rb') as f:
                    key = f.read()
                self.cipher = Fernet(key)
                self._secure_file(self.key_file)
                return
            except Exception as e:
                logger.error(f"Error loading key, generating a new one: {e}")
        self._create_new_key()

    def _create_new_key(self):
        key = Fernet.generate_key()
        with open(self.key_file, 'wb') as f:
            f.write(key)
        self._secure_file(self.key_file)
        self.cipher = Fernet(key)
        logger.info("✅ Session encryption key generated.")


    def _load_config(self):
        if self.sessions_file.exists():
            try:
                with open(self.sessions_file, 'r') as f:
                    encrypted_data = f.read()
                decrypted = self.cipher.decrypt(encrypted_data.encode())
                self.sessions = json.loads(decrypted.decode())
                self._secure_file(self.sessions_file)
            except Exception as e:
                logger.error(f"Error loading sessions: {e}")
                self.sessions = {}
        else:
            self.sessions = {}

    def save_config(self):
        try:
            config_to_save = {}
            for name, data in self.sessions.items():
                config_to_save[name] = {
                    'phone': data.get('phone', ''),
                    'api_id': data.get('api_id', ''),
                    'api_hash': data.get('api_hash', ''),
                    'created_at': data.get('created_at', ''),
                    'last_used': data.get('last_used', ''),
                    'proxy': data.get('proxy', ''),
                    'purpose': data.get('purpose', []),
                    'blocked_ok': data.get('blocked_ok', []),

                    'owner_user_id': data.get('owner_user_id', 0),
                    'owner_username': data.get('owner_username', ''),
                    'country_name': data.get('country_name', ''),
                    'country_code': data.get('country_code', ''),
                    'country_flag': data.get('country_flag', ''),
                    'account_first_name': data.get('account_first_name', ''),
                    'account_username': data.get('account_username', ''),
                }
            json_str = json.dumps(config_to_save, indent=2, ensure_ascii=False)
            encrypted = self.cipher.encrypt(json_str.encode())
            with open(self.sessions_file, 'w') as f:
                f.write(encrypted.decode())
            self._secure_file(self.sessions_file)
            logger.info(f"✅ Sessions encrypted and saved to {self.sessions_file}")
        except Exception as e:
            logger.error(f"Error saving sessions: {e}")


    def list_sessions(self) -> List[Dict]:
        sessions = []
        session_files = glob.glob(str(self.session_dir / "*.session"))
        for file in session_files:
            session_name = Path(file).stem
            is_valid = os.path.getsize(file) > 0
            sessions.append({
                'name': session_name,
                'file': file,
                'is_valid': is_valid,
                'phone': self.sessions.get(session_name, {}).get('phone', 'Unknown'),
                'api_id': self.sessions.get(session_name, {}).get('api_id', ''),
                'api_hash': self.sessions.get(session_name, {}).get('api_hash', ''),
                'created_at': self.sessions.get(session_name, {}).get('created_at', ''),
                'last_used': self.sessions.get(session_name, {}).get('last_used', ''),
                'proxy': self.sessions.get(session_name, {}).get('proxy', ''),
                'purpose': self.sessions.get(session_name, {}).get('purpose', []),
                'blocked_ok': self.sessions.get(session_name, {}).get('blocked_ok', []),
                'owner_user_id': self.sessions.get(session_name, {}).get('owner_user_id', 0),
                'owner_username': self.sessions.get(session_name, {}).get('owner_username', ''),
                'country_name': self.sessions.get(session_name, {}).get('country_name', ''),
                'country_code': self.sessions.get(session_name, {}).get('country_code', ''),
                'country_flag': self.sessions.get(session_name, {}).get('country_flag', ''),
                'account_first_name': self.sessions.get(session_name, {}).get('account_first_name', ''),
                'account_username': self.sessions.get(session_name, {}).get('account_username', ''),
            })
        return sessions

    def add_session(self, name: str, phone: str, api_id: int, api_hash: str, proxy: str = '') -> Dict:
        if name in self.sessions:
            return {'success': False, 'message': f"Session '{name}' already exists"}
        self.sessions[name] = {
            'phone': phone,
            'api_id': api_id,
            'api_hash': api_hash,
            'created_at': datetime.now().isoformat(),
            'last_used': datetime.now().isoformat(),
            'proxy': proxy or '',
            'purpose': [],
            'blocked_ok': [],
            'owner_user_id': 0,
            'owner_username': '',
            'country_name': '',
            'country_code': '',
            'country_flag': '',
            'account_first_name': '',
            'account_username': '',
        }
        self.save_config()
        return {'success': True, 'message': f"Session '{name}' added successfully"}

    def set_session_identity(self, name: str, first_name: str = '', username: str = '') -> Dict:
        if name not in self.sessions:
            return {'success': False, 'message': f"Session '{name}' not found"}
        self.sessions[name]['account_first_name'] = first_name or ''
        self.sessions[name]['account_username'] = username or ''
        self.save_config()
        return {'success': True, 'message': 'Identity set'}


    def is_phone_registered(self, phone: str) -> bool:
        target = "".join(ch for ch in phone if ch.isdigit())
        if not target:
            return False
        for data in self.sessions.values():
            existing = "".join(ch for ch in str(data.get('phone', '')) if ch.isdigit())
            if existing and existing == target:
                return True
        return False

    def set_session_owner(self, name: str, owner_user_id: int, owner_username: str = '') -> Dict:
        if name not in self.sessions:
            return {'success': False, 'message': f"Session '{name}' not found"}
        self.sessions[name]['owner_user_id'] = owner_user_id
        self.sessions[name]['owner_username'] = owner_username or ''
        self.save_config()
        return {'success': True, 'message': 'Owner set'}

    def set_session_country(self, name: str, country_name: str, country_code: str, country_flag: str) -> Dict:
        if name not in self.sessions:
            return {'success': False, 'message': f"Session '{name}' not found"}
        self.sessions[name]['country_name'] = country_name or ''
        self.sessions[name]['country_code'] = country_code or ''
        self.sessions[name]['country_flag'] = country_flag or ''
        self.save_config()
        return {'success': True, 'message': 'Country set'}

    def list_sessions_by_owner(self, owner_user_id: int) -> List[Dict]:
        return [s for s in self.list_sessions() if s.get('owner_user_id') == owner_user_id]

    def delete_session_if_owned(self, name: str, owner_user_id: int) -> Dict:
        info = self.sessions.get(name)
        if not info or info.get('owner_user_id') != owner_user_id or not owner_user_id:
            return {'success': False, 'message': "Session not found or not owned by you"}
        return self.delete_session(name)

    def set_session_purpose(self, name: str, purpose: List[str]) -> Dict:
        if name not in self.sessions:
            return {'success': False, 'message': f"Session '{name}' not found"}
        valid_keys = {k for k, _ in TASK_CHOICES}
        cleaned = [p for p in purpose if p in valid_keys]
        self.sessions[name]['purpose'] = cleaned
        self.save_config()
        if cleaned:
            labels = ", ".join(TASK_LABELS.get(k, k) for k in cleaned)
            return {'success': True, 'message': f"Session '{name}' purpose set to: {labels}", 'purpose': cleaned}
        return {'success': True, 'message': f"Session '{name}' is not usable for any task yet", 'purpose': cleaned}

    def toggle_session_purpose(self, name: str, task_key: str) -> Dict:
        if name not in self.sessions:
            return {'success': False, 'message': f"Session '{name}' not found"}
        current = list(self.sessions[name].get('purpose', []))
        if task_key in current:
            current.remove(task_key)
        else:
            current.append(task_key)
        self.sessions[name]['purpose'] = current
        self.save_config()
        return {'success': True, 'message': 'Purpose updated', 'purpose': current}

    def toggle_session_blocked_ok(self, name: str, task_key: str) -> Dict:
        if name not in self.sessions:
            return {'success': False, 'message': f"Session '{name}' not found"}
        current = list(self.sessions[name].get('blocked_ok', []))
        if task_key in current:
            current.remove(task_key)
        else:
            current.append(task_key)
        self.sessions[name]['blocked_ok'] = current
        self.save_config()
        return {'success': True, 'message': 'Updated', 'blocked_ok': current}

    def set_session_proxy(self, name: str, proxy: str) -> Dict:
        if name not in self.sessions:
            return {'success': False, 'message': f"Session '{name}' not found"}
        self.sessions[name]['proxy'] = proxy or ''
        self.save_config()
        if proxy:
            return {'success': True, 'message': f"Session '{name}' will now use proxy '{proxy}'"}
        return {'success': True, 'message': f"Session '{name}' no longer uses a proxy"}

    def update_session_usage(self, name: str):
        if name in self.sessions:
            self.sessions[name]['last_used'] = datetime.now().isoformat()
            self.save_config()

    def rename_session(self, old_name: str, new_name: str) -> Dict:
        if old_name not in self.sessions:
            return {'success': False, 'message': f"Session '{old_name}' not found"}
        if new_name in self.sessions:
            return {'success': False, 'message': f"Session '{new_name}' already exists"}
        old_file = self.session_dir / f"{old_name}.session"
        new_file = self.session_dir / f"{new_name}.session"
        if old_file.exists():
            try:
                shutil.move(old_file, new_file)
                self._secure_file(new_file)
            except Exception as e:
                return {'success': False, 'message': f"Failed to rename session file: {e}"}
        self.sessions[new_name] = self.sessions.pop(old_name)
        self.save_config()
        return {'success': True, 'message': f"Session renamed from '{old_name}' to '{new_name}'"}

    def delete_session(self, name: str) -> Dict:

        session_file = self.session_dir / f"{name}.session"
        if name not in self.sessions and not session_file.exists():
            return {'success': False, 'message': f"Session '{name}' not found"}
        if session_file.exists():
            try:
                os.remove(session_file)
            except Exception as e:
                logger.warning(f"Could not delete session file: {e}")
        journal_file = self.session_dir / f"{name}.session-journal"
        if journal_file.exists():
            try:
                os.remove(journal_file)
            except Exception as e:
                logger.warning(f"Could not delete session journal file: {e}")
        self.sessions.pop(name, None)
        self.save_config()
        return {'success': True, 'message': f"Session '{name}' deleted"}

    def get_session(self, name: str) -> Optional[Dict]:
        if name not in self.sessions:
            return None
        session_info = self.sessions[name].copy()
        session_info['session_file'] = f"{name}.session"
        session_info.setdefault('purpose', [])
        session_info.setdefault('blocked_ok', [])
        return session_info


_manager: Optional[SecureSessionManager] = None


def get_manager(session_dir: str = "session") -> SecureSessionManager:
    global _manager
    if _manager is None:
        _manager = SecureSessionManager(session_dir=session_dir)
    return _manager
