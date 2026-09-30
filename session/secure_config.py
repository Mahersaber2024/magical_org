# session/secure_config.py
# NO COMMENTS: every comment and docstring was intentionally removed from this file. Do not add any comments or docstrings in future edits.


import os
import json
import logging
import stat
from pathlib import Path

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


class SecureConfig:

    def __init__(self, config_file: str = 'session/config.encrypted', key_file: str = 'session/.config_key'):
        self.config_file = Path(config_file)
        self.key_file = Path(key_file)
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.fernet = None
        self._load_or_create_key()

    def _secure_file(self, path: Path):
        try:
            if path.exists():
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except Exception as e:
            logger.warning(f"Could not secure {path}: {e}")

    def _load_or_create_key(self):
        if self.key_file.exists():
            try:
                with open(self.key_file, 'rb') as f:
                    key = f.read()
                self.fernet = Fernet(key)
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
        self.fernet = Fernet(key)
        logger.info("✅ Config encryption key generated.")

    def encrypt_data(self, data: dict) -> str:
        json_str = json.dumps(data)
        encrypted = self.fernet.encrypt(json_str.encode())
        return encrypted.decode()

    def decrypt_data(self, encrypted_data: str) -> dict:
        decrypted = self.fernet.decrypt(encrypted_data.encode())
        return json.loads(decrypted.decode())

    def save_config(self, data: dict):
        encrypted = self.encrypt_data(data)
        with open(self.config_file, 'w') as f:
            f.write(encrypted)
        self._secure_file(self.config_file)

    def load_config(self) -> dict:
        if not self.config_file.exists():
            return {}
        try:
            with open(self.config_file, 'r') as f:
                encrypted = f.read()
            return self.decrypt_data(encrypted)
        except Exception as e:
            logger.error(f"❌ Error decrypting config: {e}")
            return {}
