"""Secret storage primitive: OS keyring when available, an explicit,
file-permission-restricted encrypted fallback otherwise (Section 16:
"Store refresh tokens and API keys in the OS keyring. If unavailable,
use an encrypted secrets file with restrictive permissions (0600) and a
key supplied separately at runtime").

Nothing here ever returns or logs a secret value except through
`get_secret`'s return value, and nothing here ever writes a secret to
SQLite — callers store only the opaque `credential_ref` this module
returns from `set_secret`. The ref's own text encodes which backend
holds it (`keyring:<id>` or `encrypted_file:<id>`), so `get_secret`/
`delete_secret` never need a side index and can never guess wrong.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

from project_context.ids import new_id

_SERVICE_NAME = "project-context"
_MASTER_KEY_FILENAME = "master.key"
_ENCRYPTED_STORE_FILENAME = "secrets.enc"
_OWNER_READ_WRITE = stat.S_IRUSR | stat.S_IWUSR  # 0o600
_OWNER_ALL = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR  # 0o700


class CredentialBackend(StrEnum):
    """Which physical store a `credential_ref` resolves through."""

    KEYRING = "keyring"
    ENCRYPTED_FILE = "encrypted_file"


class CredentialStoreError(RuntimeError):
    """Raised when a secret cannot be stored or read by *either* backend
    — never raised merely because keyring is unavailable (that is the
    expected, silently-handled fallback trigger)."""


class KeyringBackend(Protocol):
    """The three `keyring` module-level functions this store calls —
    typed so a test can inject a fake without monkeypatching the real
    `keyring` package. The real `keyring` module satisfies this
    Protocol structurally (it is the default backend)."""

    def set_password(self, service_name: str, username: str, password: str) -> None: ...
    def get_password(self, service_name: str, username: str) -> str | None: ...
    def delete_password(self, service_name: str, username: str) -> None: ...


def _default_keyring_backend() -> KeyringBackend:
    import keyring

    return keyring


@dataclass(frozen=True)
class _CredentialRef:
    backend: CredentialBackend
    local_id: str

    def __str__(self) -> str:
        return f"{self.backend.value}:{self.local_id}"

    @classmethod
    def parse(cls, credential_ref: str) -> _CredentialRef:
        backend_value, _, local_id = credential_ref.partition(":")
        if not local_id:
            raise CredentialStoreError(f"malformed credential_ref: {credential_ref!r}")
        return cls(backend=CredentialBackend(backend_value), local_id=local_id)


class CredentialStore:
    """Set/get/delete secrets by opaque `credential_ref`. Tries the OS
    keyring first (unless `prefer_keyring=False`); any keyring failure —
    missing backend, locked collection, denied access — falls back to
    the encrypted file store rather than raising, so a headless or
    freshly-imaged machine still works (never a silent plaintext
    fallback: the only two backends are keyring and Fernet-encrypted).
    """

    def __init__(
        self,
        *,
        credentials_dir: Path,
        prefer_keyring: bool = True,
        keyring_backend: KeyringBackend | None = None,
    ) -> None:
        self._credentials_dir = credentials_dir
        self._prefer_keyring = prefer_keyring
        self._keyring_backend = keyring_backend

    def _keyring(self) -> KeyringBackend:
        if self._keyring_backend is not None:
            return self._keyring_backend
        return _default_keyring_backend()

    # --- public API -------------------------------------------------------

    def set_secret(self, secret: str) -> str:
        """Store `secret` under a freshly generated local ID and return
        the `credential_ref` to persist in SQLite."""
        local_id = new_id()
        if self._prefer_keyring:
            try:
                self._keyring().set_password(_SERVICE_NAME, local_id, secret)
                return str(_CredentialRef(CredentialBackend.KEYRING, local_id))
            except Exception:  # noqa: BLE001 - any keyring failure means "fall back"
                pass
        self._set_encrypted(local_id, secret)
        return str(_CredentialRef(CredentialBackend.ENCRYPTED_FILE, local_id))

    def update_secret(self, credential_ref: str, new_secret: str) -> None:
        """Overwrite the value at an *existing* ref in place — used for
        token refresh, where the SQLite `credential_ref` a `sources` row
        points at must not change every time a token rotates. Raises
        `CredentialStoreError` if the ref's backend was keyring but the
        keyring write now fails (refresh must not silently downgrade a
        credential from keyring to plaintext-adjacent encrypted-file
        storage without the caller knowing)."""
        ref = _CredentialRef.parse(credential_ref)
        if ref.backend is CredentialBackend.KEYRING:
            try:
                self._keyring().set_password(_SERVICE_NAME, ref.local_id, new_secret)
            except Exception as exc:  # noqa: BLE001 - reported, not silently downgraded
                raise CredentialStoreError("failed to update credential in the OS keyring") from exc
            return
        self._set_encrypted(ref.local_id, new_secret)

    def get_secret(self, credential_ref: str) -> str | None:
        ref = _CredentialRef.parse(credential_ref)
        if ref.backend is CredentialBackend.KEYRING:
            try:
                return self._keyring().get_password(_SERVICE_NAME, ref.local_id)
            except Exception:  # noqa: BLE001 - a keyring read failure is "not found", not a crash
                return None
        return self._get_encrypted(ref.local_id)

    def delete_secret(self, credential_ref: str) -> None:
        """Idempotent — deleting an already-absent ref is not an error
        (Section 16: "Disconnect deletes local credential material")."""
        ref = _CredentialRef.parse(credential_ref)
        if ref.backend is CredentialBackend.KEYRING:
            with contextlib.suppress(Exception):  # already gone, or backend unavailable: fine
                self._keyring().delete_password(_SERVICE_NAME, ref.local_id)
            return
        self._delete_encrypted(ref.local_id)

    def backend_for(self, credential_ref: str) -> CredentialBackend:
        """Exposed for tests and diagnostics — never required for normal
        get/set/delete, which route on the ref's own prefix."""
        return _CredentialRef.parse(credential_ref).backend

    # --- encrypted-file fallback --------------------------------------

    def _ensure_credentials_dir(self) -> None:
        self._credentials_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._credentials_dir, _OWNER_ALL)

    def _master_key_path(self) -> Path:
        return self._credentials_dir / _MASTER_KEY_FILENAME

    def _store_path(self) -> Path:
        return self._credentials_dir / _ENCRYPTED_STORE_FILENAME

    def _get_or_create_master_key(self) -> bytes:
        """The Fernet key lives in its own file, separate from the
        encrypted secrets file it decrypts — "a key supplied separately
        at runtime," not embedded alongside what it protects."""
        self._ensure_credentials_dir()
        key_path = self._master_key_path()
        if key_path.exists():
            return key_path.read_bytes()
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        os.chmod(key_path, _OWNER_READ_WRITE)
        return key

    def _load_encrypted_store(self) -> dict[str, str]:
        store_path = self._store_path()
        if not store_path.exists():
            return {}
        fernet = Fernet(self._get_or_create_master_key())
        try:
            plaintext = fernet.decrypt(store_path.read_bytes())
        except InvalidToken as exc:
            raise CredentialStoreError(
                "encrypted credential store could not be decrypted with the current master "
                "key — the key file may have been lost or replaced"
            ) from exc
        return json.loads(plaintext.decode("utf-8"))

    def _save_encrypted_store(self, data: dict[str, str]) -> None:
        self._ensure_credentials_dir()
        fernet = Fernet(self._get_or_create_master_key())
        ciphertext = fernet.encrypt(json.dumps(data).encode("utf-8"))
        store_path = self._store_path()
        store_path.write_bytes(ciphertext)
        os.chmod(store_path, _OWNER_READ_WRITE)

    def _set_encrypted(self, local_id: str, secret: str) -> None:
        data = self._load_encrypted_store()
        data[local_id] = secret
        self._save_encrypted_store(data)

    def _get_encrypted(self, local_id: str) -> str | None:
        return self._load_encrypted_store().get(local_id)

    def _delete_encrypted(self, local_id: str) -> None:
        data = self._load_encrypted_store()
        if local_id in data:
            del data[local_id]
            self._save_encrypted_store(data)
