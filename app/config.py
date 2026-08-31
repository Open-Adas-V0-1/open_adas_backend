import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Loaded explicitly (not just relied on via pydantic-settings) so that dynamic,
# per-node keys like LLM_OVERRIDE_{NODE}_BACKEND — which have no fixed field on
# Settings — are still readable from os.environ via get_node_llm_config().
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Open_Adas"
    app_version: str = "0.1"

    db_host: str
    db_port: int = 5432
    db_name: str
    db_user: str
    db_password: str

    minio_endpoint: str
    minio_root_user: str
    minio_root_password: str
    minio_bucket: str
    minio_secure: bool = False

    # ── LLM: generic defaults, provider-agnostic ──
    llm_backend: str = "gpt"
    llm_model_id: str = ""
    llm_api_key: str | None = None
    llm_base_url: str | None = None

    # ── LLM: provider-specific keys (used when the generic ones aren't set) ──
    gemini_api_key: str | None = None
    anthropic_api_key: str | None = None

    # ── LLM: retry policy ──
    llm_retry_enabled: bool = True
    llm_retry_max_attempts: int = 3
    llm_retry_initial_wait: float = 1.0
    llm_retry_max_wait: float = 30.0
    llm_retry_jitter: float = 1.0
    llm_timeout: float = 60.0

    # ── Logging ──
    log_level: str = "INFO"
    log_json: bool = False

    # ── SysML middle-layer guards (loop protection) ──
    sysml_middle_max_visits: int = 10
    sysml_middle_recursion_limit: int = 25

    # ── Top-level supervisor guards (loop protection) ──
    supervisor_max_visits: int = 10
    supervisor_recursion_limit: int = 25

    # ── Production checkpointer ──
    checkpoint_encryption_key: str | None = None
    checkpoint_durability: str = "async"  # sync | async | exit

    # ── SysML single-processing (Layer 3) guards ──
    sysml_proc_max_visits: int = 5
    sysml_proc_recursion_limit: int = 25

    # ── SysML v2 tooling (daltskin/sysml-v2-lsp) ──
    sysml_node_bin: str = "node"
    sysml_lsp_server_path: str | None = None  # defaults to tools/sysml_v2/node_modules/.../server.js
    sysml_mcp_server_path: str | None = None  # defaults to tools/sysml_v2/node_modules/.../mcpServer.js
    sysml_tooling_timeout: float = 30.0

    # ── SysML processing thread TTL (checkpointer state only, never approved artifacts) ──
    sysml_thread_ttl_days: int = 30

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def checkpointer_database_url(self) -> str:
        """psycopg-style URL (no +asyncpg driver suffix) for AsyncPostgresSaver,
        which uses psycopg directly rather than SQLAlchemy.
        """
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_node_llm_config(node_name: str) -> dict[str, str] | None:
    """Read LLM_OVERRIDE_{NODE}_BACKEND / _MODEL_ID for a given node, if set."""
    prefix = f"LLM_OVERRIDE_{node_name.upper()}_"
    backend = os.getenv(prefix + "BACKEND")
    model_id = os.getenv(prefix + "MODEL_ID")
    if not backend and not model_id:
        return None
    return {"backend": backend, "model_id": model_id}
