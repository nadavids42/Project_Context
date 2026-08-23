"""Tests for `project_context.credentials.store.CredentialStore`: OS
keyring first, encrypted-file fallback second, restrictive permissions,
never plaintext (Section 16; FR-032; Prompt 10)."""

from __future__ import annotations

import stat

import pytest
from cryptography.fernet import Fernet

from project_context.credentials.store import (
    CredentialBackend,
    CredentialStore,
    CredentialStoreError,
)


class FakeKeyring:
    """An in-memory stand-in for the `keyring` module's three functions,
    with switches to simulate every failure mode a real OS keyring can
    hit (locked collection, no backend, denied access)."""

    def __init__(
        self, *, fail_set: bool = False, fail_get: bool = False, fail_delete: bool = False
    ):
        self._data: dict[tuple[str, str], str] = {}
        self.fail_set = fail_set
        self.fail_get = fail_get
        self.fail_delete = fail_delete
        self.set_calls: list[tuple[str, str, str]] = []

    def set_password(self, service_name: str, username: str, password: str) -> None:
        if self.fail_set:
            raise RuntimeError("keyring backend unavailable")
        self.set_calls.append((service_name, username, password))
        self._data[(service_name, username)] = password

    def get_password(self, service_name: str, username: str) -> str | None:
        if self.fail_get:
            raise RuntimeError("keyring locked")
        return self._data.get((service_name, username))

    def delete_password(self, service_name: str, username: str) -> None:
        if self.fail_delete:
            raise RuntimeError("keyring locked")
        self._data.pop((service_name, username), None)


@pytest.fixture
def credentials_dir(tmp_path):
    return tmp_path / "credentials"


def test_set_and_get_round_trip_through_a_working_keyring(credentials_dir):
    fake = FakeKeyring()
    store = CredentialStore(credentials_dir=credentials_dir, keyring_backend=fake)
    ref = store.set_secret("super-secret-token")
    assert store.backend_for(ref) is CredentialBackend.KEYRING
    assert store.get_secret(ref) == "super-secret-token"
    assert fake.set_calls  # actually went through the keyring, not the file fallback


def test_delete_secret_removes_it_from_keyring(credentials_dir):
    fake = FakeKeyring()
    store = CredentialStore(credentials_dir=credentials_dir, keyring_backend=fake)
    ref = store.set_secret("token")
    store.delete_secret(ref)
    assert store.get_secret(ref) is None


def test_keyring_set_failure_falls_back_to_encrypted_file(credentials_dir):
    fake = FakeKeyring(fail_set=True)
    store = CredentialStore(credentials_dir=credentials_dir, keyring_backend=fake)
    ref = store.set_secret("super-secret-token")
    assert store.backend_for(ref) is CredentialBackend.ENCRYPTED_FILE
    assert store.get_secret(ref) == "super-secret-token"


def test_prefer_keyring_false_always_uses_encrypted_file(credentials_dir):
    fake = FakeKeyring()  # would happily succeed, but must not be used
    store = CredentialStore(
        credentials_dir=credentials_dir, prefer_keyring=False, keyring_backend=fake
    )
    ref = store.set_secret("token")
    assert store.backend_for(ref) is CredentialBackend.ENCRYPTED_FILE
    assert fake.set_calls == []


def test_keyring_get_failure_returns_none_rather_than_raising(credentials_dir):
    fake = FakeKeyring()
    store = CredentialStore(credentials_dir=credentials_dir, keyring_backend=fake)
    ref = store.set_secret("token")
    fake.fail_get = True
    assert store.get_secret(ref) is None


def test_keyring_delete_failure_does_not_raise(credentials_dir):
    fake = FakeKeyring(fail_delete=True)
    store = CredentialStore(credentials_dir=credentials_dir, keyring_backend=fake)
    ref = store.set_secret("token")
    store.delete_secret(ref)  # must not raise


def test_get_secret_for_unknown_encrypted_ref_returns_none(credentials_dir):
    store = CredentialStore(credentials_dir=credentials_dir, prefer_keyring=False)
    fabricated_ref = f"{CredentialBackend.ENCRYPTED_FILE.value}:does-not-exist"
    assert store.get_secret(fabricated_ref) is None


