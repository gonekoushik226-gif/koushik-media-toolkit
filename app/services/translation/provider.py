"""Provider-neutral interface and errors for AI translation.

A provider turns (a) lists of text segments and (b) page images into
translations. The job and page processors only talk to this interface, so a
different AI service can be added later by implementing it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.core.errors import AppError
from app.core.jobs import JobContext
from app.services.translation.languages import Language


class TranslationError(AppError):
    title = "Translation problem"


# -- errors that stop the whole job (completed pages stay saved for a later run) --
class MissingApiKeyError(TranslationError):
    title = "API key needed"


class ApiKeyError(TranslationError):
    title = "API key problem"


class QuotaExceededError(TranslationError):
    title = "Usage limit reached"


class RateLimitError(TranslationError):
    title = "Too many requests"


class ServiceUnavailableError(TranslationError):
    title = "AI service unavailable"


class NetworkError(TranslationError):
    title = "Network problem"


class ModelNotFoundError(TranslationError):
    title = "AI model not available"


# -- errors that only affect one page (the page is left untranslated and retried next time) --
class PageError(TranslationError):
    """Base for failures limited to one request/page."""


class ContentBlockedError(PageError):
    title = "Page blocked by the AI service"


class InvalidResponseError(PageError):
    title = "Unusable AI response"


class RequestRejectedError(PageError):
    title = "Request rejected"


class ResponseTooLongError(PageError):
    """The answer hit the model's output limit; the caller should send less at once."""

    title = "Response too long"


FATAL_ERRORS = (MissingApiKeyError, ApiKeyError, QuotaExceededError, RateLimitError, ServiceUnavailableError,
                NetworkError, ModelNotFoundError)


@dataclass(frozen=True)
class ModelInfo:
    id: str
    display_name: str = ""
    input_token_limit: int | None = None
    output_token_limit: int | None = None

    @property
    def label(self) -> str:
        return f"{self.display_name} ({self.id})" if self.display_name and self.display_name != self.id else self.id


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def describe(self) -> str:
        if not self.requests:
            return "No requests were sent to the AI service."
        return (f"{self.requests} request(s) to the AI service, about {self.input_tokens:,} input and "
                f"{self.output_tokens:,} output tokens (billed to your own API account if you are on a paid plan).")


class TranslationProvider(ABC):
    """Interface implemented by each AI service."""

    name = "AI provider"

    def __init__(self) -> None:
        self.usage = Usage()

    @abstractmethod
    def list_models(self, ctx: JobContext | None = None) -> list[ModelInfo]:
        """Models usable for translation with this key (also validates the key)."""

    @abstractmethod
    def translate_segments(
        self,
        segments: list[dict],
        target: Language,
        source: Language | None,
        context: str,
        ctx: JobContext,
    ) -> dict[str, str]:
        """Translate ``[{"id", "text"}, ...]``; returns ``{id: translation}``."""

    @abstractmethod
    def analyze_image(
        self,
        image: bytes,
        mime_type: str,
        target: Language,
        source: Language | None,
        doc_type: str,
        include_sfx: bool,
        ctx: JobContext,
    ) -> list[dict]:
        """Find and translate text on a page image. Returns dicts with
        ``box`` (normalised x0, y0, x1, y1), ``kind``, ``original``, ``translation``."""
