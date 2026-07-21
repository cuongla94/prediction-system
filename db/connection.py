"""Database connection helper."""

from __future__ import annotations

import os
from typing import Optional

import psycopg


def get_db() -> Optional[psycopg.connection.Connection]:
    """Get a database connection from DATABASE_URL env var.

    Returns None if DATABASE_URL is not set or connection fails.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return None

    try:
        return psycopg.connect(database_url, connect_timeout=5)
    except Exception:
        return None