def test_delete_secret_is_idempotent_for_encrypted_file(credentials_dir):
    store = CredentialStore(credentials_dir=credentials_dir, prefer_keyring=False)
    ref = store.set_secret("token")
    store.delete_secret(ref)
    store.delete_secret(ref)  # second call must not raise
    assert store.get_secret(ref) is None


def test_malformed_credential_ref_raises(credentials_dir):
    store = CredentialStore(credentials_dir=credentials_dir, prefer_keyring=False)
    with pytest.raises(CredentialStoreError):
        store.get_secret("not-a-valid-ref-at-all")


def test_encrypted_store_and_master_key_files_are_owner_only(credentials_dir):
    store = CredentialStore(credentials_dir=credentials_dir, prefer_keyring=False)
    store.set_secret("token")

    key_path = credentials_dir / "master.key"
    store_path = credentials_dir / "secrets.enc"
    assert key_path.exists()
    assert store_path.exists()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(credentials_dir.stat().st_mode) == 0o700


def test_encrypted_store_never_contains_the_plaintext_secret_on_disk(credentials_dir):
    store = CredentialStore(credentials_dir=credentials_dir, prefer_keyring=False)
    secret = "sk-extremely-sensitive-value-12345"
    store.set_secret(secret)

    store_bytes = (credentials_dir / "secrets.enc").read_bytes()
    key_bytes = (credentials_dir / "master.key").read_bytes()
    assert secret.encode("utf-8") not in store_bytes
    assert secret.encode("utf-8") not in key_bytes


def test_master_key_persists_across_store_instances(credentials_dir):
    first = CredentialStore(credentials_dir=credentials_dir, prefer_keyring=False)
    ref = first.set_secret("token")

    second = CredentialStore(credentials_dir=credentials_dir, prefer_keyring=False)
    assert second.get_secret(ref) == "token"


def test_multiple_secrets_coexist_in_the_encrypted_store(credentials_dir):
    store = CredentialStore(credentials_dir=credentials_dir, prefer_keyring=False)
    ref_a = store.set_secret("secret-a")
    ref_b = store.set_secret("secret-b")
    assert store.get_secret(ref_a) == "secret-a"
    assert store.get_secret(ref_b) == "secret-b"
    store.delete_secret(ref_a)
    assert store.get_secret(ref_a) is None
    assert store.get_secret(ref_b) == "secret-b"  # deleting one leaves the other intact


def test_update_secret_overwrites_in_place_for_keyring(credentials_dir):
    fake = FakeKeyring()
    store = CredentialStore(credentials_dir=credentials_dir, keyring_backend=fake)
    ref = store.set_secret("old-token")
    store.update_secret(ref, "new-token")
    assert store.get_secret(ref) == "new-token"
    assert store.backend_for(ref) is CredentialBackend.KEYRING  # ref itself never changes


def test_update_secret_overwrites_in_place_for_encrypted_file(credentials_dir):
    store = CredentialStore(credentials_dir=credentials_dir, prefer_keyring=False)
    ref = store.set_secret("old-token")
    store.update_secret(ref, "new-token")
    assert store.get_secret(ref) == "new-token"


def test_update_secret_raises_if_keyring_write_fails(credentials_dir):
    fake = FakeKeyring()
    store = CredentialStore(credentials_dir=credentials_dir, keyring_backend=fake)
    ref = store.set_secret("old-token")
    fake.fail_set = True
    with pytest.raises(CredentialStoreError):
        store.update_secret(ref, "new-token")


def test_corrupted_or_replaced_master_key_raises_a_clear_error(credentials_dir):
    store = CredentialStore(credentials_dir=credentials_dir, prefer_keyring=False)
    ref = store.set_secret("token")

    # Simulate the master key file being lost/replaced (e.g. a fresh
    # profile) — the encrypted store on disk can no longer be read.
    (credentials_dir / "master.key").write_bytes(Fernet.generate_key())

    with pytest.raises(CredentialStoreError):
        store.get_secret(ref)
