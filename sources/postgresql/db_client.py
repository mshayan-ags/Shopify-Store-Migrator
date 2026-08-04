import logging
import os
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv

from utils.errors import ConfigurationError

load_dotenv()

logger = logging.getLogger("postgresql_export")

_connection = None


def _get_psycopg2():
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError as exc:
        raise ConfigurationError(
            "psycopg2 is required for the PostgreSQL connector -- install it with: pip install psycopg2-binary"
        ) from exc
    return psycopg2


def get_connection():
    global _connection

    if _connection is not None and getattr(_connection, "closed", 0) == 0:
        return _connection

    psycopg2 = _get_psycopg2()

    database_url = os.getenv("DATABASE_URL")
    if database_url:
        logger.info("Connecting to PostgreSQL via DATABASE_URL")
        _connection = psycopg2.connect(database_url)
        return _connection

    pg_host = os.getenv("PG_HOST")
    pg_port = os.getenv("PG_PORT", "5432")
    pg_database = os.getenv("PG_DATABASE")
    pg_user = os.getenv("PG_USER")
    pg_password = os.getenv("PG_PASSWORD")

    if not pg_host or not pg_database or not pg_user:
        raise ConfigurationError(
            "Missing PostgreSQL connection settings -- set DATABASE_URL, or all of "
            "PG_HOST/PG_DATABASE/PG_USER (PG_PORT defaults to 5432, PG_PASSWORD may be blank) in your .env"
        )

    logger.info("Connecting to PostgreSQL database '%s' on %s:%s", pg_database, pg_host, pg_port)
    _connection = psycopg2.connect(
        host=pg_host,
        port=pg_port,
        dbname=pg_database,
        user=pg_user,
        password=pg_password,
    )
    return _connection


def fetch_all(query: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    psycopg2 = _get_psycopg2()
    import psycopg2.extras

    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
