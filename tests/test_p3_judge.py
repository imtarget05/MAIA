"""P3: LLM-judge returns None in mock mode, parses scores otherwise."""
from maia.eval_judge import judge_enabled, judge_faithfulness


class _MockLLM:
    mode = "mock"


class _FakeLLM:
    mode = "local"

    def chat(self, messages, max_tokens=256, temperature=0.0):
        return '{"supported": 0.85, "reason": "claims match context"}'


class _BadLLM:
    mode = "local"

    def chat(self, messages, max_tokens=256, temperature=0.0):
        return "not json at all"


def test_mock_mode_returns_none():
    assert judge_faithfulness("answer", ["ctx"], llm=_MockLLM()) is None


def test_fake_llm_parsed():
    j = judge_faithfulness("answer", ["ctx"], llm=_FakeLLM())
    assert j is not None and j["faith_judge"] == 0.85


def test_bad_json_returns_none():
    assert judge_faithfulness("answer", ["ctx"], llm=_BadLLM()) is None


def test_empty_returns_none():
    assert judge_faithfulness("", ["ctx"], llm=_FakeLLM()) is None
    assert judge_faithfulness("a", [], llm=_FakeLLM()) is None


def test_judge_off_by_default(monkeypatch):
    monkeypatch.delenv("MAIA_EVAL_JUDGE", raising=False)
    assert judge_enabled() is False
