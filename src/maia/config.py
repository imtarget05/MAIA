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

    # Vector store backend selection (ADR-0005).
    #
    # Which implementation of the maia.retrieval_port protocols to build.
    # "qdrant" stays the default and stays the only backend the eval baselines
    # were measured on: the Azure cutover is gated on score-normalised eval
    # parity (see docs/azure-integration.md), not on the backend merely
    # existing. Accepted: qdrant | azure_ai_search | memory. "memory" is the
    # offline InMemoryVectorStore used by tests and demos.
    VECTOR_STORE_BACKEND: str = "qdrant"

    # Azure hosting (managed identity + Key Vault). Every field defaults to
    # "" so an absent Azure configuration is indistinguishable from a local
    # run -- maia.azure_identity decides, at call time, between the ambient
    # credential chain and a managed identity. No network call happens while
    # this module is imported.
    #
    # AZURE_TENANT_ID / AZURE_CLIENT_ID: the user-assigned managed identity's
    # client id. On Azure it pins WHICH identity DefaultAzureCredential uses
    # (without it the system-assigned identity is picked, which is a different
    # principal with different role assignments -- a silent 403, not a useful
    # error). Off Azure they are left empty so the ambient chain (`az login`)
    # is what a developer gets.
    AZURE_TENANT_ID: str = ""
    AZURE_CLIENT_ID: str = ""
    # Vault URI (e.g. https://my-vault.vault.azure.net/). Non-empty turns on
    # Key Vault secret resolution in maia.azure_identity; empty keeps the
    # process environment as the only secret source.
    AZURE_KEY_VAULT_URI: str = ""
    # Azure AI Search (S1 or higher -- the free tier has no vector search).
    AZURE_AI_SEARCH_ENDPOINT: str = ""
    AZURE_AI_SEARCH_INDEX: str = "maia_knowledge"
    # API key is the LOCAL DEV fallback only. The cloud path is managed
    # identity (Search Index Data Reader/Contributor role on the service), so
    # this stays empty in Azure and the value is never a deployment secret.
    # It exists at all because a dev container has no identity to assume.
    AZURE_AI_SEARCH_API_KEY: str = ""

    # Embeddings (§9)
    #
    # Two different dimensions are in play and conflating them is a bug:
    #   EMBED_MODEL         — the production model, Cloudflare Workers AI
    #                         ``@cf/baai/bge-m3``, which emits 1024-dim vectors.
    #   CLOUDFLARE_EMBED_DIM — that model's vector width. The Qdrant collection
    #                         is created from the *runtime* embedder dim, so
    #                         this is the value that must match in production.
    #   EMBED_DIM           — width of the OFFLINE fallbacks only: the
    #                         deterministic hash embedder and the fastembed
    #                         model (``paraphrase-multilingual-MiniLM-L12-v2``,
    #                         which really is 384-dim).
    EMBED_MODEL: str = "@cf/baai/bge-m3"
    CLOUDFLARE_EMBED_DIM: int = 1024
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

    # LLM (§9) — Cloudflare (legacy) hoặc local OpenAI-compat (LM Studio LAN).
    CLOUDFLARE_ACCOUNT_ID: str = ""
    CLOUDFLARE_API_TOKEN: str = ""
    CLOUDFLARE_MODEL: str = "@cf/meta/llama-3.1-8b-instruct"
    # Local OpenAI-compatible LLM, reached through the centralized llm-gateway
    # proxy so usage is attributable in the gateway's central telemetry.
    # LLM_PROVIDER: cloudflare (creds) | local (gateway/LAN) | mock (forced).
    LLM_PROVIDER: str = "local"
    # Base URL resolution order (see maia.llm_endpoints): the explicit
    # LLM_BASE_URL wins when set, then the gateway, then the direct LAN
    # upstream (tried only when the gateway refuses the connection).
    LLM_BASE_URL: str = ""
    LLM_GATEWAY_URL: str = "http://localhost:8787/v1"
    LLM_DIRECT_UPSTREAM_URL: str = "http://192.168.1.8:1234/v1"
    LLM_CHAT_MODEL: str = "qwen2.5-vl-3b-instruct"
    LLM_TIMEOUT_SEC: int = 120
    # Short connect budget so a gateway that is down is skipped in seconds
    # instead of blocking for the full LLM_TIMEOUT_SEC.
    LLM_CONNECT_TIMEOUT_SEC: float = 3.0
    # Identifies MAIA in the gateway's centralized telemetry
    # (llm-telemetry.jsonl -> "project"). Canonical ids: MAIA / ApexInspect-AI.
    LLM_PROJECT: str = "MAIA"

    # Embeddings endpoint: same gateway, same X-Project attribution.
    # EMBEDDINGS_PROVIDER is "" (auto) by default, which keeps the existing
    # backend chain unchanged (Cloudflare -> fastembed -> hash). Set it to
    # local_openai / lmstudio / ollama to route embeddings through the
    # gateway's /v1/embeddings. EMBEDDINGS_BASE_URL follows the same
    # gateway -> direct-upstream resolution as LLM_BASE_URL.
    EMBEDDINGS_PROVIDER: str = ""
    EMBEDDINGS_BASE_URL: str = ""
    EMBEDDINGS_MODEL: str = "text-embedding-nomic-embed-text-v1.5"
    EMBEDDINGS_API_KEY: str = ""
    EMBEDDINGS_TIMEOUT_SEC: int = 60
    EMBEDDINGS_CONNECT_TIMEOUT_SEC: float = 3.0
    # Vector width of the gateway embedding model. It must match the Qdrant
    # collection width, otherwise embedding raises EmbeddingDimMismatch
    # rather than writing meaningless vectors into the collection.
    EMBEDDINGS_DIM: int = 0

    # Storage
    STORAGE_DIR: str = "./storage"
    DATA_DIR: str = "./data/samples"
    ENTERPRISE_DATA_DIR: str = "./data/enterprise"

    # Durable application state — PostgreSQL.
    #
    # THIS IS THE HITL DURABLE CHECKPOINT AUTHORITY. It is deliberately NOT
    # defaulted: an empty value means the durable path has no database, and
    # langgraph_agent.get_durable_graph() then fails explicitly rather than
    # falling back to a local SQLite file. The fallback is the reason a process
    # restart used to lose an approval-pending action while appearing durable.
    #
    # A blank default also means `import maia.config` never tries to connect, so
    # unit tests that do not touch the durable graph are unaffected.
    #
    # Do NOT put a real DSN here: this value is a default, not a secret store.
    # Supply it through the environment (or a local .env, which is gitignored).
    DATABASE_URL: str = ""

    # Streaming checkpoint budget (the MemorySaver graph in langgraph_agent).
    #
    # The SSE path runs on a process-wide in-memory checkpointer keyed by
    # "tenant:session", so without a bound every new session_id leaks a thread
    # that is never released for the life of the process. These two knobs cap
    # that: MAX_SESSIONS evicts the least-recently-used thread once the map is
    # full, SESSION_TTL_SEC evicts anything idle for longer than the TTL.
    #
    # Deliberately NOT applied to the durable (SqliteSaver) graph: a paused
    # HITL run must survive until the human answers, however long that takes.
    MAX_STREAMING_SESSIONS: int = 512
    STREAMING_SESSION_TTL_SEC: int = 3600

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

    # ITSM ticketing (Jira-first provider adapter, local fallback).
    # When ITSM_ENABLED=true and ITSM_BASE_URL is set, create_it_ticket
    # attempts a real provider call (default provider: jira); on any
    # failure it falls back to the local JSON audit store in tools.py.
    ITSM_ENABLED: bool = False
    ITSM_PROVIDER: str = "jira"
    ITSM_BASE_URL: str = ""
    ITSM_API_TOKEN: str = ""
    ITSM_PROJECT_KEY: str = ""
    ITSM_TIMEOUT_SEC: int = 8

    # Outbox background worker (lifespan loop, self-contained).
    # Default OFF so unit tests stay deterministic; enable on the server.
    OUTBOX_WORKER_ENABLED: bool = False
    OUTBOX_WORKER_INTERVAL_SEC: float = 60.0
    OUTBOX_WORKER_STARTUP_DRAIN: bool = True
    OUTBOX_MAX_RETRIES: int = 5

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
    # OpenTelemetry OTLP exporter (opt-in). When OTEL_ENABLED is False (default)
    # tracing helpers are NoOp and the opentelemetry packages are not required.
    # When True, spans are exported to OTEL_EXPORTER_OTLP_ENDPOINT
    # (HTTP/protobuf or gRPC, auto-detected by the exporter).
    OTEL_ENABLED: bool = False
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    OTEL_SERVICE_NAME: str = "maia"
    UI_SHOW_TECH_BADGE: bool = False
    ANSWER_STYLE: str = "concise"

    # Rate limiting: fixed window per user on /chat + /chat/stream.
    # REDIS_URL="" (default) -> process-local memory limiter (single replica).
    # Set REDIS_URL=redis://... for multi-replica deployments.
    REDIS_URL: str = ""
    CHAT_RATE_LIMIT_PER_MIN: int = 60

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
    # Auth database DSN. The local default is a SQLite file in the process
    # working directory, which is convenient for dev and unusable in a
    # deployment: most PaaS filesystems are ephemeral, so every user, session,
    # refresh token and approval is lost on redeploy. Outside development this
    # MUST be set explicitly — see the startup assertion below.
    AUTH_DB_URL: str = "sqlite:///./maia_auth.db"
    JWT_SECRET_KEY: str = ""
    CORS_ORIGINS: str = ""
    ENVIRONMENT: str = "development"
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_ALLOWED_REDIRECT_URIS: str = ""
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

    # PromptOps (versioned prompt library + offline eval gate).
    # Prompts live on disk under PROMPTS_DIR, never inline in code, so a prompt
    # change is a reviewable Git diff with its own eval suite. See prompts/README.md.
    PROMPTS_DIR: str = "./prompts"
    # Minimum pass rate for the prompt eval gate (1.0 = every case must pass).
    PROMPT_EVAL_MIN_SCORE: float = 1.0
    # Output directory for eval reports (CI artefact / audit evidence).
    PROMPT_EVAL_REPORT_DIR: str = "./storage/prompt_evals"

    # MCP (Model Context Protocol) — tool-calling over JSON-RPC 2.0.
    # ON by default: the agent is expected to be able to call tools. The risk is
    # bounded by four things, not by switching the feature off —
    #   1. taxonomy gate: only `general` questions are eligible
    #      (MCP_ELIGIBLE_INTENTS); leave/IT/VPN/expense/benefits/policy questions
    #      always go to retrieval, so an HR question cannot be answered by a
    #      marketing tool;
    #   2. specific route patterns: metric names and explicit channel/verb phrases
    #      only — bare "email" / "chi phí" / "nội dung" do not match;
    #   3. MCP_TOOL_ALLOWLIST + MCP_MAX_TOOL_CALLS per turn;
    #   4. every call is audited (tool + hashed args) and every integration degrades
    #      to a visible dry run without credentials.
    # Set it to false to remove the mcp_dispatch node from the graph entirely.
    MCP_ENABLED: bool = True
    MCP_SERVERS: str = "airtable,notification,sql_analytics,market_insight"
    MCP_BRIDGE_TRANSPORT: str = "inprocess"  # inprocess | stdio
    MCP_TOOL_TIMEOUT_SEC: float = 15.0
    # Hard cap on tool calls per agent turn: a runaway plan must not fan out into
    # dozens of side effects (emails, Airtable rows) before a human notices.
    MCP_MAX_TOOL_CALLS: int = 4
    MCP_TOOL_ALLOWLIST: str = ""  # empty = every tool of the enabled servers
    MCP_AUDIT_LOG_PATH: str = "./storage/mcp_audit.jsonl"

    # MCP prompts (the PromptOps library exposed as maia://prompts/...).
    #
    # OFF by default, and it stays off even when MCP_ENABLED is on, because a
    # prompt is an *instruction injected into the model's turn* rather than a
    # side-effecting call. Tool calls are bounded by an allowlist and a budget;
    # a prompt changes what the model believes it was asked to do, so it earns
    # its own opt-in and its own allowlist.
    #
    # Prompts are also never keyword-routed. The model lists them and asks for
    # one *by name*; auto-matching a prompt on question words is how an HR
    # question ends up executing a marketing instruction.
    MCP_PROMPTS_ENABLED: bool = False
    # Empty = every prompt advertised by the `prompts` server. Set it to
    # "prompts.nl_to_sql" style entries to pin exactly which ones are usable.
    MCP_PROMPT_ALLOWLIST: str = ""
    # Prompts are read-only, so this is a runaway-loop guard, not a side-effect
    # budget. Kept small: a turn that needs more than a couple of prompts is
    # better served by the RAG corpus.
    MCP_MAX_PROMPT_FETCHES: int = 2

    # Integrations used by the MCP servers. Empty credential = the server runs in
    # local/dry-run mode and says so in its result (`dry_run: true`); it never
    # pretends a message was delivered.
    AIRTABLE_API_KEY: str = ""
    AIRTABLE_BASE_ID: str = ""
    AIRTABLE_TABLE: str = "Content Calendar"
    AIRTABLE_TIMEOUT_SEC: int = 10
    TEAMS_WEBHOOK_URL: str = ""
    ZALO_OA_ACCESS_TOKEN: str = ""
    ZALO_OA_ENDPOINT: str = "https://business.openapi.zalo.me/message/template"
    NOTIFICATION_OUTBOX_PATH: str = "./storage/notification_outbox.jsonl"
    NOTIFICATION_DRY_RUN: bool = True

    # Marketing / product data pipeline (mini warehouse + collectors).
    MARKET_DB_PATH: str = "./storage/market.db"
    MARKET_DATA_DIR: str = "./data/market"
    # Real HTTP collection is opt-in: offline by default so tests and demos never
    # depend on an external API (or silently scrape something).
    MARKET_HTTP_ENABLED: bool = False
    MARKET_HTTP_TIMEOUT_SEC: float = 10.0
    MARKET_HTTP_MAX_PAGES: int = 3
    MARKET_MAX_ROWS: int = 5000

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


_DEVELOPMENT_ENVIRONMENTS = {"", "dev", "development", "local", "test"}
_DEFAULT_AUTH_DB_URL = "sqlite:///./maia_auth.db"

# The auth DSN was previously a hardcoded literal in api.py with no settings
# field and no env override, so it always resolved to a SQLite file beside the
# working directory. On a platform with an ephemeral filesystem that silently
# discards every account, session, refresh token and approval on each deploy or
# restart. Fail loudly at import instead of losing the data quietly.
if settings.ENVIRONMENT.lower() not in _DEVELOPMENT_ENVIRONMENTS and (
    settings.AUTH_DB_URL == _DEFAULT_AUTH_DB_URL
):
    raise RuntimeError(
        "AUTH_DB_URL must be configured when ENVIRONMENT="
        f"{settings.ENVIRONMENT!r}. The default {_DEFAULT_AUTH_DB_URL!r} is a "
        "SQLite file in the process working directory, which is ephemeral on "
        "most PaaS filesystems: every user, session, refresh token and approval "
        "is lost on redeploy. Set AUTH_DB_URL to a durable location, e.g. "
        "sqlite:////mnt/data/maia_auth.db on a mounted disk, or to a "
        "PostgreSQL DSN."
    )
