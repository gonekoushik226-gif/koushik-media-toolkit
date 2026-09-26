"""Gemini API client tests against a local mock server (no network, no real key).

Fake keys are assembled at run time so that no string in this file looks like
a real Google API key.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.core.jobs import JobContext
from app.services.translation import gemini
from app.services.translation.languages import get_language
from app.services.translation.provider import (
    ApiKeyError,
    ContentBlockedError,
    InvalidResponseError,
    MissingApiKeyError,
    ModelNotFoundError,
    NetworkError,
    QuotaExceededError,
    RateLimitError,
    RequestRejectedError,
    ResponseTooLongError,
    ServiceUnavailableError,
)
from app.utils.logging_setup import redact

FAKE_KEY = "AIza" + "Fake" + "x" * 31  # built at run time; not a real key
EN = get_language("en")


def answer(payload: dict, finish: str = "STOP", usage: dict | None = None) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}, "finishReason": finish}],
            "usageMetadata": usage or {"promptTokenCount": 10, "candidatesTokenCount": 5}}


def api_error(status: int, message: str, reason: str = "", details: list | None = None) -> tuple[int, dict]:
    return status, {"error": {"code": status, "message": message, "status": reason, "details": details or []}}


class MockGemini:
    """A tiny HTTP server that plays back scripted responses and records requests."""

    def __init__(self):
        self.responses: list = []
        self.requests: list[dict] = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                mock.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                      "body": json.loads(body) if body else None})
                status, payload, headers = 200, {}, {}
                if mock.responses:
                    item = mock.responses.pop(0)
                    if len(item) == 3:
                        status, payload, headers = item
                    else:
                        status, payload = item
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(raw)

            do_GET = _reply
            do_POST = _reply

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1beta"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def mock():
    server = MockGemini()
    yield server
    server.close()


@pytest.fixture
def waits():
    return []


@pytest.fixture
def provider(mock, waits):
    return gemini.GeminiProvider(FAKE_KEY, model="gemini-test", base_url=mock.url, max_retries=3, timeout=(2, 5),
                                 sleep=waits.append)


def segments(n=2):
    return [{"id": f"p1b{i}", "text": f"Hello {i}"} for i in range(n)]


def translations(n=2, prefix="Bonjour"):
    return {"translations": [{"id": f"p1b{i}", "text": f"{prefix} {i}"} for i in range(n)]}


# ----------------------------------------------------------------------------
def test_missing_key():
    with pytest.raises(MissingApiKeyError):
        gemini.GeminiProvider("   ")


def test_key_only_in_header(provider, mock):
    mock.responses = [(200, answer(translations()))]
    result = provider.translate_segments(segments(), EN, None, "", JobContext())
    assert result == {"p1b0": "Bonjour 0", "p1b1": "Bonjour 1"}
    request = mock.requests[0]
    assert request["headers"]["x-goog-api-key"] == FAKE_KEY
    assert FAKE_KEY not in request["path"] and "key=" not in request["path"]
    assert request["path"] == "/v1beta/models/gemini-test:generateContent"
    body = request["body"]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseSchema" in body["generationConfig"]
    assert "English" in body["systemInstruction"]["parts"][0]["text"]
    assert json.loads(body["contents"][0]["parts"][0]["text"].split("\n", 1)[1])["segments"][0]["text"] == "Hello 0"
    assert FAKE_KEY not in repr(provider)
    assert provider.usage.requests == 1 and provider.usage.input_tokens == 10


def test_image_request_and_box_conversion(provider, mock):
    mock.responses = [(200, answer({"regions": [
        {"box_2d": [100, 200, 300, 600], "kind": "dialogue", "original": "やあ", "translation": "Hi"},
        {"box_2d": [500, 500, 400, 400], "kind": "weird", "original": "x", "translation": "Swapped"},
        {"box_2d": [1, 2], "kind": "text", "original": "bad", "translation": "dropped"},
        {"box_2d": [10, 10, 20, 20], "kind": "text", "original": "empty", "translation": "  "},
    ]}))]
    regions = provider.analyze_image(b"\xff\xd8fakejpeg", "image/jpeg", EN, get_language("ja"), "comic", True,
                                     JobContext())
    assert regions[0] == {"box": (0.2, 0.1, 0.6, 0.3), "kind": "dialogue", "original": "やあ", "translation": "Hi"}
    assert regions[1]["box"] == (0.4, 0.4, 0.5, 0.5) and regions[1]["kind"] == "text"
    assert len(regions) == 2
    parts = mock.requests[0]["body"]["contents"][0]["parts"]
    assert "text" in parts[0] and parts[1]["inlineData"]["mimeType"] == "image/jpeg"
    assert base64.b64decode(parts[1]["inlineData"]["data"]) == b"\xff\xd8fakejpeg"
    system = mock.requests[0]["body"]["systemInstruction"]["parts"][0]["text"]
    assert "Japanese" in system and "box_2d" in system


@pytest.mark.parametrize("response, error", [
    (api_error(400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT",
               [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]), ApiKeyError),
    (api_error(403, "Your API key was reported as leaked.", "PERMISSION_DENIED"), ApiKeyError),
    (api_error(403, "Permission denied.", "PERMISSION_DENIED"), ApiKeyError),
    (api_error(401, "Unauthenticated", "UNAUTHENTICATED"), ApiKeyError),
    (api_error(404, "models/gemini-test is not found", "NOT_FOUND"), ModelNotFoundError),
    (api_error(402, "Prepaid credits depleted", "FAILED_PRECONDITION"), QuotaExceededError),
    (api_error(429, "You exceeded your current quota, please check your plan and billing details.",
               "RESOURCE_EXHAUSTED",
               [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                 "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]),
     QuotaExceededError),
    (api_error(400, "User location is not supported for the API use.", "FAILED_PRECONDITION"), RequestRejectedError),
    (api_error(400, "Request contains an invalid argument.", "INVALID_ARGUMENT"), RequestRejectedError),
])
def test_error_mapping_without_retry(provider, mock, waits, response, error):
    mock.responses = [response]
    with pytest.raises(error) as info:
        provider.translate_segments(segments(), EN, None, "", JobContext())
    assert len(mock.requests) == 1 and not waits  # no retries for these
    assert FAKE_KEY not in info.value.message and FAKE_KEY not in (info.value.details or "")


def test_rate_limit_waits_for_server_delay_then_succeeds(provider, mock, waits):
    mock.responses = [api_error(429, "Resource has been exhausted (e.g. check quota).", "RESOURCE_EXHAUSTED",
                                [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]),
                      (200, answer(translations()))]
    ctx = JobContext()
    assert provider.translate_segments(segments(), EN, None, "", ctx)["p1b1"] == "Bonjour 1"
    assert waits == [37.0] and len(mock.requests) == 2
    assert "retrying in 37 s" in ctx.detail


def test_rate_limit_gives_up_after_max_retries(provider, mock, waits):
    mock.responses = [api_error(429, "Too many requests", "RESOURCE_EXHAUSTED")] * 4
    with pytest.raises(RateLimitError):
        provider.translate_segments(segments(), EN, None, "", JobContext())
    assert len(mock.requests) == 4 and len(waits) == 3  # 1 try + 3 retries, never endless


def test_retry_after_header_and_cap(provider, mock, waits):
    mock.responses = [(429, {"error": {"code": 429, "message": "slow down"}}, {"Retry-After": "500"}),
                      (200, answer(translations()))]
    provider.translate_segments(segments(), EN, None, "", JobContext())
    assert waits == [gemini.MAX_RATE_LIMIT_WAIT]


def test_server_errors_are_retried(provider, mock, waits):
    mock.responses = [api_error(503, "The model is overloaded.", "UNAVAILABLE"),
                      api_error(500, "Internal error", "INTERNAL"), (200, answer(translations()))]
    provider.translate_segments(segments(), EN, None, "", JobContext())
    assert len(mock.requests) == 3 and len(waits) == 2
    mock.responses = [api_error(503, "overloaded", "UNAVAILABLE")] * 5
    with pytest.raises(ServiceUnavailableError):
        provider.translate_segments(segments(), EN, None, "", JobContext())


def test_network_failure(waits):
    provider = gemini.GeminiProvider(FAKE_KEY, base_url="http://127.0.0.1:9/v1beta", max_retries=2, timeout=(1, 1),
                                     sleep=waits.append)
    with pytest.raises(NetworkError) as info:
        provider.translate_segments(segments(), EN, None, "", JobContext())
    assert len(waits) == 2 and FAKE_KEY not in str(info.value.details)


def test_schema_rejection_falls_back(provider, mock):
    mock.responses = [api_error(400, "Invalid JSON payload: responseSchema unknown field", "INVALID_ARGUMENT"),
                      (200, answer(translations()))]
    provider.translate_segments(segments(), EN, None, "", JobContext())
    assert "responseSchema" in mock.requests[0]["body"]["generationConfig"]
    assert "responseSchema" not in mock.requests[1]["body"]["generationConfig"]


def test_blocked_and_bad_answers(provider, mock):
    mock.responses = [(200, {"promptFeedback": {"blockReason": "SAFETY"}})]
    with pytest.raises(ContentBlockedError):
        provider.translate_segments(segments(), EN, None, "", JobContext())
    mock.responses = [(200, {"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]})]
    with pytest.raises(ContentBlockedError):
        provider.translate_segments(segments(), EN, None, "", JobContext())
    mock.responses = [(200, answer(translations(), finish="MAX_TOKENS"))]
    with pytest.raises(ResponseTooLongError):
        provider.translate_segments(segments(), EN, None, "", JobContext())
    # not JSON twice -> invalid (asked exactly once more, no endless loop)
    not_json = {"candidates": [{"content": {"parts": [{"text": "Sorry, I cannot"}]}, "finishReason": "STOP"}]}
    mock.requests.clear()
    mock.responses = [(200, not_json), (200, not_json)]
    with pytest.raises(InvalidResponseError):
        provider.translate_segments(segments(), EN, None, "", JobContext())
    assert len(mock.requests) == 2


def test_fenced_json_and_missing_ids(provider, mock):
    fenced = {"candidates": [{"content": {"parts": [{"text": "```json\n" + json.dumps(translations(1)) + "\n```"}]},
                              "finishReason": "STOP"}]}
    mock.responses = [(200, fenced), (200, answer({"translations": [{"id": "p1b1", "text": "Bonjour 1"}]}))]
    result = provider.translate_segments(segments(2), EN, None, "", JobContext())
    assert result == {"p1b0": "Bonjour 0", "p1b1": "Bonjour 1"}
    second = json.loads(mock.requests[1]["body"]["contents"][0]["parts"][0]["text"].split("\n", 1)[1])
    assert [s["id"] for s in second["segments"]] == ["p1b1"]  # only the missing block was asked again


def test_thought_parts_are_ignored(provider, mock):
    data = answer(translations(1))
    data["candidates"][0]["content"]["parts"].insert(0, {"text": "thinking...", "thought": True})
    mock.responses = [(200, data)]
    assert provider.translate_segments(segments(1), EN, None, "", JobContext()) == {"p1b0": "Bonjour 0"}


def test_list_models(provider, mock):
    mock.responses = [
        (200, {"models": [
            {"name": "models/gemini-2.9-pro", "displayName": "Gemini 2.9 Pro",
             "supportedGenerationMethods": ["generateContent"], "inputTokenLimit": 1000, "outputTokenLimit": 100},
            {"name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"]},
            {"name": "models/gemini-3.8-flash-tts", "supportedGenerationMethods": ["generateContent"]},
        ], "nextPageToken": "page2"}),
        (200, {"models": [{"name": "models/gemini-3.8-flash", "displayName": "Gemini 3.8 Flash",
                           "supportedGenerationMethods": ["generateContent", "countTokens"]}]}),
    ]
    models = provider.list_models()
    assert [m.id for m in models] == ["gemini-3.8-flash", "gemini-2.9-pro"]  # recommended first, others filtered
    assert "pageToken=page2" in mock.requests[1]["path"]
    assert all(FAKE_KEY not in r["path"] for r in mock.requests)


def test_cancel_during_wait(provider, mock):
    mock.responses = [api_error(503, "overloaded", "UNAVAILABLE")] * 5
    ctx = JobContext()
    provider._sleep_override = lambda seconds: ctx.cancel()
    from app.core.errors import JobCancelled

    with pytest.raises(JobCancelled):
        provider.translate_segments(segments(), EN, None, "", ctx)
    assert len(mock.requests) == 1


def test_helpers():
    assert gemini.parse_retry_delay({"details": [{"retryDelay": "12.5s"}]}, {}) == 12.5
    assert gemini.parse_retry_delay({}, {"Retry-After": "7"}) == 7.0
    assert gemini.parse_retry_delay({}, {}) is None
    assert gemini.extract_json('Here you go: {"a": 1} thanks') == {"a": 1}
    assert gemini.is_translation_model("gemini-3.8-flash")
    assert not gemini.is_translation_model("gemini-3.8-flash-image") and not gemini.is_translation_model("imagen-4")


def test_redaction_of_keys():
    line = f"calling https://example.com/x?key={FAKE_KEY}&alt=json with api_key={FAKE_KEY} or {FAKE_KEY}"
    cleaned = redact(line)
    assert FAKE_KEY not in cleaned and "Fake" not in cleaned
