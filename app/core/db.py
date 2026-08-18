from typing import Any, List, Optional

import asyncpg
from asyncpg.pool import Pool

from app.core.config import settings

pool: Optional[Pool] = None


async def init_pool() -> None:
    global pool
    # statement_cache_size=0: required on Supabase's Transaction-mode pooler (port 6543) —
    # PgBouncer in transaction mode can hand a connection to a different backend between
    # statements, so asyncpg's default prepared-statement caching breaks intermittently.
    # If you ever move to the Session pooler (5432) or a direct connection, this is harmless
    # to leave in place, just slightly less efficient.
    pool = await asyncpg.create_pool(
        settings.database_url, min_size=2, max_size=10, statement_cache_size=0
    )


async def close_pool() -> None:
    if pool is not None:
        await pool.close()


def get_pool() -> Pool:
    if pool is None:
        raise RuntimeError("DB pool not initialized")
    return pool


def rec(row: Any) -> Optional[dict]:
    """Serialize a single asyncpg Record (or None) to a plain dict."""
    return dict(row) if row else None


def recs(rows: Any) -> List[dict]:
    """Serialize a list of asyncpg Records to plain dicts."""
    return [dict(r) for r in rows]
