"""CM TECHMAP — Groq rate controller tests."""

from app.services.groq_rate_control import GroqRateController


class _FakePipeline:
    def __init__(self, store):
        self.store = store
        self.ops = []

    def incr(self, key, amount=1):
        self.ops.append(("incr", key, amount))
        return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))
        return self

    def lpush(self, key, value):
        self.ops.append(("lpush", key, value))
        return self

    def ltrim(self, key, start, end):
        self.ops.append(("ltrim", key, start, end))
        return self

    def execute(self):
        out = []
        for op in self.ops:
            if op[0] == "incr":
                key, amount = op[1], op[2]
                self.store[key] = int(self.store.get(key, 0)) + int(amount)
                out.append(self.store[key])
            elif op[0] == "expire":
                out.append(True)
            elif op[0] == "lpush":
                key, value = op[1], op[2]
                current = self.store.get(key, [])
                if not isinstance(current, list):
                    current = [current]
                current.insert(0, value)
                self.store[key] = current
                out.append(len(current))
            elif op[0] == "ltrim":
                key, start, end = op[1], op[2], op[3]
                current = self.store.get(key, [])
                if isinstance(current, list):
                    self.store[key] = current[start:end + 1]
                out.append(True)
        self.ops = []
        return out


class _FakeRedis:
    def __init__(self):
        self.data = {}

    def pipeline(self):
        return _FakePipeline(self.data)

    def setex(self, key, _ttl, value):
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)


def _controller_with_fake_redis(monkeypatch):
    ctl = GroqRateController()
    fake = _FakeRedis()
    monkeypatch.setattr(ctl, "_redis", fake)
    return ctl, fake


def test_reserve_accepts_within_budget(monkeypatch):
    ctl, _ = _controller_with_fake_redis(monkeypatch)
    r = ctl.reserve(100, 200)
    assert r.granted is True
    assert r.reason == "ok"


def test_remember_headers_roundtrip(monkeypatch):
    ctl, _ = _controller_with_fake_redis(monkeypatch)
    ctl.remember_headers(
        {
            "x-ratelimit-limit-tokens": "1000",
            "x-ratelimit-remaining-tokens": "500",
            "x-ratelimit-reset-tokens": "5s",
        }
    )
    last = ctl.get_last_headers()
    assert last is not None
    assert last.get("x-ratelimit-limit-tokens") == "1000"


def test_circuit_breaker_opens_after_failures(monkeypatch):
    ctl, _ = _controller_with_fake_redis(monkeypatch)
    threshold = 5
    for _ in range(threshold):
        ctl.record_failure("429 Too Many Requests")
    assert ctl.is_circuit_open() is True


def test_compute_retry_delay_uses_retry_after(monkeypatch):
    ctl, _ = _controller_with_fake_redis(monkeypatch)
    delay = ctl.compute_retry_delay("error retry-after: 2", attempt=0)
    assert delay >= 2.0


def test_reserve_for_entity_allows_within_entity_quota(monkeypatch):
    ctl, _ = _controller_with_fake_redis(monkeypatch)
    r = ctl.reserve_for_entity(entity_key="project:abc", input_tokens=100, output_tokens=200)
    assert r.granted is True
    assert r.reason == "ok"


def test_reserve_for_entity_denies_when_entity_is_hot(monkeypatch):
    ctl, _ = _controller_with_fake_redis(monkeypatch)

    monkeypatch.setattr("app.services.groq_rate_control.settings.groq_rpm_limit_free", 6)
    monkeypatch.setattr("app.services.groq_rate_control.settings.groq_safety_factor", 1.0)
    monkeypatch.setattr("app.services.groq_rate_control.settings.groq_min_utilization_factor", 1.0)
    monkeypatch.setattr("app.services.groq_rate_control.settings.groq_fairness_entity_share", 0.30)
    monkeypatch.setattr("app.services.groq_rate_control.settings.groq_fairness_entity_min_rpm", 1)

    r1 = ctl.reserve_for_entity(entity_key="project:burst", input_tokens=10, output_tokens=10)
    r2 = ctl.reserve_for_entity(entity_key="project:burst", input_tokens=10, output_tokens=10)
    r3 = ctl.reserve_for_entity(entity_key="project:burst", input_tokens=10, output_tokens=10)

    assert r1.granted is True
    assert r2.reason in {"ok", "entity_rpm_exceeded"}
    assert r3.reason in {"entity_rpm_exceeded", "rpm_exceeded"}


def test_record_failure_emits_circuit_event(monkeypatch):
    ctl, fake = _controller_with_fake_redis(monkeypatch)
    for _ in range(6):
        ctl.record_failure("429 rate limited")
    events_raw = fake.get("cm_techmap:groq:telemetry:events")
    assert isinstance(events_raw, list)
    assert ctl.is_circuit_open() is True
