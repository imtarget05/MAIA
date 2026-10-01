"""Fail-closed identity controls for TODO 5 (NC2/NC4/NC9/NC10, P11, P8).

The main contract (env fallback, KV read, 404/empty/malformed, SDK-absent,
redaction, MI pinning) lives in tests/test_azure_identity.py. This file pins
what it leaves open:

- NC2: on Azure, a missing AZURE_CLIENT_ID fails explicitly (ambient chain
  could otherwise resolve the wrong principal — silent 403).
- NC4: a generic Key Vault failure surfaces as SecretUnavailable.
- NC9: AZURE_CLIENT_SECRET (or any long-lived credential) is never read.
- NC10: Key Vault selected + Key Vault failing does NOT fall back to env,
  even when the env var holds a value.
- P11: ALLOW → DENY(403) → REVOKE → RESTORE application behavior with a
  stateful stub (no stale secret invented mid-revoke, recovery works).
- P8: SecretClient objects are reused per vault URI; secret VALUES are
  re-read on every resolve (rotation visible without restart).
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia import azure_identity as az


@pytest.fixture(autouse=True)
def _isolated_client_cache(monkeypatch):
    monkeypatch.setattr(az, "_secret_clients", {})
    yield
    az._secret_clients.clear()


def _production(monkeypatch, **overrides):
    monkeypatch.setattr(az.settings, "ENVIRONMENT", "production")
    for k, v in overrides.items():
        monkeypatch.setattr(az.settings, k, v)


def test_nc2_missing_client_id_fails_explicitly_on_azure(monkeypatch):
    _production(monkeypatch, AZURE_CLIENT_ID="", AZURE_TENANT_ID="",
                AZURE_KEY_VAULT_URI="")
    with pytest.raises(az.IdentityConfigurationError) as exc:
        az.azure_credential()
    assert "AZURE_CLIENT_ID" in str(exc.value)


def test_nc2_missing_client_id_never_checked_off_azure(monkeypatch):
    """Local dev keeps working with no identity configured at all."""
    monkeypatch.setattr(az.settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(az.settings, "AZURE_CLIENT_ID", "")
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    recorded: list = []

    class _FakeCred:
        def __init__(self, **kwargs):
            recorded.append(kwargs)

    fake = SimpleNamespace(DefaultAzureCredential=_FakeCred)
    monkeypatch.setitem(sys.modules, "azure", SimpleNamespace(identity=fake))
    monkeypatch.setitem(sys.modules, "azure.identity", fake)
    az.azure_credential()
    assert recorded == [{}]


def test_nc4_generic_key_vault_failure_is_a_domain_error(monkeypatch):
    monkeypatch.setattr(az.settings, "AZURE_KEY_VAULT_URI",
                        "https://v.vault.azure.net")

    class _BoomClient:
        def get_secret(self, name):
            raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(az, "_key_vault_secret_client", lambda: _BoomClient())
    with pytest.raises(az.SecretUnavailable) as exc:
        az.resolve_kv_secret("SMTP_PASSWORD")
    assert "SMTP_PASSWORD" in str(exc.value)
    assert "connection reset" not in str(exc.value)


def test_nc9_long_lived_client_secret_is_never_read(monkeypatch):
    """Even when set, AZURE_CLIENT_SECRET must not influence construction."""
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "hunter2-should-be-ignored")
    _production(monkeypatch, AZURE_CLIENT_ID="mi-client-id",
                AZURE_TENANT_ID="", AZURE_KEY_VAULT_URI="")
    recorded: list = []

    class _FakeCred:
        def __init__(self, **kwargs):
            recorded.append(kwargs)

    fake = SimpleNamespace(DefaultAzureCredential=_FakeCred)
    monkeypatch.setitem(sys.modules, "azure", SimpleNamespace(identity=fake))
    monkeypatch.setitem(sys.modules, "azure.identity", fake)
    az.azure_credential()
    assert recorded == [{"managed_identity_client_id": "mi-client-id"}]


def test_nc10_key_vault_failure_never_falls_back_to_env(monkeypatch):
    """The env var holds a usable value AND Key Vault is selected AND Key
    Vault fails: must raise, must not return the env value (that would make
    rotation ineffective and hide the outage)."""
    monkeypatch.setattr(az.settings, "AZURE_KEY_VAULT_URI",
                        "https://v.vault.azure.net")
    monkeypatch.setenv("JWT_SECRET_KEY", "stale-env-value")

    class _DeniedClient:
        def get_secret(self, name):
            e = RuntimeError("forbidden")
            e.status_code = 403
            raise e

    monkeypatch.setattr(az, "_key_vault_secret_client", lambda: _DeniedClient())
    with pytest.raises(az.SecretUnavailable):
        az.resolve_kv_secret("JWT_SECRET_KEY")


class _FlappingClient:
    """Stateful stub: ALLOW -> DENY(403) -> REVOKE persists -> RESTORE."""

    def __init__(self):
        self.mode = "allow"
        self.calls: list[str] = []

    def get_secret(self, name):
        self.calls.append(f"{self.mode}:{name}")
        if self.mode == "allow":
            return SimpleNamespace(value="live-value")
        e = RuntimeError("forbidden")
        e.status_code = 403
        raise e


def test_p11_allow_deny_revoke_restore(monkeypatch):
    monkeypatch.setattr(az.settings, "AZURE_KEY_VAULT_URI",
                        "https://v.vault.azure.net")
    monkeypatch.setenv("JWT_SECRET_KEY", "stale-env-value")
    flapping = _FlappingClient()
    monkeypatch.setattr(az, "_key_vault_secret_client", lambda: flapping)

    assert az.resolve_kv_secret("JWT_SECRET_KEY") == "live-value"

    flapping.mode = "deny"
    with pytest.raises(az.SecretUnavailable):
        az.resolve_kv_secret("JWT_SECRET_KEY")
    # Revoked: still failing on retry, and nothing stale invented.
    with pytest.raises(az.SecretUnavailable):
        az.resolve_kv_secret("JWT_SECRET_KEY")

    flapping.mode = "allow"
    assert az.resolve_kv_secret("JWT_SECRET_KEY") == "live-value"
    assert flapping.calls == [
        "allow:JWT_SECRET_KEY",
        "deny:JWT_SECRET_KEY",
        "deny:JWT_SECRET_KEY",
        "allow:JWT_SECRET_KEY",
    ]


def test_p8_secret_clients_are_reused_per_vault_uri(monkeypatch):
    monkeypatch.setattr(az, "azure_credential", lambda: SimpleNamespace())
    built: list = []

    class _FakeSecretClient:
        def __init__(self, vault_url, credential):
            built.append(vault_url)

    c1 = az._cached_secret_client("https://a.vault.azure.net", _FakeSecretClient)
    c2 = az._cached_secret_client("https://a.vault.azure.net", _FakeSecretClient)
    c3 = az._cached_secret_client("https://b.vault.azure.net", _FakeSecretClient)
    assert c1 is c2
    assert c3 is not c1
    assert built == ["https://a.vault.azure.net", "https://b.vault.azure.net"]


def test_p8_clients_are_not_reused_across_different_identities(monkeypatch):
    """STEP 2: the cache key must include the identity, not only the vault.

    The sibling test above proves a vault A client is never handed to a vault B
    call. That is necessary but not sufficient. `_secret_clients` is keyed by
    vault URL alone, so a caller that changes the user-assigned managed identity
    mid-process -- a redeploy with a rotated identity, a test, or a multi-tenant
    deployment sharing one worker -- would keep receiving the client built with
    the OLD identity. That is a privilege confusion, not a performance detail:
    the cached client authenticates as a principal the current configuration did
    not ask for.

    This fails against the vault-URL-only key and passes once the identity is
    part of the key.
    """
    monkeypatch.setattr(az, "azure_credential", lambda: SimpleNamespace())
    built: list[tuple[str, str]] = []

    class _FakeSecretClient:
        def __init__(self, vault_url, credential):
            built.append((vault_url, az.settings.AZURE_CLIENT_ID.strip()))

    vault = "https://same.vault.azure.net"

    monkeypatch.setattr(az.settings, "AZURE_CLIENT_ID", "identity-A")
    c1 = az._cached_secret_client(vault, _FakeSecretClient)

    monkeypatch.setattr(az.settings, "AZURE_CLIENT_ID", "identity-B")
    c2 = az._cached_secret_client(vault, _FakeSecretClient)

    assert c2 is not c1, (
        "a client built with identity-A was reused after the configured "
        "identity changed to identity-B; the cached client would authenticate "
        "as a principal the current configuration did not select"
    )
    assert [identity for _, identity in built] == ["identity-A", "identity-B"], built

    # ...and the same identity on the same vault still reuses, so the fix keys
    # on identity WITHOUT giving up the reuse the cache exists for.
    monkeypatch.setattr(az.settings, "AZURE_CLIENT_ID", "identity-A")
    c3 = az._cached_secret_client(vault, _FakeSecretClient)
    assert c3 is c1, (
        "reusing within one (vault, identity) pair is the point of the cache; "
        "a fix that disables reuse entirely would pass the test above and "
        "silently reinstate the per-call credential chain this avoids"
    )


def test_p8_secret_values_are_never_cached(monkeypatch):
    """Rotation must be visible without restart: every resolve re-reads."""
    monkeypatch.setattr(az.settings, "AZURE_KEY_VAULT_URI",
                        "https://v.vault.azure.net")
    reads: list = []

    class _RotatingClient:
        def get_secret(self, name):
            reads.append(name)
            return SimpleNamespace(value=f"value-{len(reads)}")

    monkeypatch.setattr(az, "_key_vault_secret_client", lambda: _RotatingClient())
    assert az.resolve_kv_secret("SMTP_PASSWORD") == "value-1"
    assert az.resolve_kv_secret("SMTP_PASSWORD") == "value-2"
    assert reads == ["SMTP_PASSWORD", "SMTP_PASSWORD"]
