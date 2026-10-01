"""Secretless credential + Key Vault resolution for Azure hosting.

Vì sao một module riêng thay vì rải `DefaultAzureCredential` khắp `src/`:

  * **Không có network lúc import.** `DefaultAzureCredential()` ở cấp module
    là cái nhanh nhất để làm hỏng `/health`: nó probe IMDS, và container
    không có identity sẽ treo vài giây rồi mới raise. Ở đây credential chỉ
    được dựng khi thật sự cần, đúng pattern lazy import của
    `maia.persistence`.
  * **Một code path, hai môi trường.** `resolve_kv_secret(name)` đọc Key
    Vault khi `AZURE_KEY_VAULT_URI` có giá trị, nếu không thì đọc biến môi
    trường cùng tên. Dev container (`.env`) và Azure Container Apps (managed
    identity) vì thế chạy đúng một nhánh code — không có `if is_azure()`
    rải rác ở từng chỗ gọi.
  * **Domain error thay vì SDK error.** Caller không nên phải `except`
    `azure.core.exceptions.AzureError`; `maia.persistence.PersistenceUnavailable`
    là tiền lệ cho cách làm này và file này theo đúng tiền lệ đó.

Tên secret trong Key Vault CỐ TÌNH trùng tên biến môi trường của MAIA. Nhờ
vậy `resolve_kv_secret("JWT_SECRET_KEY")` là đúng ở cả hai môi trường mà
không cần bảng ánh xạ nào — và bảng ánh xạ chính là chỗ dễ lệch nhất khi
thêm secret mới. Danh sách `kv_secret_names()` phải khớp với
`keyVaultSecrets` trong `infra/` (Bicep); xem `docs/azure-integration.md`.

`resolve_kv_secret` is the DOCUMENTED ENTRY POINT, and it has NO caller in
`src/` yet. Stated plainly so nobody has to guess whether that is an
oversight:

  * `requirements.api.txt` deliberately does NOT ship
    `azure-keyvault-secrets`, so wiring this into the import path of
    `maia.persistence` / `maia.api` would turn a lightweight profile into a
    hard dependency and break `/health` on an ACA container.
  * The V3/V4 call sites -- reading `JWT_SECRET_KEY` / `MAIA_POSTGRES_DSN`
    from Key Vault instead of the process environment -- call THIS function
    with the same single argument in both environments. That is the whole
    reason the module exists.

Until then this is a public, tested API awaiting its wiring. Do not use it
at module scope, and do not read "no caller yet" as "dead code".
"""

from __future__ import annotations

import os
from typing import Any, Final

from .config import settings

#: Mirrors config._DEVELOPMENT_ENVIRONMENTS. Duplicated rather than imported
#: because that name is private to config.py, and config.py is meant to stay a
#: leaf module: it must not import anything that can fail. The deployment
#: signal is the same one config.py already uses to fail fast on a SQLite auth
#: DB, so both modules agree on what "deployed" means.
_DEVELOPMENT_ENVIRONMENTS: Final[frozenset[str]] = frozenset(
    {"", "dev", "development", "local", "test"}
)

#: Key Vault secret names MAIA expects to be provisioned, in Bicep's order.
#:
#: These are MAIA ENV VAR NAMES verbatim, not a parallel naming scheme — see
#: the module docstring. Anything NOT listed here is either non-secret
#: (endpoints, index names, collection names) or covered by managed identity
#: (the Key Vault data-plane call itself, Azure AI Search), so it must not be
#: provisioned as a secret at all.
#:
#: MUST stay in sync with `keyVaultSecrets` in `infra/`. MAIA_POSTGRES_DSN is
#: `persistence.ENV_DSN`, and that single name is asserted equal in
#: tests/test_azure_identity.py.
#:
#: Drift status, stated precisely because an earlier version of this comment
#: claimed the two lists "cannot silently drift" while NO test enforced it:
#: `infra/` does not exist in this working tree, so the list-equality check is
#: `skipif`-guarded on the Bicep file being present. It becomes a real check
#: the day the module lands, and it does NOT run today. Until then the sync
#: between this tuple and the Bicep is a review convention, not an enforced
#: invariant.
KV_SECRET_NAMES: Final[tuple[str, ...]] = (
    "JWT_SECRET_KEY",
    "AUTH_DB_URL",
    "QDRANT_API_KEY",
    "MAIA_POSTGRES_DSN",
    "AZURE_AI_SEARCH_API_KEY",
    "SMTP_PASSWORD",
    "GOOGLE_CLIENT_SECRET",
)


