"""Database connections. Every tool opens a short connection, does its work, and closes it."""
import psycopg
from psycopg.rows import dict_row

from app.config import settings


def connect() -> psycopg.Connection:
    """A new connection whose rows come back as dicts: row["price_uzs"] instead of row[3]."""
    return psycopg.connect(settings.database_conninfo, row_factory=dict_row)