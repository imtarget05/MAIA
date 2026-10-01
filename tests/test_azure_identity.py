"""Secretless credential + Key Vault resolution: the dev/cloud parity contract.

What is actually worth testing here is not `DefaultAzureCredential` (Azure's
test), it is the boundary MAIA owns:

  * no network — and above all no credential construction — at import time,
    because a module-level `DefaultAzureCredential()` is the fastest way to
    hang `/health` in a container with no identity;
  * `resolve_kv_secret` reads Key Vault or the process env depending on
    `AZURE_KEY_VAULT_URI`, with the SAME argument in both environments;
  * a missing SDK is a domain error, not an ImportError traceback;
  * the secret-name list is non-empty, unique, and contains the name
    `maia.persistence` already reads (`MAIA_POSTGRES_DSN`).
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia import azure_identity as az
from maia.persistence import ENV_DSN


@pytest.fixture(autouse=True)
def no_kv(monkeypatch):
    """Every test starts in the dev (no-vault) configuration."""
    monkeypatch.setattr(az.settings, "AZURE_KEY_VAULT_URI", "")
    monkeypatch.setattr(az.settings, "ENVIRONMENT", "development")


# --------------------------------------------------------------------------- #
# 1. the secret-name contract shared with the Bicep side
# --------------------------------------------------------------------------- #
def test_kv_secret_names_is_non_empty_and_unique():
    names = az.kv_secret_names()
    assert names, "the Key Vault contract must not be an empty list"
    assert len(names) == len(set(names)), f"duplicate secret names: {names}"


def test_kv_secret_names_contains_the_dsn_persistence_already_reads():
    """Two lists for the same DSN is how they drift. persistence.ENV_DSN must
    be one of the names, not a parallel spelling."""
    assert ENV_DSN in az.kv_secret_names()


def test_kv_secret_names_are_valid_key_vault_names():
    """Key Vault allows only [-0-9a-zA-Z_]; a dot or a slash would fail at
    provision time, not here, which is the expensive way to find out."""
    for name in az.kv_secret_names():
        assert all(ch.isalnum() or ch == "-" or ch == "_" for ch in name), name


def test_kv_secret_names_is_immutable_for_its_callers():
    """The point is that a caller holding the result cannot corrupt the
    contract for everybody else — not that it is a distinct object."""
    names = az.kv_secret_names()
    assert isinstance(names, tuple)
    with pytest.raises(TypeError):
        names[0] = "something-else"  # type: ignore[index]


# --------------------------------------------------------------------------- #
# 2. the environment fallback — the "dev container" path
# --------------------------------------------------------------------------- #
def test_resolve_kv_secret_falls_back_to_the_process_environment(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "dev-secret")
    assert az.resolve_kv_secret("JWT_SECRET_KEY") == "dev-secret"


def test_resolve_kv_secret_raises_when_neither_source_has_the_value(monkeypatch):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with pytest.raises(az.SecretUnavailable) as exc:
        az.resolve_kv_secret("JWT_SECRET_KEY")
    # The message must name the secret and mention the Key Vault switch,
    # because "it didn't work" is not an actionable error.
    assert "JWT_SECRET_KEY" in str(exc.value)
    assert "AZURE_KEY_VAULT_URI" in str(exc.value)


def test_resolve_kv_secret_never_returns_an_empty_string(monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "")
    with pytest.raises(az.SecretUnavailable):
        az.resolve_kv_secret("SMTP_PASSWORD")


# --------------------------------------------------------------------------- #
# 3. the Key Vault path — the "Azure" path, with a stub client
# --------------------------------------------------------------------------- #
def test_resolve_kv_secret_reads_key_vault_when_the_uri_is_set(monkeypatch):
    monkeypatch.setattr(az.settings, "AZURE_KEY_VAULT_URI",
                        "https://maia-kv.vault.azure.net")
    monkeypatch.setenv("JWT_SECRET_KEY", "stale-env-value")
    seen: dict[str, object] = {}

    class _StubClient:
        def __init__(self) -> None:
            seen["vault_url"] = az.settings.AZURE_KEY_VAULT_URI
            seen["credential"] = "managed-identity"

        def get_secret(self, name: str):
            seen["name"] = name
            return SimpleNamespace(value="vault-value")

    monkeypatch.setattr(az, "_key_vault_secret_client", lambda: _StubClient())
    assert az.resolve_kv_secret("JWT_SECRET_KEY") == "vault-value"
    assert seen["name"] == "JWT_SECRET_KEY"
    # Key Vault wins over the environment even when the env var is set:
    # otherwise a rotated secret is shadowed by a stale container config.
    assert seen["vault_url"] == "https://maia-kv.vault.azure.net"
    assert seen["credential"] == "managed-identity"


def test_key_vault_404_becomes_a_domain_error(monkeypatch):
    """A not-provisioned secret is a configuration fact, not an SDK type."""
    monkeypatch.setattr(az.settings, "AZURE_KEY_VAULT_URI", "https://v.vault.azure.net")

    class _StubClient:
        def get_secret(self, name: str):
            raise KeyError(f"SecretNotFound: {name}")

    monkeypatch.setattr(az, "_key_vault_secret_client", lambda: _StubClient())
    with pytest.raises(az.SecretUnavailable) as exc:
        az.resolve_kv_secret("GOOGLE_CLIENT_SECRET")
    assert "GOOGLE_CLIENT_SECRET" in str(exc.value)


def test_an_empty_key_vault_secret_is_an_error(monkeypatch):
    monkeypatch.setattr(az.settings, "AZURE_KEY_VAULT_URI", "https://v.vault.azure.net")
    monkeypatch.setattr(
        az, "_key_vault_secret_client",
        lambda: SimpleNamespace(get_secret=lambda name: SimpleNamespace(value="")),
    )
    with pytest.raises(az.SecretUnavailable):
        az.resolve_kv_secret("MAIA_POSTGRES_DSN")


# --------------------------------------------------------------------------- #
# 4. the missing-SDK path
# --------------------------------------------------------------------------- #
def test_azure_credential_raises_a_domain_error_when_the_sdk_is_absent(monkeypatch):
    # `None` in sys.modules makes the import fail the way a missing package
    # does, without uninstalling anything.
    monkeypatch.setitem(sys.modules, "azure", None)
    monkeypatch.setitem(sys.modules, "azure.identity", None)
    with pytest.raises(az.AzureIdentityUnavailable) as exc:
        az.azure_credential()
    # The message must carry the install line: this exception is what a
    # developer hits, and a bare ImportError does not say what to do.
    assert "azure-identity" in str(exc.value)


def test_key_vault_client_raises_a_domain_error_when_the_sdk_is_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "azure", None)
    monkeypatch.setitem(sys.modules, "azure.keyvault", None)
    monkeypatch.setattr(az.settings, "AZURE_KEY_VAULT_URI", "https://v.vault.azure.net")
    with pytest.raises(az.AzureIdentityUnavailable) as exc:
        az.resolve_kv_secret("JWT_SECRET_KEY")
    assert "azure-keyvault-secrets" in str(exc.value)


# --------------------------------------------------------------------------- #
# 5. no import-time credential construction
# --------------------------------------------------------------------------- #
def test_importing_the_module_constructs_no_credential(monkeypatch):
    """The regression this guards is a module-level
    `DefaultAzureCredential()`: it probes IMDS, so in a container without an
    identity it blocks for seconds before failing — on the import path, i.e.
    before /health can answer."""
    constructed: list[str] = []

    class _ExplodingCredential:
        def __init__(self, *a, **k):
            constructed.append("built")
            raise AssertionError("a credential was constructed at import time")

    fake = SimpleNamespace(DefaultAzureCredential=_ExplodingCredential)
    monkeypatch.setitem(sys.modules, "azure", SimpleNamespace(identity=fake))
    monkeypatch.setitem(sys.modules, "azure.identity", fake)

    importlib.reload(az)  # the import path under test

    assert constructed == []
    # ...and the reloaded module is still the one callers get.
    assert callable(az.resolve_kv_secret)


# --------------------------------------------------------------------------- #
# 6. dev vs cloud credential selection
# --------------------------------------------------------------------------- #
def test_running_on_azure_is_false_for_a_development_environment(monkeypatch):
    monkeypatch.setattr(az.settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(az.settings, "AZURE_KEY_VAULT_URI", "")
    assert az.running_on_azure() is False
    monkeypatch.setattr(az.settings, "ENVIRONMENT", "test")
    assert az.running_on_azure() is False


def test_running_on_azure_is_true_for_a_production_environment(monkeypatch):
    monkeypatch.setattr(az.settings, "ENVIRONMENT", "production")
    assert az.running_on_azure() is True


def test_running_on_azure_is_true_when_only_the_vault_uri_is_set(monkeypatch):
    """A deployment that forgets ENVIRONMENT must still get the cloud path."""
    monkeypatch.setattr(az.settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(az.settings, "AZURE_KEY_VAULT_URI", "https://v.vault.azure.net")
    assert az.running_on_azure() is True


def _install_fake_identity(monkeypatch) -> list[dict]:
    """Put a fake `azure.identity` in sys.modules; return the recorded kwargs."""
    recorded: list[dict] = []

    class _FakeDefaultAzureCredential:
        def __init__(self, **kwargs):
            recorded.append(kwargs)

    fake = SimpleNamespace(DefaultAzureCredential=_FakeDefaultAzureCredential)
    monkeypatch.setitem(sys.modules, "azure", SimpleNamespace(identity=fake))
    monkeypatch.setitem(sys.modules, "azure.identity", fake)
    return recorded


def test_managed_identity_client_id_is_pinned_on_azure(monkeypatch):
    """Without the pin, DefaultAzureCredential resolves the SYSTEM-assigned
    identity — a different principal with different role assignments, which
    surfaces as a silent 403 rather than a useful error."""
    recorded = _install_fake_identity(monkeypatch)
    monkeypatch.setattr(az.settings, "AZURE_CLIENT_ID", "mi-client-id")
    monkeypatch.setattr(az.settings, "ENVIRONMENT", "production")
    az.azure_credential()
    assert recorded == [{"managed_identity_client_id": "mi-client-id"}]


def test_off_azure_the_ambient_chain_is_used(monkeypatch):
    """A developer runs `az login`; forcing a managed identity there would make
    local runs impossible, so AZURE_CLIENT_ID is ignored off Azure."""
    recorded = _install_fake_identity(monkeypatch)
    monkeypatch.setattr(az.settings, "AZURE_CLIENT_ID", "mi-client-id")
    monkeypatch.setattr(az.settings, "ENVIRONMENT", "development")
    az.azure_credential()
    assert recorded == [{}]


# --------------------------------------------------------------------------- #
# 7. import safety, proven in a clean interpreter
# --------------------------------------------------------------------------- #
def test_the_three_new_modules_import_with_the_azure_sdk_unavailable():
    """`import maia.retrieval_backends` must not need the Azure SDK.

    requirements.api.txt deliberately does NOT ship azure-*, so on a real ACA
    container these modules are imported with the SDK absent. A module-level
    `from azure.identity import DefaultAzureCredential` would turn that into a
    failed startup; a lazy import inside a function is a domain error at the
    point of use instead. Run in a subprocess so a module already imported by
    an earlier test in this file cannot mask the result.
    """
    import subprocess
    import textwrap

    repo = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(
        """
        import sys

        class _BlockAzure:
            def find_spec(self, name, path=None, target=None):
                if name == "azure" or name.startswith("azure."):
                    raise ImportError("blocked: " + name)
                return None

        sys.meta_path.insert(0, _BlockAzure())
        import maia.retrieval_port, maia.retrieval_backends, maia.azure_identity
        from maia.retrieval_backends import AzureAISearchAdapter, AzureSearchUnavailable
        try:
            AzureAISearchAdapter(endpoint="https://x.search.windows.net", index_name="i")
        except AzureSearchUnavailable as exc:
            print("ok", exc)
        else:
            raise SystemExit("expected AzureSearchUnavailable")
        """
    )
    env = {**dict(os.environ), "PYTHONPATH": str(repo / "src")}
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, cwd=str(repo), check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("ok "), result.stdout


# --------------------------------------------------------------------------- #
# 7b. L3: importing the adapters must not drag maia.test_utils in
# --------------------------------------------------------------------------- #
def test_importing_the_adapters_does_not_load_the_test_utils_module():
    """`backend="memory"` is a supported production setting, so
    `build_vector_store` returns `maia.test_utils.InMemoryVectorStore` -- but
    the import is LAZY, so a production boot (no Azure SDK, no test modules)
    never puts a test module into `sys.modules`.

    Also proves the offline/memory path still works with every `azure.*` import
    blocked, which is the profile the ACA container actually runs.
    """
    import subprocess
    import textwrap

    repo = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(
        """
        import sys

        class _BlockAzure:
            def find_spec(self, name, path=None, target=None):
                if name == "azure" or name.startswith("azure."):
                    raise ImportError("blocked: " + name)
                return None

        sys.meta_path.insert(0, _BlockAzure())

        import maia.retrieval_backends as rb
        assert "maia.test_utils" not in sys.modules, "test_utils imported eagerly"

        store = rb.build_vector_store(dim=8, backend="memory")
        assert type(store).__name__ == "InMemoryVectorStore", type(store)
        # ...and only now is it loaded, because it was actually requested.
        assert "maia.test_utils" in sys.modules

        # The Azure branch still reports a missing SDK as a domain error.
        # (Endpoint/index come from the subprocess env below: `settings` is
        # read at import time, so setting os.environ after the import would
        # come too late.)
        try:
            rb.build_vector_store(dim=8, backend="azure_ai_search")
        except rb.AzureSearchUnavailable:
            pass
        else:
            raise SystemExit("expected AzureSearchUnavailable")
        print("ok")
        """
    )
    env = {
        **dict(os.environ),
        "PYTHONPATH": str(repo / "src"),
        "AZURE_AI_SEARCH_ENDPOINT": "https://maia.search.windows.net",
        "AZURE_AI_SEARCH_INDEX": "maia_knowledge",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, cwd=str(repo), check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "ok", result.stdout


# --------------------------------------------------------------------------- #
# 8. H2: an SDK exception message must not reach the caller
# --------------------------------------------------------------------------- #
class _LeakySdkError(Exception):
    """Stands in for `azure.core.exceptions.HttpResponseError`.

    The real class's `__str__` embeds the request URL and, often, the response
    body -- so interpolating `{exc}` puts whatever the service echoed back into
    the exception message.
    """

    status_code = 403

    def __str__(self) -> str:
        return (
            "Code=Forbidden\n"
            "Message: Key Vault secret is not accessible\n"
            "{'id': 'https://v.vault.azure.net/secrets/MAIA_POSTGRES_DSN/1', "
            "'value': 'postgres://maia:hunter2@db:5432/maia'}"
        )


def test_a_key_vault_error_message_never_contains_the_sdk_error_text(monkeypatch):
    """The reviewer reproduced a `SecretUnavailable` carrying a DSN with a
    password in it, straight from `{exc}`. The message goes on to be logged and
    returned, so the interpolation is a secret leak."""
    monkeypatch.setattr(az.settings, "AZURE_KEY_VAULT_URI",
                        "https://maia-kv.vault.azure.net")

    class _StubClient:
        def get_secret(self, name: str):
            raise _LeakySdkError()

    monkeypatch.setattr(az, "_key_vault_secret_client", lambda: _StubClient())
    with pytest.raises(az.SecretUnavailable) as exc:
        az.resolve_kv_secret("MAIA_POSTGRES_DSN")

    message = str(exc.value)
    assert "hunter2" not in message, message
    assert "postgres://" not in message, message
    assert "Code=Forbidden" not in message, message
    # What IS in the message: enough to act on. Which secret, which vault,
    # which error class, and the status code -- all of which are non-secret.
    assert "MAIA_POSTGRES_DSN" in message
    assert "maia-kv.vault.azure.net" in message
    assert "_LeakySdkError" in message
    assert "403" in message


def test_the_original_exception_is_kept_as_the_cause(monkeypatch):
    """Dropping `{exc}` must not cost the traceback.

    `logging.exception` prints the ORIGINAL message too, which is exactly why
    the underlying SDK error has to be treated as sensitive too -- the code
    comment above the raise says so, and this test is what keeps `from exc`
    from being dropped as "redundant".
    """
    monkeypatch.setattr(az.settings, "AZURE_KEY_VAULT_URI",
                        "https://maia-kv.vault.azure.net")

    class _StubClient:
        def get_secret(self, name: str):
            raise _LeakySdkError()

    monkeypatch.setattr(az, "_key_vault_secret_client", lambda: _StubClient())
    with pytest.raises(az.SecretUnavailable) as exc:
        az.resolve_kv_secret("JWT_SECRET_KEY")
    assert isinstance(exc.value.__cause__, _LeakySdkError)
    assert exc.value.__cause__ is not None


def test_a_sdk_error_without_a_status_code_still_names_the_type(monkeypatch):
    """`status_code` is best-effort: not every SDK error has one, and a missing
    status must not turn into a crash inside the error path."""
    monkeypatch.setattr(az.settings, "AZURE_KEY_VAULT_URI",
                        "https://maia-kv.vault.azure.net")

    class _NoStatus(Exception):
        pass

    class _StubClient:
        def get_secret(self, name: str):
            raise _NoStatus("boom, with a url in it: https://v.vault.azure.net")

    monkeypatch.setattr(az, "_key_vault_secret_client", lambda: _StubClient())
    with pytest.raises(az.SecretUnavailable) as exc:
        az.resolve_kv_secret("GOOGLE_CLIENT_SECRET")
    assert "_NoStatus" in str(exc.value)
    assert "HTTP" not in str(exc.value), "no status means no status suffix"
    assert "boom" not in str(exc.value)


# --------------------------------------------------------------------------- #
# 9. L1: AZURE_TENANT_ID is read, or it is removed
# --------------------------------------------------------------------------- #
def test_azure_tenant_id_is_pinned_on_azure(monkeypatch):
    """`AZURE_TENANT_ID` existed in `config.py` and `.env.example` while NOTHING
    read it, which is the worst of both worlds: documentation implying an
    effect the code does not have. It is now passed to the credential chain on
    the cloud path only, matching what `.env.example` already claimed."""
    recorded = _install_fake_identity(monkeypatch)
    monkeypatch.setattr(az.settings, "AZURE_CLIENT_ID", "mi-client-id")
    monkeypatch.setattr(az.settings, "AZURE_TENANT_ID", "mi-tenant-id")
    monkeypatch.setattr(az.settings, "ENVIRONMENT", "production")
    az.azure_credential()
    assert recorded == [{
        "managed_identity_client_id": "mi-client-id",
        "tenant_id": "mi-tenant-id",
    }]


def test_azure_tenant_id_alone_is_not_enough_on_azure(monkeypatch):
    """Tightened in TODO 5: a tenant pins the AUTHORITY but not the IDENTITY,
    so tenant-alone still risks the system-assigned principal (silent 403).
    TODO 3 selected user-assigned identity, therefore the client id is
    required on the cloud path — pinning the tenant without pinning WHO
    authenticates is half a fix."""
    recorded = _install_fake_identity(monkeypatch)
    monkeypatch.setattr(az.settings, "AZURE_CLIENT_ID", "")
    monkeypatch.setattr(az.settings, "AZURE_TENANT_ID", "mi-tenant-id")
    monkeypatch.setattr(az.settings, "ENVIRONMENT", "production")
    with pytest.raises(az.IdentityConfigurationError):
        az.azure_credential()
    assert recorded == []


def test_off_azure_the_tenant_id_is_ignored_too(monkeypatch):
    """`.env.example` says both Azure identity fields are ignored off Azure, so
    a developer running `az login` must not be pinned to a tenant from a stale
    env file."""
    recorded = _install_fake_identity(monkeypatch)
    monkeypatch.setattr(az.settings, "AZURE_CLIENT_ID", "mi-client-id")
    monkeypatch.setattr(az.settings, "AZURE_TENANT_ID", "mi-tenant-id")
    monkeypatch.setattr(az.settings, "ENVIRONMENT", "development")
    az.azure_credential()
    assert recorded == [{}]


def test_the_tenant_field_still_exists_in_settings():
    """The field is now CONSUMED, not removed -- so the `.env.example` block
    that documents it is accurate and nothing needs deleting. The point of the
    L1 decision was to pick ONE of the two options and be consistent; this test
    is what makes "we wired it" a fact rather than an intention."""
    assert hasattr(az.settings, "AZURE_TENANT_ID")
    # and the module actually reads it -- grep-proof, so a future refactor that
    # drops the read cannot pass by accident.
    import inspect as _inspect

    source = _inspect.getsource(az._default_azure_credential)
    assert "AZURE_TENANT_ID" in source


# --------------------------------------------------------------------------- #
# 10. the Bicep list cannot drift -- once infra/ exists
# --------------------------------------------------------------------------- #
def _bicep_files() -> list:
    repo = Path(__file__).resolve().parents[1]
    infra = repo / "infra"
    if not infra.is_dir():
        return []
    files = sorted(infra.rglob("*.bicep"))
    params = infra / "parameters"
    if params.is_dir():
        files += sorted(params.rglob("*.bicepparam"))
    return files


@pytest.mark.skipif(
    not _bicep_files(),
    reason="infra/ has no Bicep module in this working tree yet",
)
def test_the_python_secret_list_matches_the_bicep_key_vault_secrets():
    """DOCUMENTED SKIP while two secret lists legitimately coexist.

    `KV_SECRET_NAMES` (7 names) is the FUTURE resolver contract: secrets the
    app will resolve from Key Vault after the migration wave. Bicep's
    `keyVaultSecretNames` (demo-user-a-pw/demo-user-b-pw) is the CURRENT ACA
    secret-ref wiring: names the running container resolves TODAY. Forcing
    equality now would either provision unneeded vault secrets or break ACA
    startup on unprovisioned references. Convergence happens at migration,
    when this test must be re-armed to compare the blocks (see the
    block-scoped collection below, kept working so re-arming is a delete).
    """
    import re as _re

    bicep_names: set[str] = set()
    pattern = _re.compile(
        r"keyVaultSecrets\s*[:=]", )
    block_pattern = _re.compile(
        r"keyVaultSecretNames\s*=\s*\[(.*?)\]", _re.DOTALL)
    for path in _bicep_files():
        text = path.read_text(encoding="utf-8")
        for block in block_pattern.findall(text):
            bicep_names.update(
                _re.findall(r"'([A-Z][A-Z0-9_]{2,})'", block))
        if not pattern.search(text):
            continue
        # Collect every quoted string that names a secret in that block.
        bicep_names.update(
            m for m in _re.findall(r"['\"]([A-Z][A-Z0-9_]{2,})['\"]", text)
        )
    if not bicep_names:
        pytest.skip(
            "no MAIA-style secret names in Bicep yet: the current "
            "keyVaultSecretNames blocks carry the live ACA demo secrets, "
            "while KV_SECRET_NAMES is the future resolver contract. "
            "Re-arm at migration by deleting this skip."
        )
    assert set(az.kv_secret_names()) == bicep_names
