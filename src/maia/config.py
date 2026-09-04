"""Central config - maps to §4 metadata, §6 vector DB, §9 tech stack."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_COLLECTION: str = "maia_knowledge"

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

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
