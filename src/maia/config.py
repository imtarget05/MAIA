"""Central config - maps to §4 metadata, §6 vector DB, §9 tech stack."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_COLLECTION: str = "maia_knowledge"
    QDRANT_API_KEY: str = ""

    EMBED_MODEL: str = "BAAI/bge-small-en-v1.5"
    EMBED_DIM: int = 384

    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 50

    TOP_K_DENSE: int = 10
    TOP_K_BM25: int = 10
    TOP_K_FUSED: int = 8
    TOP_K_FINAL: int = 3
    SIMILARITY_THRESHOLD: float = 0.3
    RRF_K: int = 60

    CLOUDFLARE_ACCOUNT_ID: str = ""
    CLOUDFLARE_API_TOKEN: str = ""
    CLOUDFLARE_MODEL: str = "@cf/meta/llama-3.1-8b-instruct"

    STORAGE_DIR: str = "./storage"
    DATA_DIR: str = "./data/samples"
    ENTERPRISE_DATA_DIR: str = "./data/enterprise"

    # --- MAIA Receptionist / Agent ---
    TENANT_ID: str = "default"
    DEFAULT_EMPLOYEE_ID: str = "emp_001"
    MAX_HISTORY_TURNS: int = 8
    AGENT_MAX_ITER: int = 3
    AGENT_EVIDENCE_THRESHOLD: float = 0.3
    AGENT_GROUNDING_THRESHOLD: float = 0.15
    HR_MOCK_DB_PATH: str = "./storage/hr_mock.json"
    # HRIS real connector (optional, falls back to mock)
    HRIS_ENABLED: bool = False
    HRIS_BASE_URL: str = ""
    HRIS_API_KEY: str = ""
    HRIS_TIMEOUT_SEC: int = 5

    # --- Kafka streaming ingestion (PROJECT 2) ---
    KAFKA_BOOTSTRAP_SERVERS: str = "localhost:9092"
    KAFKA_TOPIC_CHUNKS: str = "topic.doc.chunks"
    KAFKA_TOPIC_DLQ: str = "topic.doc.chunks.dlq"
    KAFKA_TOPIC_FAILED: str = "topic.doc.embedding.failed"
    KAFKA_NUM_PARTITIONS: int = 4
    KAFKA_CONSUMER_GROUP: str = "embedding-workers"
    # Partitioning mode:
    #   "ordered"         -> key = document_id (all chunks of a doc -> 1 partition)
    #   "max-throughput"  -> key = hash(document_id + chunk_id) (spread across partitions)
    KAFKA_PARTITIONING: str = "ordered"
    KAFKA_WORKERS: int = 2
    KAFKA_MAX_RETRIES: int = 3
    KAFKA_RETRY_BACKOFF_MS: int = 250
    KAFKA_SKIP_EMBEDDED: bool = True  # idempotency: skip re-embedding already-stored chunks
    STREAM_TRANSPORT: str = "inmemory"  # inmemory | kafka

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
