"""`build_llm()` must map providers CLOSED, not fail open.

Root cause: `build_llm` had one explicit branch (local) and a `return` for
everything else, so `LLM_PROVIDER=clodflare` silently became Cloudflare with
credentials, and `LLM_PROVIDER=mock` produced the SAME object as
`LLM_PROVIDER=cloudflare` -- the only difference being a `mode` property derived
from whether creds happened to be present. A typo in a deploy config was
invisible; "forced mock" was indistinguishable from "Cloudflare, silently
mocking".

The contract pinned here:
* every accepted provider name maps to its adapter, aliases included,
* an unknown name raises `LLMProviderConfigurationError` naming the accepted
  values, and does NOT construct anything,
* `mock` and `cloudflare` are distinguishable objects, so a caller can tell a
  deliberate mock from a real backend that happens to have no creds.

`LLMProviderConfigurationError` subclasses `ValueError` for the same reason
`BackendConfigurationError` does (`maia.retrieval_backends`): it is a wrong
configuration VALUE, not a missing dependency, and existing `except ValueError`
around settings validation keeps working.

Fully offline -- `build_llm` only constructs objects.
"""
from __future__ import annotations

import pytest

from maia import llm as llm_mod
from maia.config import settings
from maia.llm import (
    SUPPORTED_PROVIDERS,
    CloudflareLLM,
    LLMProviderConfigurationError,
    LocalOpenAICompatLLM,
    build_llm,
)


@pytest.fixture
def set_provider(monkeypatch):
    def _set(value: str) -> str:
        monkeypatch.setattr(settings, "LLM_PROVIDER", value)
        return value

    return _set


@pytest.fixture(autouse=True)
def no_network_on_construction(monkeypatch):
    """`build_llm` must not open a socket for any provider, including a typo."""
    def _boom(*_a, **_k):
        raise AssertionError("build_llm must not perform I/O")

    monkeypatch.setattr(llm_mod.requests, "post", _boom)


# --------------------------------------------------------------------------- #
# 1. accepted providers map to their adapter
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "name",
    ["local", "lmstudio", "local_openai", "ollama", "  LOCAL  "],
)
def test_local_family_maps_to_the_openai_compat_client(set_provider, name):
    assert isinstance(build_llm(), LocalOpenAICompatLLM)


def test_cloudflare_maps_to_the_cloudflare_client(set_provider):
    set_provider("cloudflare")
    llm = build_llm()
    assert isinstance(llm, CloudflareLLM)
    assert llm.provider == llm_mod.PROVIDER_CLOUDFLARE
    assert llm.account_id == settings.CLOUDFLARE_ACCOUNT_ID
    assert llm.api_token == settings.CLOUDFLARE_API_TOKEN


def test_mock_maps_to_a_client_that_can_never_reach_the_network(set_provider):
    """`mock` is FORCED mock: it must not become Cloudflare-with-credentials
    just because creds are present in the environment."""
    set_provider("mock")
    llm = build_llm()
    assert llm.mode == "mock"
    assert llm.generation_state == llm_mod.GENERATION_UNKNOWN
    out = llm.chat([{"role": "user", "content": "ping [S1]"}])
    assert out.startswith("(MOCK LLM")


def test_mock_ignores_present_credentials(set_provider, monkeypatch):
    """With creds configured, `mock` must STILL be mock. This is the exact
    conflation the defect describes: both names produced one identical object.
    """
    monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "token")
    set_provider("mock")
    llm = build_llm()

    assert llm.mode == "mock"
    assert llm.account_id == "" and llm.api_token == ""
    assert llm.chat([{"role": "user", "content": "ping"}]).startswith("(MOCK LLM")


def test_cloudflare_without_credentials_is_not_the_mock_object(set_provider, monkeypatch):
    monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "")
    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "")
    set_provider("cloudflare")
    cloudflare = build_llm()
    set_provider("mock")
    mock = build_llm()

    assert cloudflare.provider != mock.provider, (
        "a misconfigured cloudflare and a deliberate mock must be distinguishable"
    )
    assert type(cloudflare) is type(mock)  # same class is fine ...
    assert cloudflare.provider == llm_mod.PROVIDER_CLOUDFLARE
    assert mock.provider == llm_mod.PROVIDER_MOCK


# --------------------------------------------------------------------------- #
# 2. unknown providers FAIL LOUDLY
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("typo", ["clodflare", "CLOUDFLARE!", "openai", "", "  ", "qwen"])
def test_an_unknown_provider_raises_instead_of_becoming_cloudflare(set_provider, typo):
    set_provider(typo)
    with pytest.raises(LLMProviderConfigurationError) as exc:
        build_llm()
    message = str(exc.value)
    assert "LLM_PROVIDER" in message
    for name in SUPPORTED_PROVIDERS:
        assert name in message


def test_an_unknown_provider_constructs_nothing(monkeypatch):
    """Failing closed means no object escapes: a caller that catches the error
    must not be able to keep using a silently-wrong adapter."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "clodflare")
    monkeypatch.setattr(
        llm_mod, "CloudflareLLM",
        lambda *a, **k: pytest.fail("must not construct CloudflareLLM"),
    )
    monkeypatch.setattr(
        llm_mod, "LocalOpenAICompatLLM",
        lambda *a, **k: pytest.fail("must not construct LocalOpenAICompatLLM"),
    )
    with pytest.raises(LLMProviderConfigurationError):
        build_llm()


def test_a_typo_is_case_and_whitespace_insensitive_but_not_fuzzy(set_provider):
    """Normalisation is strip+lower (an env-file value may be padded or
    upper-case). It is NOT fuzzy matching -- `clodflare` must not resolve."""
    set_provider("lmstudio")
    assert isinstance(build_llm(), LocalOpenAICompatLLM)
    set_provider("lm-studio")
    with pytest.raises(LLMProviderConfigurationError):
        build_llm()


# --------------------------------------------------------------------------- #
# 3. the error type and the accepted set
# --------------------------------------------------------------------------- #

def test_provider_configuration_error_is_a_value_error():
    """A wrong configuration value, not a missing dependency."""
    assert issubclass(LLMProviderConfigurationError, ValueError)


def test_supported_providers_contains_the_settings_default():
    assert settings.LLM_PROVIDER in SUPPORTED_PROVIDERS
    for name in ("local", "lmstudio", "local_openai", "ollama",
                 "cloudflare", "mock"):
        assert name in SUPPORTED_PROVIDERS


def test_the_default_provider_still_builds():
    """The documented default (settings.LLM_PROVIDER) must keep working."""
    llm = build_llm()
    assert isinstance(llm, (LocalOpenAICompatLLM, CloudflareLLM))