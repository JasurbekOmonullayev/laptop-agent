"""Test setup. Tests run against a separate database, <POSTGRES_DB>_test, created fresh on every run.

The real database is never touched: POSTGRES_DB is switched BEFORE app.config is imported,
and app.config reads the variable only once, at import.
"""
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import psycopg
import pytest
from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent.parent
REAL_DB = os.environ.get("POSTGRES_DB") or dotenv_values(PROJECT_DIR / ".env")["POSTGRES_DB"]
TEST_DB = f"{REAL_DB}_test"
os.environ["POSTGRES_DB"] = TEST_DB

from app.config import settings  # noqa: E402  (must come after the switch above)
from app.db import connect  # noqa: E402

assert f"dbname={TEST_DB}" in settings.database_conninfo, "tests must never run on the real database"

# Laptops with known stock, so every test knows exactly what is available.
LAPTOPS = [
    # offer_no,  brand,    model,          price_uzs,  stock_qty
    ("SO1000001", "Acer", "Test One", 7_000_000, 1),
    ("SO1000003", "HP", "Test Three", 9_000_000, 3),
    ("SO1000010", "Lenovo", "Test Ten", 12_000_000, 10),
    ("SO1000000", "Asus", "Test Empty", 5_000_000, 0),
]
P10_TEXT = "The shop stores only the client's name, phone number and booking history, only to process bookings."


def database_conninfo(dbname: str) -> str:
    params = psycopg.conninfo.conninfo_to_dict(settings.database_conninfo)
    params["dbname"] = dbname
    return psycopg.conninfo.make_conninfo(**params)


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """Create an empty test database, apply the real migrations, add the test laptops."""
    with psycopg.connect(database_conninfo("postgres"), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEST_DB}"')

    subprocess.run([sys.executable, str(PROJECT_DIR / "scripts" / "migrate.py")], check=True,
                   env={**os.environ, "POSTGRES_DB": TEST_DB}, capture_output=True)

    valid_until = date.today() + timedelta(days=30)
    with connect() as conn:
        for offer_no, brand, model, price, stock in LAPTOPS:
            conn.execute(
                """INSERT INTO laptops (offer_no, brand, model, country, price_uzs, stock_qty,
                                        offer_valid_until, description, source_url)
                   VALUES (%s, %s, %s, 'China', %s, %s, %s, 'test laptop', 'https://example.com')""",
                [offer_no, brand, model, price, stock, valid_until],
            )
        conn.execute("INSERT INTO shop_policies VALUES ('P10', 'Personal data', %s)", [P10_TEXT])
    yield


@pytest.fixture(autouse=True)
def clean_tables():
    """Every test starts with no bookings, no pending summaries and no conversations."""
    with connect() as conn:
        conn.execute("TRUNCATE booking_status_log, bookings, pending_actions, messages, conversations")
    yield


@pytest.fixture
def conversation() -> str:
    with connect() as conn:
        return str(conn.execute("INSERT INTO conversations DEFAULT VALUES RETURNING conversation_id")
                   .fetchone()["conversation_id"])


@pytest.fixture
def scripted_llm(monkeypatch):
    """Replace the real LLM: each call returns the next scripted reply."""
    from app import agent

    def install(*replies):
        remaining = list(replies)
        monkeypatch.setattr(agent, "call_llm", lambda messages: remaining.pop(0))
        return remaining
    return install


@pytest.fixture(scope="session")
def real_database_conninfo() -> str:
    """Only for READING the PDF chunks in the live RAG tests; nothing is ever written there."""
    return database_conninfo(REAL_DB)
    