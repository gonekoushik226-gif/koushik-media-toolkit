"""The user's own AI API key: save, replace, remove, use for one session.

The application never ships a key. Each user creates their own key with the
provider and enters it in the app; requests are then made with that key, so
usage limits and any charges belong to the user's own provider account.
"""

from __future__ import annotations

import logging

from app.config.credentials import CredentialStoreError, SecretStore, default_store, mask_secret
from app.core.errors import AppError, InvalidInputError

log = logging.getLogger(__name__)

GEMINI_KEY_NAME = "Gemini API key"


def validate_key_format(key: str) -> str:
    value = (key or "").strip().strip('"').strip("'")
    if not value:
        raise InvalidInputError("Please paste your API key first.")
    if any(ch.isspace() for ch in value) or not value.isascii() or not 20 <= len(value) <= 200:
        raise InvalidInputError("This does not look like an API key. Copy the whole key from Google AI Studio "
                                "(it is one long word without spaces) and paste it again.")
    return value


class ApiKeyManager:
    def __init__(self, store: SecretStore | None = None, name: str = GEMINI_KEY_NAME):
        self.store = store or default_store()
        self.name = name
        self._session_key: str | None = None

    def _stored(self) -> str | None:
        try:
            return self.store.get(self.name)
        except CredentialStoreError:
            log.warning("Could not read the saved API key from %s", self.store.description)
            return None

    def key(self) -> str | None:
        """The key to use: one entered for this session, else the saved one."""
        return self._session_key or self._stored()

    def is_saved(self) -> bool:
        return self._stored() is not None

    def save(self, key: str) -> None:
        value = validate_key_format(key)
        try:
            self.store.set(self.name, value)
        except CredentialStoreError as exc:
            raise AppError(str(exc), title="Could not save the API key") from exc
        self._session_key = None
        log.info("API key saved in %s (%s)", self.store.description, mask_secret(value))

    def use_for_session(self, key: str) -> None:
        self._session_key = validate_key_format(key)
        log.info("API key set for this session only (%s)", mask_secret(self._session_key))

    def remove(self) -> bool:
        self._session_key = None
        try:
            removed = self.store.delete(self.name)
        except CredentialStoreError as exc:
            raise AppError(str(exc), title="Could not remove the API key") from exc
        log.info("Saved API key removed" if removed else "No saved API key to remove")
        return removed

    def status(self) -> str:
        saved = self._stored()
        if self._session_key:
            return f"Using a key for this session only: {mask_secret(self._session_key)} (not saved)"
        if saved:
            return f"API key saved in {self.store.description}: {mask_secret(saved)}"
        return "No API key configured"
