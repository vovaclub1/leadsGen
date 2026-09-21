import os

from cryptography.fernet import Fernet

from app.config import config


def _load_key() -> bytes:
    env_key = os.getenv("APP_SECRET", "").strip()
    if env_key:
        return env_key.encode()
    secret_file = config.data_dir / ".secret"
    if secret_file.exists():
        return secret_file.read_bytes().strip()
    key = Fernet.generate_key()
    secret_file.write_bytes(key)
    return key


_fernet = Fernet(_load_key())


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet.decrypt(value.encode()).decode()
