"""Apply SQL files from migrations/ in filename order, each one exactly once."""
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT_DIR / "migrations"


def get_conninfo() -> str:
    load_dotenv(PROJECT_DIR / ".env")
    return psycopg.conninfo.make_conninfo(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


def main() -> None:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))

    with psycopg.connect(get_conninfo(), autocommit=True) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        applied = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")}

        for path in files:
            if path.name in applied:
                print(f"skip     {path.name}")
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)",
                    (path.name,),
                )
            print(f"applied  {path.name}")


if __name__ == "__main__":
    main()