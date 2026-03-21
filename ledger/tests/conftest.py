import os
import asyncpg
import pytest
import pytest_asyncio
from pathlib import Path
from dotenv import load_dotenv

from src.event_store import EventStore

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://shuaib@/ledger_test?host=/var/run/postgresql")
SCHEMA = (Path(__file__).parent.parent / "src" / "schema.sql").read_text()


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DATABASE_URL)
    async with p.acquire() as conn:
        await conn.execute(
            "DROP TABLE IF EXISTS outbox, events, projection_checkpoints, event_streams CASCADE"
        )
        await conn.execute(SCHEMA)
    yield p
    await p.close()


@pytest_asyncio.fixture
async def store(pool):
    return EventStore(pool)


@pytest.fixture
def stream_id():
    """Returns a unique TEXT stream ID in the canonical loan-{id} format."""
    import uuid
    return f"loan-{uuid.uuid4()}"


@pytest.fixture
def session_stream_id():
    """Returns a unique TEXT stream ID in the canonical agent-{id}-{session} format."""
    import uuid
    return f"agent-{uuid.uuid4()}-{uuid.uuid4()}"
