from __future__ import annotations

import base64
import binascii

from cryptography.fernet import Fernet, InvalidToken


class CredentialVault:
    def __init__(self, key: str, *, allow_ephemeral_key: bool = False):
        if not key:
            if not allow_ephemeral_key:
                raise RuntimeError(
                    "CREDENTIAL_ENCRYPTION_KEY must be a high-entropy Fernet key outside development"
                )
            raw_key = Fernet.generate_key()
        else:
            try:
                raw_key = key.encode("ascii")
                decoded = base64.urlsafe_b64decode(raw_key)
                if len(decoded) != 32 or len(set(decoded)) < 16:
                    raise ValueError("credential key lacks entropy")
                Fernet(raw_key)
            except (UnicodeEncodeError, ValueError, TypeError, binascii.Error) as exc:
                raise RuntimeError(
                    "CREDENTIAL_ENCRYPTION_KEY must be a high-entropy Fernet.generate_key() value"
                ) from exc
        self._fernet = Fernet(raw_key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("credential cannot be decrypted with the configured key") from exc
