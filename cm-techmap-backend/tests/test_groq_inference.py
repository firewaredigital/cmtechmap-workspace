"""CM TECHMAP — Groq inference unit tests (network-free)."""

import types

from app.services.groq_inference import GroqInferenceService


class _DummyRateController:
    def __init__(self):
        self.failures = 0
        self.successes = 0

    def reserve(self, _input_tokens, _output_tokens):
        return types.SimpleNamespace(granted=True, reason="ok")

    def reserve_for_entity(self, *, entity_key, input_tokens, output_tokens):
        _ = (entity_key, input_tokens, output_tokens)
        return types.SimpleNamespace(granted=True, reason="ok")

    def remember_headers(self, _headers):
        return None

    def record_success(self):
        self.successes += 1

    def record_failure(self, _error):
        self.failures += 1

    def is_circuit_open(self):
        return False

    def compute_retry_delay(self, _error, _attempt):
        return 0.01


class _GuardedRateController(_DummyRateController):
    def reserve(self, _input_tokens, _output_tokens):
        return types.SimpleNamespace(granted=False, reason="circuit_open")

    def reserve_for_entity(self, *, entity_key, input_tokens, output_tokens):
        _ = (entity_key, input_tokens, output_tokens)
        return types.SimpleNamespace(granted=False, reason="entity_rpm_exceeded")


class _DummyClient:
    class _Chat:
        class _Completions:
            def create(self, **_kwargs):
                usage = types.SimpleNamespace(prompt_tokens=12, completion_tokens=34)
                choice = types.SimpleNamespace(message=types.SimpleNamespace(content='{"ok": true}'))
                return types.SimpleNamespace(usage=usage, choices=[choice], id="groq-test-id")

        completions = _Completions()

    chat = _Chat()


def test_chat_with_retries_denied_by_guard(monkeypatch):
    svc = GroqInferenceService()
    monkeypatch.setattr("app.services.groq_inference.groq_rate_controller", _GuardedRateController())
    monkeypatch.setattr(svc, "_get_client", lambda: _DummyClient())

    result = svc._chat_with_retries(
        model="test-model",
        messages=[{"role": "user", "content": "hello"}],
        max_completion_tokens=128,
    )

    assert result.ok is False
    assert result.error == "rate_guard:circuit_open"


def test_chat_with_retries_success_records_metrics(monkeypatch):
    svc = GroqInferenceService()
    rc = _DummyRateController()
    monkeypatch.setattr("app.services.groq_inference.groq_rate_controller", rc)
    monkeypatch.setattr(svc, "_get_client", lambda: _DummyClient())

    result = svc._chat_with_retries(
        model="test-model",
        messages=[{"role": "user", "content": "hello"}],
        max_completion_tokens=128,
    )

    assert result.ok is True
    assert result.input_tokens == 12
    assert result.output_tokens == 34
    assert rc.successes == 1
    assert rc.failures == 0


def test_chat_with_retries_uses_entity_reservation(monkeypatch):
    svc = GroqInferenceService()
    rc = _DummyRateController()
    monkeypatch.setattr("app.services.groq_inference.groq_rate_controller", rc)
    monkeypatch.setattr(svc, "_get_client", lambda: _DummyClient())

    result = svc._chat_with_retries(
        model="test-model",
        messages=[{"role": "user", "content": "hello"}],
        max_completion_tokens=64,
        fairness_entity="project:abc",
    )

    assert result.ok is True


def test_merge_vision_payloads_dedupes_polygons():
    svc = GroqInferenceService()
    base = {
        "detections": [
            {"polygon": [[1, 1], [2, 2], [1, 1]], "confidence": 0.9},
        ],
        "terrain": [],
        "notes": ["base"],
    }
    tile = {
        "detections": [
            {"polygon": [[1, 1], [2, 2], [1, 1]], "confidence": 0.8},
            {"polygon": [[3, 3], [4, 4], [3, 3]], "confidence": 0.7},
        ],
        "terrain": [],
        "notes": ["tile"],
    }

    merged = svc._merge_vision_payloads(base, tile)
    assert len(merged["detections"]) == 2
    assert "base" in merged["notes"]
    assert "tile" in merged["notes"]
