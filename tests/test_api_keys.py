"""The user's own API key: storage, masking, save / replace / remove / session use."""

from __future__ import annotations

import sys
import uuid

import pytest

from app.config.credentials import MemorySecretStore, WindowsCredentialStore, mask_secret
from app.core.errors import InvalidInputError
from app.services.translation.keys import ApiKeyManager, validate_key_format

KEY_A = "AIza" + "TestKeyA" + "a" * 27  # built at run time; not real keys
KEY_B = "AIza" + "TestKeyB" + "b" * 27


def test_mask_never_shows_the_whole_key():
    masked = mask_secret(KEY_A)
    assert masked.startswith("AIza") and masked.endswith("aaaa") and "TestKeyA" not in masked
    assert mask_secret("short") == "•" * 8 and mask_secret("") == ""


@pytest.mark.parametrize("value", ["", "   ", "has space inside key 1234567890", "ключ" * 10, "tooshort"])
def test_invalid_key_formats(value):
    with pytest.raises(InvalidInputError):
        validate_key_format(value)


def test_key_format_strips_quotes_and_spaces():
    assert validate_key_format(f'  "{KEY_A}"\n') == KEY_A


def test_manager_save_replace_remove_session():
    manager = ApiKeyManager(MemorySecretStore())
    assert manager.key() is None and manager.status() == "No API key configured"
    manager.save(KEY_A)
    assert manager.key() == KEY_A and manager.is_saved()
    assert KEY_A not in manager.status() and "aaaa" in manager.status()
    manager.save(KEY_B)  # replace
    assert manager.key() == KEY_B
    manager.use_for_session(KEY_A)  # session key wins, stored key untouched
    assert manager.key() == KEY_A and "session" in manager.status() and manager.store.get(manager.name) == KEY_B
    assert manager.remove()
    assert manager.key() is None and not manager.is_saved() and not manager.remove()


def test_manager_logs_only_masked_keys(caplog):
    caplog.set_level("DEBUG")
    manager = ApiKeyManager(MemorySecretStore())
    manager.save(KEY_A)
    manager.use_for_session(KEY_B)
    manager.remove()
    assert KEY_A not in caplog.text and KEY_B not in caplog.text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager")
def test_windows_credential_manager_round_trip():
    store = WindowsCredentialStore(prefix=f"MediaToolkitTest-{uuid.uuid4().hex}")
    name = "Gemini API key"
    try:
        assert store.get(name) is None
        store.set(name, KEY_A)
        assert store.get(name) == KEY_A
        store.set(name, KEY_B)  # replace
        assert store.get(name) == KEY_B
        # a new store object (like a restarted app) reads the same key
        assert WindowsCredentialStore(prefix=store._prefix).get(name) == KEY_B
        manager = ApiKeyManager(store, name)
        assert manager.is_saved() and manager.key() == KEY_B
    finally:
        assert store.delete(name)
    assert store.get(name) is None and not store.delete(name)
