from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class CredentialVault:
    def __init__(self, key: str):
        if key:
            try:
                raw_key = key.encode("ascii")
                Fernet(raw_key)
            except (ValueError, TypeError):
                raw_key = base64.urlsafe_b64encode(hashlib.sha256(key.encode("utf-8")).digest())
        else:
            raw_key = base64.urlsafe_b64encode(hashlib.sha256(b"development-only-change-me").digest())
        self._fernet = Fernet(raw_key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("credential cannot be decrypted with the configured key") from exc
