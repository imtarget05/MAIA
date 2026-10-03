"""
Durable persistence cho MAIA — ba tầng, tách biệt, KHÔNG trộn vào nhau.

================================================================================
PROBE-ONLY — NOT THE CANONICAL CHECKPOINT AUTHORITY
================================================================================

``AsyncPostgresSaver`` trong module này KHÔNG phục vụ request nào. Đây là
điểm dễ nhầm của repo này, và nó đã tồn tại một thời gian: có bằng chứng
restart bằng Postgres, nhưng ``POST /agent/chat`` dựng graph qua
``langgraph_agent.get_durable_graph()`` — vốn KHÔNG gọi module này.

Capability tồn tại KHÔNG có nghĩa runtime dùng capability. Đó chính là lý do
``docs/_p8_process_restart_probe.py`` (probe gọi helper này) không đủ để
chứng minh HITL bền vững trên served path.

Canonical authority hiện tại:

    POST /agent/chat
      -> langgraph_agent.get_durable_graph()
      -> PostgresSaver (SYNC, qua DATABASE_URL)

và được kiểm chứng bởi ``docs/_p9_served_path_probe.py`` +
``tests/test_served_path_durability.py``, chạy đúng hai hàm mà endpoint gọi.

Module này được giữ lại vì:
  * ``tests/test_persistence.py`` và probe _p8 dùng nó;
  * async checkpointer là đường hướng hợp lệ khi API chuyển sang async end-to-end.

Nếu API chuyển async, hãy WIRE module này vào api.py — hoặc xoá nó. Đừng để
nó tiếp tục là "authority thứ hai" không ai gọi.
================================================================================

Ba tầng này trả lời ba câu hỏi khác nhau, và trộn chúng là nguồn bug kinh điển:

    1. Thread / checkpoint state  — "tao đang làm gì dở?"      -> PostgresSaver
    2. Long-term memory          — "tao ĐÃ biết gì về khách?"  -> LongTermMemory
    3. RAG / vector index        — "tài liệu nào liên quan?"   -> Qdrant

Vì sao phải tách:
    * Thread state bị xoá khi thread kết thúc, memory thì không
    * Thread state chỉ agent được ghi, memory thì cần truy vấn theo tenant
    * Vector index có lifecycle riêng (re-embed khi đổi model)

`AsyncPostgresSaver` là lớp bọc mỏng quanh `langgraph.checkpoint.postgres`.
Ta không viết lại checkpoint protocol — thư viện đã làm đúng. Việc của ta là:
    * setup schema một lần
    * quản lý vòng đời connection (dùng được cả trong lẫn ngoài `async with`)
    * cấu hình không hardcode — mọi thứ qua env
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

# Biến môi trường — KHÔNG hardcode credential trong code.
ENV_DSN = "MAIA_POSTGRES_DSN"
ENV_CHECKPOINT_TABLE = "MAIA_CHECKPOINT_TABLE"
DEFAULT_CHECKPOINT_TABLE = "maia_checkpoints"

#: Lý do bật/tắt từng tầng, để log khỏi mơ hồ.
TIER_THREAD_STATE = "thread_state"
TIER_LONG_TERM_MEMORY = "long_term_memory"
TIER_RAG_INDEX = "rag_index"

ALL_TIERS = (TIER_THREAD_STATE, TIER_LONG_TERM_MEMORY, TIER_RAG_INDEX)


class PersistenceUnavailable(RuntimeError):
    """Không cấu hình được Postgres — nói rõ cần gì, không im lặng fallback."""


def dsn_from_env() -> str:
    """
    Đọc DSN từ env.

    Cố tình KHÔNG có default: một DSN mặc định trong code nghĩa là credential
    nằm trong repo, và môi trường khác sẽ âm thầm ghi vào đúng database đó.
    """
    dsn = os.environ.get(ENV_DSN)
    if not dsn:
        raise PersistenceUnavailable(
            f"Thiếu {ENV_DSN}. Cần dạng "
            f"'postgresql://USER:PASS@HOST:PORT/DBNAME' (lấy từ secret manager).")
    return dsn


def checkpoint_table_from_env() -> str:
    return os.environ.get(ENV_CHECKPOINT_TABLE, DEFAULT_CHECKPOINT_TABLE)


def postgres_available() -> bool:
    """True nếu cấu hình đủ. Không mở kết nối — để caller quyết định."""
    return bool(os.environ.get(ENV_DSN))


async def open_saver(conn: Any):
    """
    Tạo AsyncPostgresSaver từ một connection đã mở và chạy setup.

    Tách riêng khỏi vòng đời connection để test có thể tự quản lý.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    saver = AsyncPostgresSaver(conn)
    await saver.setup()
    return saver


@asynccontextmanager
async def durable_checkpointer(conn: Any = None):
    """
    Cấp một AsyncPostgresSaver đã setup, tự quản lý connection nếu cần.

    Dùng:
        async with durable_checkpointer() as saver:
            graph = build_graph(checkpointer=saver)

    Nếu truyền `conn` vào thì ta dùng conn đó và KHÔNG đóng nó — gọi bên
    ngoài giữ quyền sở hữu connection của mình.
    """
    if conn is not None:
        saver = await open_saver(conn)
        yield saver
        return

    try:
        from psycopg_pool import AsyncConnectionPool
    except ImportError as exc:  # pragma: no cover
        raise PersistenceUnavailable(
            "Thiếu psycopg_pool. Cài: pip install 'psycopg[pool]'") from exc

    pool = AsyncConnectionPool(
        dsn_from_env(),
        min_size=1, max_size=5,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    await pool.open()
    try:
        async with pool.connection() as pool_conn:
            yield await open_saver(pool_conn)
    finally:
        await pool.close()
