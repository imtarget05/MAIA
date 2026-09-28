import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import maia.ner_tool as ner_tool
from maia.agent.intent_router import IntentRouter
from maia.agent.tools import TOOL_REGISTRY


def test_registry_contains_extract_entities():
    assert "extract_entities" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["extract_entities"])
    # intent_router allowlist derives from TOOL_REGISTRY (no hardcode)
    import inspect

    src = inspect.getsource(IntentRouter.decide)
    assert "TOOL_REGISTRY" in src
    assert "extract_entities" not in src  # not hardcoded per-tool


def _fake_bundle(monkeypatch, labels):
    """Inject a fake (vocab, labels, model) bundle to avoid needing torch."""

    class FakeOut:
        def __init__(self, ids):
            self._ids = ids

        def argmax(self, _dim):
            class W:
                def __init__(self, ids):
                    self._ids = ids

                def __getitem__(self, i):
                    assert i == 0

                    class Row:
                        def tolist(self_inner):
                            return self._ids

                    return Row()

            return W(self._ids)

    class FakeModel:
        def __init__(self, ids):
            self._ids = ids

        def __call__(self, _ids):
            return FakeOut(self._ids)

    fake_ids = [labels.index(t) for t in
                ["B-LOCATION", "I-LOCATION", "O", "B-SYMPTOM", "O"]]
    monkeypatch.setattr(ner_tool, "_bundle", ({"<UNK>": 1}, labels, FakeModel(fake_ids)))
    # stub torch module used inside extract_entities
    import types

    fake_torch = types.SimpleNamespace(
        tensor=lambda x: x,
        no_grad=lambda: _NoGrad(),
    )

    class _NoGrad:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    monkeypatch.setitem(sys.modules, "torch", fake_torch)


def test_extract_with_mock_model(monkeypatch):
    labels = ["O", "B-LOCATION", "I-LOCATION", "B-SYMPTOM"]
    _fake_bundle(monkeypatch, labels)
    res = ner_tool.extract_entities("Hà Nội đẹp sốt cao")
    assert res["ok"] is True
    assert res["model"] == "bilstm-phoner"
    assert {"type": "LOCATION", "text": "Hà Nội"} in res["entities"]
    assert {"type": "SYMPTOM", "text": "sốt"} in res["entities"]


def test_tool_wrapper_no_tenant_check(monkeypatch):
    labels = ["O", "B-LOCATION", "I-LOCATION", "B-SYMPTOM"]
    _fake_bundle(monkeypatch, labels)
    fn = TOOL_REGISTRY["extract_entities"]
    # works with and without tenant_id, never unauthorized
    r1 = fn("Hà Nội đẹp sốt cao", tenant_id="tenant_A")
    r2 = fn("Hà Nội đẹp sốt cao", tenant_id="tenant_B")
    assert r1["ok"] and r2["ok"]
    assert r1["entities"] == r2["entities"]
    assert r1["tenant_id"] == "tenant_A"


def test_graceful_fallback_missing_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("MAIA_NER_CKPT_DIR", str(tmp_path / "nope"))
    monkeypatch.setattr(ner_tool, "_bundle", None)
    res = ner_tool.extract_entities("Hà Nội")
    assert res["ok"] is False
    assert "error" in res
    # wrapper propagates gracefully instead of raising
    r = TOOL_REGISTRY["extract_entities"]("Hà Nội")
    assert r["ok"] is False


def test_entity_filter_raises_precision():
    docs = [
        "Bệnh viện Bạch Mai ở Hà Nội tiếp nhận bệnh nhân COVID-19",
        "Thời tiết hôm nay nắng đẹp không liên quan",
        "Hà Nội mưa to chiều nay",
    ]
    ents = [{"type": "LOCATION", "text": "Hà Nội"}]

    def prec(ds):
        kw = ["hà", "nội"]
        return sum(1 for d in ds if any(k in d.lower() for k in kw)) / max(1, len(ds))

    filt = ner_tool.filter_docs_by_entities(docs, ents)
    assert len(filt) == 2
    assert prec(filt) >= prec(docs)
    assert prec(filt) == 1.0


def test_langchain_mirror():
    from maia.langchain.tools import TOOL_REGISTRY_LC

    assert "extract_entities" in TOOL_REGISTRY_LC
    assert TOOL_REGISTRY_LC["extract_entities"].name == "extract_entities"
