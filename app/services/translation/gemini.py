"""Google Gemini API provider (REST, via the existing ``requests`` dependency).

Official documentation used:
  API keys ........ https://ai.google.dev/gemini-api/docs/api-key
  generateContent . https://ai.google.dev/api/generate-content
  models.list ..... https://ai.google.dev/api/models
  Images / boxes .. https://ai.google.dev/gemini-api/docs/image-understanding
  Errors / limits . https://ai.google.dev/gemini-api/docs/troubleshooting, .../rate-limits

The user's own API key is sent only in the ``x-goog-api-key`` request header -
never in a URL, so it cannot appear in logs or error messages.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
import time

import requests

from app import APP_ID, __version__
from app.core.jobs import JobContext, run_abandonable
from app.services.translation import prompts
from app.services.translation.languages import Language
from app.services.translation.models import REGION_KINDS
from app.services.translation.provider import (
    ApiKeyError,
    ContentBlockedError,
    InvalidResponseError,
    MissingApiKeyError,
    ModelInfo,
    ModelNotFoundError,
    NetworkError,
    QuotaExceededError,
    RateLimitError,
    RequestRejectedError,
    ResponseTooLongError,
    ServiceUnavailableError,
    TranslationError,
    TranslationProvider,
)
from app.utils.logging_setup import redact

log = logging.getLogger(__name__)

PROVIDER_NAME = "Google Gemini API"
API_BASE = "https://generativelanguage.googleapis.com/v1beta"
KEY_PAGE_URL = "https://aistudio.google.com/apikey"
RATE_LIMIT_URL = "https://aistudio.google.com/rate-limit"
KEY_DOCS_URL = "https://ai.google.dev/gemini-api/docs/api-key"
PRICING_URL = "https://ai.google.dev/gemini-api/docs/pricing"
# Google's current recommendations for new projects (docs, 2026-09): "3.5 Flash-Lite or 3.8 Flash".
DEFAULT_MODEL = "gemini-3.8-flash"
RECOMMENDED_MODELS = ("gemini-3.8-flash", "gemini-3.5-flash-lite")
_NON_TEXT_MARKERS = ("tts", "image", "embedding", "live", "transcribe", "robotics", "computer-use", "audio", "veo",
                     "lyria", "omni", "research", "antigravity", "aqa")
_BLOCK_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY",
                  "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION", "LANGUAGE", "OTHER"}
MAX_RATE_LIMIT_WAIT = 90.0

_STRING = {"type": "STRING"}
TEXT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "translations": {
            "type": "ARRAY",
            "items": {"type": "OBJECT", "properties": {"id": _STRING, "text": _STRING}, "required": ["id", "text"]},
        }
    },
    "required": ["translations"],
}
IMAGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "regions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "box_2d": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                    "kind": {"type": "STRING", "enum": list(REGION_KINDS)},
                    "original": _STRING,
                    "translation": _STRING,
                },
                "required": ["box_2d", "kind", "original", "translation"],
            },
        }
    },
    "required": ["regions"],
}


class _SchemaRejected(Exception):
    """The endpoint did not accept responseSchema; retry without it."""


def is_translation_model(model_id: str) -> bool:
    lowered = model_id.lower()
    return lowered.startswith("gemini") and not any(marker in lowered for marker in _NON_TEXT_MARKERS)


def parse_retry_delay(error: dict, headers) -> float | None:
    """Seconds to wait, from RetryInfo ("retryDelay": "37s") or a Retry-After header."""
    for detail in error.get("details") or []:
        if isinstance(detail, dict) and "retryDelay" in detail:
            match = re.match(r"^\s*([\d.]+)\s*s?\s*$", str(detail["retryDelay"]))
            if match:
                return float(match.group(1))
    value = headers.get("Retry-After") if headers is not None else None
    try:
        return float(value) if value else None
    except ValueError:
        return None


def extract_json(text: str) -> dict:
    """Parse the model's JSON answer (tolerates ``` fences and stray text)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("the answer is not a JSON object")
    return value