class AzureIdentityUnavailable(RuntimeError):
    """Cần azure-identity nhưng nó không được cài.

    Optional dep: MAIA chạy offline/dev không cần Azure SDK nào cả. Đây là
    domain error để caller xử lý được, không phải ImportError traceback.
    """


class SecretUnavailable(RuntimeError):
    """Không resolve được một secret từ Key Vault lẫn từ environment.

    Nêu rõ secret nào và đã thử cả hai nguồn, vì "thiếu cấu hình" và "sai tên
    secret trong vault" là hai lỗi khác nhau cần hai cách sửa khác nhau.
    """


def kv_secret_names() -> tuple[str, ...]:
    """Các secret name mà phía Bicep được kỳ vọng provision.

    Luôn trả về `tuple` bất biến, kể cả nếu constant sau này bị đổi thành
    list: caller giữ tham chiếu này không bao giờ sở hữu được thứ gì sửa
    được. Xem `tests/test_azure_identity.py` — có một test so sánh list này với
    `keyVaultSecrets` trong `infra/`, nhưng nó `skipif` khi `infra/` chưa tồn
    tại, nên HIỆN TẠI đồng bộ hai list chỉ là quy ước review chứ chưa được ép.
    """
    return tuple(KV_SECRET_NAMES)


def running_on_azure() -> bool:
    """True khi process đang chạy trên hạ tầng Azure (không phải dev).

    Dùng `ENVIRONMENT` vì đó là tín hiệu deploy mà chính `config.py` đã dùng
    để fail fast — hai module cùng đọc một tín hiệu thì không thể lệch nhau.
    """
    if settings.ENVIRONMENT.strip().lower() not in _DEVELOPMENT_ENVIRONMENTS:
        return True
    # Key Vault URI chỉ được set trong cloud, nên nó là tín hiệu dự phòng cho
    # một deployment quên set ENVIRONMENT.
    return bool(settings.AZURE_KEY_VAULT_URI.strip())


def _default_azure_credential() -> Any:
    """`DefaultAzureCredential`, pinned to the managed identity when on Azure.

    Trả về `Any` vì `azure-identity` là optional dep: type nó ở đây sẽ biến
    một module dependency-light thành module hard-dependency. Đây là điểm
    duy nhất trong repo được phép chạm vào kiểu Azure SDK; mọi thứ khác đi
    qua hai hàm public của file này.
    """
    try:
        from azure.identity import (  # pyright: ignore[reportMissingImports] - optional dep; ImportError becomes AzureIdentityUnavailable below
            DefaultAzureCredential,
        )
    except ImportError as exc:  # pragma: no cover - depends on local install
        raise AzureIdentityUnavailable(
            "Thiếu azure-identity. Cài: pip install 'azure-identity>=1.25'"
        ) from exc

    client_id = settings.AZURE_CLIENT_ID.strip()
    tenant_id = settings.AZURE_TENANT_ID.strip()
    # On Azure, AZURE_CLIENT_ID is the user-assigned managed identity. Pin it
    # explicitly: without this, DefaultAzureCredential resolves the
    # SYSTEM-assigned identity, which is a different principal with different
    # role assignments -- a silent 403, not a helpful error. AZURE_TENANT_ID is
    # pinned for the same reason: without a tenant the chain resolves against
    # the wrong AAD authority, which surfaces as an opaque credential error (or,
    # worse, succeeds against a tenant the deployment was never granted into).
    # Off Azure the ambient chain is what a developer actually wants (`az
    # login`), and forcing a managed identity there would make local runs
    # impossible -- so BOTH are ignored off Azure, exactly as `.env.example`
    # already documents. That is also why the field was not removed: it is
    # read, on the cloud path only.
    if running_on_azure() and (client_id or tenant_id):
        kwargs: dict[str, str] = {}
        if client_id:
            kwargs["managed_identity_client_id"] = client_id
        if tenant_id:
            kwargs["tenant_id"] = tenant_id
        return DefaultAzureCredential(**kwargs)
    return DefaultAzureCredential()


