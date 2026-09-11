"""Central config - maps to §4 metadata, §6 vector DB, §9 tech stack.

Flat-only Settings (Simplification Step 2, Option A): every field below is a
real ``BaseSettings`` field and the single source of truth for ``.env``
loading. There is intentionally NO ``__getattr__``/``__setattr__`` delegation
to sub-configs and no ``settings.<sub>.`` access — all callers use
``settings.<FLAT_FIELD>`` directly.

Archived features (voice/finetune/mcp/teams, see ``_archive/``) have no fields
here. Live-but-disabled features (stream/Kafka, CRAG, LTM) keep their flat
fields, default OFF.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict

# Process-wide cache for the ephemeral JWT fallback (see
# Settings.jwt_secret_key). Without this, every property access minted a fresh
# random key, so maia.auth.SECRET_KEY != maia.api.SECRET_KEY and login
# succeeded while all authed endpoints 401'd. Module-level on purpose: keeps
# Settings flat-only (no new field, no sub-config).
_ephemeral_jwt_key: str | None = None


class Settings(BaseSettings):
    """Top-level settings: flat fields, env-bound, no magic."""

    # Vector store (§6)
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_COLLECTION: str = "maia_knowledge"
    QDRANT_API_KEY: str = ""

    # Embeddings (§9)
    EMBED_MODEL: str = "@cf/baai/bge-m3"
    EMBED_DIM: int = 384

    # Chunking (§4)
    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 50

    # Retrieval (§7: dense + BM25 -> RRF)
    TOP_K_DENSE: int = 10
    TOP_K_BM25: int = 10
    TOP_K_FUSED: int = 8
    TOP_K_FINAL: int = 3
    SIMILARITY_THRESHOLD: float = 0.3
    RRF_K: int = 60
    # When True, the LangGraph agent's retrieve node uses a LlamaIndex
    # VectorStoreIndex (backed by the existing QdrantStore) instead of the
    # default HybridRetriever.  Opt-in; default HybridRetriever is tested.
    # WS2 (2026-09-10): default flipped True after head-to-head eval on Qdrant
    # Cloud (8 docs / 25 chunks, top-k=3, 10 golden groups): hit@k / recall@k /
    # context_precision / mrr identical vs dense-only baseline in all groups.
    # Hybrid = LlamaIndex dense (MaiaQdrantStore) + BM25 -> RRF k=60.
    LLAMA_INDEX_DATA_PLANE: bool = True

    # LLM (§9)
    CLOUDFLARE_ACCOUNT_ID: str = ""
    CLOUDFLARE_API_TOKEN: str = ""
    CLOUDFLARE_MODEL: str = "@cf/meta/llama-3.1-8b-instruct"

    # Storage
    STORAGE_DIR: str = "./storage"
    DATA_DIR: str = "./data/samples"
    ENTERPRISE_DATA_DIR: str = "./data/enterprise"

    # Agent core (multi-tenant, grounding, HRIS)
    TENANT_ID: str = "default"
    DEFAULT_EMPLOYEE_ID: str = "emp_001"
    MAX_HISTORY_TURNS: int = 8
    AGENT_MAX_ITER: int = 3
    AGENT_EVIDENCE_THRESHOLD: float = SIMILARITY_THRESHOLD
    AGENT_GROUNDING_THRESHOLD: float = 0.15
    USE_LLM_GROUNDING: bool = False
    TOOL_TENANT_CHECK: bool = True
    HR_MOCK_DB_PATH: str = "./storage/hr_mock.json"
    HRIS_ENABLED: bool = False
    HRIS_BASE_URL: str = ""
    HRIS_API_KEY: str = ""
    HRIS_TIMEOUT_SEC: int = 5

    # Kafka streaming ingestion (PROJECT 2). Disabled by default.
    KAFKA_ENABLED: bool = False
    KAFKA_BOOTSTRAP_SERVERS: str = "localhost:9092"
    KAFKA_TOPIC_CHUNKS: str = "topic.doc.chunks"
    KAFKA_TOPIC_DLQ: str = "topic.doc.chunks.dlq"
    KAFKA_TOPIC_FAILED: str = "topic.doc.embedding.failed"
    KAFKA_NUM_PARTITIONS: int = 4
    KAFKA_CONSUMER_GROUP: str = "embedding-workers"
    KAFKA_PARTITIONING: str = "ordered"
    KAFKA_WORKERS: int = 2
    KAFKA_MAX_RETRIES: int = 3
    KAFKA_RETRY_BACKOFF_MS: int = 250
    KAFKA_SKIP_EMBEDDED: bool = True
    STREAM_TRANSPORT: str = "inmemory"

    # Corrective RAG (opt-in). Disabled by default.
    CRAG_ENABLED: bool = False
    CRAG_GRADE_USE_LLM: bool = False
    CRAG_GRADE_THRESHOLD: float = 0.35
    CRAG_MAX_CORRECTIONS: int = 2
    CRAG_WEB_SEARCH_ENABLED: bool = False

    # Long-term memory. Disabled by default.
    LTM_ENABLED: bool = False
    # WS6: cross-session auto-learning on chat() is opt-in (default False);
    # explicit writes via POST /memory/store remain always available.
    LTM_LEARN_ON_CHAT: bool = False
    LTM_DB_PATH: str = "./storage/ltm.db"
    LTM_RECALL_K: int = 3

    # URL ingestion
    URL_FETCH_TIMEOUT: int = 10
    URL_MAX_BYTES: int = 2000000
    URL_MAX_LINKS_PER_SESSION: int = 20

    # PII role-email allowlist (G-04-FU2: department contacts survive redact)
    ROLE_EMAIL_ALLOWLIST: str = (
        "support@company.com,help@company.com,it-help@company.com,"
        "hr@company.com,hr-escalation@company.com,security@company.com,"
        "benefits@company.com,eap@company.com,finance@company.com,"
        "onboarding@company.com"
    )
    ROLE_EMAIL_ALLOW_PREFIXES: str = (
        "support,help,it-help,it-,hr,hr-,security,benefits,eap,onboarding,finance"
    )
    ROLE_EMAIL_ALLOW_DOMAIN: str = "company.com"

    # Observability / UI
    PIPELINE_TRACE: bool = False
    UI_SHOW_TECH_BADGE: bool = False
    ANSWER_STYLE: str = "concise"

    # Reliability (circuit breakers, retries)
    RELIABILITY_FAILURE_THRESHOLD: int = 5
    RELIABILITY_RECOVERY_TIMEOUT_SEC: float = 30.0
    RELIABILITY_MAX_RETRIES: int = 2
    RELIABILITY_RETRY_BACKOFF_SEC: float = 0.25
    RELIABILITY_QDRANT_THRESHOLD: int = 0
    RELIABILITY_LLM_THRESHOLD: int = 0
    RELIABILITY_EMBED_THRESHOLD: int = 0

    # Workflow / sessions
    WORKFLOW_DB_PATH: str = "./storage/workflow.db"
    SESSION_DB_PATH: str = "./storage/session.db"

    # Auth
    JWT_SECRET_KEY: str = ""
    CORS_ORIGINS: str = ""
    ENVIRONMENT: str = "development"
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    API_BASE_URL: str = "http://localhost:8000"

    # Email / notifications
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "MAIA <no-reply@company.com>"
    SMTP_USE_TLS: bool = True
    HR_EMAIL: str = "hr@company.com"
    IT_EMAIL: str = "it-help@company.com"
    SECURITY_EMAIL: str = "security@company.com"
    APP_BASE_URL: str = "http://localhost:8501"
    PASSWORD_RESET_EXPIRE_MIN: int = 30
    BOOTSTRAP_FIRST_ADMIN: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    @property
    def jwt_secret_key(self) -> str:
        if not self.JWT_SECRET_KEY:
            if self.ENVIRONMENT == "production":
                raise RuntimeError(
                    "JWT_SECRET_KEY must be set in production. "
                    "Set JWT_SECRET_KEY in .env or environment variables."
                )
            import secrets
            import warnings
            warnings.warn(
                "JWT_SECRET_KEY is not set — using an ephemeral random key. "
                "All tokens/sessions will be invalidated on restart. "
                "Set JWT_SECRET_KEY in .env for production.",
                RuntimeWarning,
                stacklevel=1,
            )
            global _ephemeral_jwt_key
            if _ephemeral_jwt_key is None:
                _ephemeral_jwt_key = secrets.token_urlsafe(48)
            return _ephemeral_jwt_key
        return self.JWT_SECRET_KEY


settings = Settings()