class GeminiProvider(TranslationProvider):
    name = PROVIDER_NAME

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = API_BASE,
        session: requests.Session | None = None,
        max_retries: int = 4,
        timeout: tuple[float, float] = (15.0, 300.0),
        sleep=None,
    ):
        super().__init__()
        if not api_key or not api_key.strip():
            raise MissingApiKeyError("No API key is configured for AI translation.")
        self._api_key = api_key.strip()
        self.model = (model or DEFAULT_MODEL).strip().removeprefix("models/")
        self.base_url = base_url.rstrip("/")
        self._session = session or requests.Session()
        self.max_retries = max_retries
        self.timeout = timeout
        self._sleep_override = sleep
        self._use_schema = True

    def __repr__(self) -> str:  # never show the key
        return f"GeminiProvider(model={self.model!r})"

    # ------------------------------------------------------------------ HTTP
    def _wait(self, seconds: float, ctx: JobContext | None) -> None:
        if self._sleep_override is not None:
            self._sleep_override(seconds)
        elif ctx is not None:
            ctx.wait(seconds)
        else:
            time.sleep(seconds)

    def _send(self, method: str, path: str, ctx: JobContext | None, body: dict | None, params: dict | None):
        headers = {"x-goog-api-key": self._api_key, "Content-Type": "application/json",
                   "User-Agent": f"{APP_ID}/{__version__}"}
        url = f"{self.base_url}/{path}"

        def call():
            return self._session.request(method, url, headers=headers, json=body, params=params, timeout=self.timeout)

        return run_abandonable(call, ctx) if ctx is not None else call()

    def _request(self, method: str, path: str, ctx: JobContext | None, body: dict | None = None,
                 params: dict | None = None) -> dict:
        attempt = 0
        while True:
            if ctx is not None:
                ctx.check_cancelled()
            delay = None
            try:
                response = self._send(method, path, ctx, body, params)
            except requests.exceptions.Timeout as exc:
                error, retryable = NetworkError("The AI service did not answer in time.", details=redact(str(exc))), True
            except requests.exceptions.ConnectionError as exc:
                error, retryable = NetworkError(
                    "Could not connect to the Google Gemini API. Check your internet connection, proxy or firewall.",
                    details=redact(str(exc))), True
            except requests.exceptions.RequestException as exc:
                error, retryable = NetworkError("The request to the AI service failed.", details=redact(str(exc))), True
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise InvalidResponseError("The AI service returned an unreadable answer.") from exc
                error, retryable, delay = self._classify(response)
            attempt += 1
            if not retryable or attempt > self.max_retries:
                raise error
            wait = delay if delay is not None else min(60.0, 2.0 ** attempt + random.uniform(0, 1))
            wait = min(wait, MAX_RATE_LIMIT_WAIT)
            log.info("Gemini request failed (%s); retry %s/%s in %.0f s", error.title, attempt, self.max_retries, wait)
            if ctx is not None:
                ctx.set_progress(None, f"{error.title} - retrying in {wait:.0f} s")
            self._wait(wait, ctx)

    def _classify(self, response) -> tuple[TranslationError, bool, float | None]:
        status = response.status_code
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if not isinstance(error, dict):
            error = {}
        message = str(error.get("message") or response.text[:300] or "")
        # The details carry machine-readable reasons, e.g. ErrorInfo "API_KEY_INVALID" or a QuotaFailure quotaId
        # such as "GenerateRequestsPerDayPerProjectPerModel-FreeTier".
        try:
            detail_text = json.dumps(error.get("details") or [])
        except (TypeError, ValueError):
            detail_text = ""
        text = f"{message} {error.get('status', '')} {error.get('code', '')} {detail_text}".lower()
        details = redact(f"HTTP {status}: {message}".strip())

        if "leaked" in text:
            return ApiKeyError("Google blocked this API key because it was reported as leaked. Create a new key in "
                               "Google AI Studio and save it in the app.", details), False, None
        if status == 401 or any(s in text for s in ("api_key_invalid", "api key not valid", "api key expired",
                                                     "invalid api key", "api_key_expired")):
            return ApiKeyError("Google did not accept the API key. Check that the whole key was pasted, or create "
                               "a new key in Google AI Studio.", details), False, None
        if status == 403:
            return ApiKeyError("This API key is not allowed to use the Gemini API. Make sure the key was created in "
                               "Google AI Studio for a project where the Gemini API is enabled.", details), False, None
        if status == 402:
            return QuotaExceededError("Your prepaid Gemini API credit is used up. Add credit in Google AI Studio, "
                                      "then run the translation again - finished pages are kept.", details), False, None
        if status == 404:
            return ModelNotFoundError(f"The AI model '{self.model}' is not available for your API key. Choose another "
                                      "model under Translate > AI provider & API key.", details), False, None
        if status == 429:
            # Note: per-minute limits also mention "plan and billing details", so only a per-day quota id
            # (or the documented "quota_exceeded" code) means the daily quota is used up.
            if any(s in text for s in ("per day", "perday", "per_day", "daily", "quota_exceeded")):
                return QuotaExceededError(
                    "Your daily Gemini API limit has been reached (daily limits reset at midnight Pacific time). "
                    "Finished pages are kept - run the translation again later to continue, or raise your limits "
                    "in Google AI Studio.", details), False, None
            return (RateLimitError("The Gemini API is receiving too many requests from your key. Finished pages are "
                                   "kept - wait a minute and run the translation again.", details),
                    True, parse_retry_delay(error, response.headers) or 20.0)
        if status == 400:
            if "location is not supported" in text:
                return RequestRejectedError("The Gemini API is not available for your region with this key.",
                                            details), False, None
            if "schema" in text and self._use_schema:
                raise _SchemaRejected(details)
            return RequestRejectedError("The AI service rejected the request for this page.", details), False, None
        if status in (408, 499):
            return NetworkError("The request to the AI service timed out.", details), True, None
        if status >= 500:
            return ServiceUnavailableError(f"Google's AI service is busy or unavailable (HTTP {status}). Finished "
                                           "pages are kept - try again in a few minutes.", details), True, None
        return TranslationError(f"The AI service returned an error (HTTP {status}).", details), False, None

    # ------------------------------------------------------------ generation
    def _generate(self, system: str, parts: list[dict], schema: dict, ctx: JobContext | None) -> dict:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        if self._use_schema:
            body["generationConfig"]["responseSchema"] = schema
        path = f"models/{self.model}:generateContent"
        try:
            data = self._request("POST", path, ctx, body)
        except _SchemaRejected:
            log.info("responseSchema was rejected; continuing without it")
            self._use_schema = False
            body["generationConfig"].pop("responseSchema", None)
            data = self._request("POST", path, ctx, body)
        return self._read_answer(data)

    def _read_answer(self, data: dict) -> dict:
        self.usage.requests += 1
        usage = data.get("usageMetadata") or {}
        self.usage.input_tokens += int(usage.get("promptTokenCount") or 0)
        self.usage.output_tokens += int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0)
        feedback = data.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise ContentBlockedError(f"The AI service refused this page ({feedback['blockReason']}).")
        candidates = data.get("candidates") or []
        if not candidates:
            raise InvalidResponseError("The AI service returned no answer for this page.")
        candidate = candidates[0]
        finish = str(candidate.get("finishReason") or "")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and not p.get("thought"))
        if finish == "MAX_TOKENS":
            raise ResponseTooLongError("The answer was longer than the model allows.")
        if finish in _BLOCK_REASONS and not text.strip():
            raise ContentBlockedError(f"The AI service did not translate this page ({finish}).")
        if not text.strip():
            raise InvalidResponseError("The AI service returned an empty answer.")
        try:
            return extract_json(text)
        except ValueError as exc:
            raise InvalidResponseError("The AI service's answer was not valid JSON.", details=text[:300]) from exc

    def _generate_valid(self, system: str, parts: list[dict], schema: dict, ctx: JobContext | None) -> dict:
        """One extra attempt when the answer is unusable (a second answer is usually fine)."""
        try:
            return self._generate(system, parts, schema, ctx)
        except InvalidResponseError:
            log.info("Unusable answer from Gemini; asking once more")
            return self._generate(system, parts, schema, ctx)

    # -------------------------------------------------------------- public
    def list_models(self, ctx: JobContext | None = None) -> list[ModelInfo]:
        models: list[ModelInfo] = []
        token = None
        for _ in range(20):  # pagination guard
            params = {"pageSize": 1000}
            if token:
                params["pageToken"] = token
            data = self._request("GET", "models", ctx, params=params)
            for item in data.get("models") or []:
                if not isinstance(item, dict):
                    continue
                if "generateContent" not in (item.get("supportedGenerationMethods") or []):
                    continue
                model_id = str(item.get("name", "")).removeprefix("models/")
                if not is_translation_model(model_id):
                    continue
                models.append(ModelInfo(model_id, str(item.get("displayName") or ""),
                                        item.get("inputTokenLimit"), item.get("outputTokenLimit")))
            token = data.get("nextPageToken")
            if not token:
                break
        models.sort(key=lambda m: (m.id not in RECOMMENDED_MODELS, "preview" in m.id, m.id))
        return models

    def translate_segments(self, segments: list[dict], target: Language, source: Language | None, context: str,
                           ctx: JobContext) -> dict[str, str]:
        wanted = [{"id": str(s["id"]), "text": str(s["text"])} for s in segments]
        result = self._translate_once(wanted, target, source, context, ctx)
        missing = [s for s in wanted if s["id"] not in result]
        if missing and len(missing) < len(wanted):
            log.info("%s segment(s) missing from the answer; asking again for those", len(missing))
            result.update(self._translate_once(missing, target, source, context, ctx))
            missing = [s for s in wanted if s["id"] not in result]
        if missing:
            raise InvalidResponseError(f"The AI service did not translate {len(missing)} of {len(wanted)} text blocks.")
        return {s["id"]: result[s["id"]] for s in wanted}

    def _translate_once(self, segments: list[dict], target: Language, source: Language | None, context: str,
                        ctx: JobContext) -> dict[str, str]:
        payload: dict = {"segments": segments}
        if context:
            payload["context_before"] = context
        user = prompts.segments_user(json.dumps(payload, ensure_ascii=False))
        answer = self._generate_valid(prompts.text_system(target, source), [{"text": user}], TEXT_SCHEMA, ctx)
        items = answer.get("translations")
        if not isinstance(items, list):
            raise InvalidResponseError("The AI service's answer did not contain translations.")
        known = {s["id"] for s in segments}
        result = {}
        for item in items:
            if isinstance(item, dict) and str(item.get("id")) in known and isinstance(item.get("text"), str):
                result[str(item["id"])] = item["text"]
        return result

    def analyze_image(self, image: bytes, mime_type: str, target: Language, source: Language | None, doc_type: str,
                      include_sfx: bool, ctx: JobContext) -> list[dict]:
        parts = [
            {"text": prompts.image_user(target)},  # the docs recommend the text prompt before a single image
            {"inlineData": {"mimeType": mime_type, "data": base64.b64encode(image).decode("ascii")}},
        ]
        answer = self._generate_valid(prompts.image_system(target, source, doc_type, include_sfx), parts,
                                      IMAGE_SCHEMA, ctx)
        items = answer.get("regions")
        if not isinstance(items, list):
            raise InvalidResponseError("The AI service's answer did not contain text regions.")
        return [region for region in (normalize_region(item) for item in items) if region is not None]


def normalize_region(item) -> dict | None:
    """Convert Gemini's box_2d [ymin, xmin, ymax, xmax] (0-1000) into a
    normalised (x0, y0, x1, y1) box; drop malformed items."""
    if not isinstance(item, dict):
        return None
    box = item.get("box_2d")
    translation = item.get("translation")
    if not isinstance(translation, str) or not translation.strip():
        return None
    if not isinstance(box, list | tuple) or len(box) != 4:
        return None
    try:
        ymin, xmin, ymax, xmax = (min(1000.0, max(0.0, float(v))) for v in box)
    except (TypeError, ValueError):
        return None
    if ymin > ymax:
        ymin, ymax = ymax, ymin
    if xmin > xmax:
        xmin, xmax = xmax, xmin
    if xmax - xmin < 2 or ymax - ymin < 2:
        return None
    kind = str(item.get("kind") or "text").lower()
    return {
        "box": (xmin / 1000, ymin / 1000, xmax / 1000, ymax / 1000),
        "kind": kind if kind in REGION_KINDS else "text",
        "original": str(item.get("original") or ""),
        "translation": translation.strip(),
    }