def azure_credential() -> Any:
    """Credential cho Azure data-plane calls (Key Vault, AI Search).

    Caller nên gọi MỘT LẦN lúc khởi động rồi tái sử dụng — mỗi lần gọi dựng
    lại một chuỗi credential chain, và chain đó gọi IMDS khi thất bại.
    """
    return _default_azure_credential()


def _key_vault_secret_client() -> Any:
    """`SecretClient` cho vault đang cấu hình. `Any`: optional dep, xem trên."""
    try:
        from azure.keyvault.secrets import (  # pyright: ignore[reportMissingImports] - optional dep; ImportError becomes AzureIdentityUnavailable below
            SecretClient,
        )
    except ImportError as exc:  # pragma: no cover - depends on local install
        raise AzureIdentityUnavailable(
            "Thiếu azure-keyvault-secrets. Cài: "
            "pip install 'azure-keyvault-secrets>=4.11'"
        ) from exc
    return SecretClient(
        vault_url=settings.AZURE_KEY_VAULT_URI.strip(),
        credential=azure_credential(),
    )


def resolve_kv_secret(name: str) -> str:
    """Đọc secret `name` từ Key Vault, fallback về biến môi trường cùng tên.

    Key Vault thắng khi `AZURE_KEY_VAULT_URI` có giá trị, kể cả khi biến môi
    trường cũng đang set. Lý do: nếu env thắng thì một secret đã bị rotate sẽ
    vẫn được đọc từ giá trị cũ trong container config, và sự xoay vòng trở
    nên không có tác dụng. Ở ACA, app dùng cơ chế này chứ không dùng
    key-vault reference của ACA — reference đẩy secret vào env, tức là đúng
    cái mà đoạn trên cố tình bỏ qua.

    Ném `SecretUnavailable` khi cả hai nguồn rỗng; không bao giờ trả về chuỗi
    rỗng, vì chuỗi rỗng ở đây sẽ biến thành credential sai một cách âm thầm.
    """
    vault_uri = settings.AZURE_KEY_VAULT_URI.strip()
    if not vault_uri:
        value = os.environ.get(name, "")
        if value:
            return value
        raise SecretUnavailable(
            f"Thiếu secret {name!r}. Chưa bật Key Vault "
            f"(AZURE_KEY_VAULT_URI rỗng) nên cần set {name} trong môi "
            f"trường; nếu chạy trên Azure thì cần provision secret này trong "
            f"vault và bật AZURE_KEY_VAULT_URI."
        )

    try:
        secret = _key_vault_secret_client().get_secret(name)
    except AzureIdentityUnavailable:
        raise
    except Exception as exc:  # every SDK failure becomes SecretUnavailable
        # Key Vault 404 (`ResourceNotFoundError`) means "not provisioned",
        # which is a configuration fact the caller must see, not an SDK type
        # to handle. Everything else (403 role, network, 429) is equally a
        # "secret unavailable" from the caller's point of view.
        #
        # ONLY the exception TYPE and the HTTP status go into the message.
        # `{exc}` is deliberately absent:
        # `azure.core.exceptions.HttpResponseError.__str__` embeds the request
        # URL and sometimes the response body, so interpolating it is a secret
        # leak. A reviewer reproduced a `SecretUnavailable` whose message
        # contained `{'value':'postgres://maia:hunter2@db:5432/maia'}` -- a
        # DSN with the password in it, in a message that then travels into an
        # API error response and into every log line that records it.
        #
        # `from exc` keeps the traceback, and `logging.exception` prints the
        # original SDK message too -- so the underlying SDK error must be
        # treated as sensitive data as well, not as an ordinary log line. To
        # debug Key Vault, turn on targeted DEBUG logging deliberately; do not
        # add `{exc}` back here.
        status = getattr(exc, "status_code", None)
        detail = f" (HTTP {status})" if status is not None else ""
        raise SecretUnavailable(
            f"Không đọc được secret {name!r} từ {vault_uri}: "
            f"{type(exc).__name__}{detail}. Xem traceback (`from exc`) "
            "để biết chi tiết; message cố tình KHÔNG nhúng nội dung lỗi SDK "
            "vì nó có thể chứa URL request/response body chứa secret."
        ) from exc

    value = getattr(secret, "value", None) or ""
    if not value:
        raise SecretUnavailable(
            f"Secret {name!r} tồn tại trong {vault_uri} nhưng rỗng."
        )
    return value
