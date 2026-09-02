from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer

from app.config import get_settings


@asynccontextmanager
async def build_production_checkpointer():
    """The ONE checkpointer for the whole stack. Layers 2 and 3 never build their own —
    they inherit this one via the config object propagated through the 'inside a node'
    wrappers at each boundary.

    - Serializer: EncryptedSerializer wraps LangGraph's own JsonPlusSerializer (which
      already round-trips pydantic models, enums, datetimes, and UUIDs losslessly) with
      AES encryption, so checkpoint state is encrypted at rest in Postgres.
    - Key: CHECKPOINT_ENCRYPTION_KEY from env — never hardcoded.
    """
    settings = get_settings()
    if not settings.checkpoint_encryption_key:
        raise ValueError(
            "CHECKPOINT_ENCRYPTION_KEY is not set. Checkpoint state must be encrypted at rest."
        )

    serde = EncryptedSerializer.from_pycryptodome_aes(key=settings.checkpoint_encryption_key.encode())

    async with AsyncPostgresSaver.from_conn_string(
        settings.checkpointer_database_url, serde=serde
    ) as checkpointer:
        await checkpointer.setup()
        yield checkpointer
