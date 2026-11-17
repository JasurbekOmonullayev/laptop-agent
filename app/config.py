"""All settings in one place: values from .env plus the shop's fixed business rules."""
import os
from dataclasses import dataclass
from pathlib import Path

import psycopg
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent

# Business rules from the Shop_Policies sheet. They are rules, not settings, so they live in code.
BOOKING_HOLD_HOURS = 48        # P03: a confirmed booking is held for 48 hours
MAX_UNITS_PER_BOOKING = 3      # P04: at most 3 units of one model per booking
SUMMARY_VALID_MINUTES = 15     # a summary older than 15 minutes must be shown again


@dataclass(frozen=True)
class Settings:
    database_conninfo: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    embedding_base_url: str
    embedding_model: str
    embedding_dim: int


def load_settings() -> Settings:
    load_dotenv(PROJECT_DIR / ".env")
    return Settings(
        database_conninfo=psycopg.conninfo.make_conninfo(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=os.getenv("POSTGRES_PORT", "5432"),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
        ),
        llm_base_url=os.environ["LLM_BASE_URL"],
        llm_api_key=os.environ["LLM_API_KEY"],
        llm_model=os.environ["LLM_MODEL"],
        embedding_base_url=os.environ["EMBEDDING_BASE_URL"].rstrip("/"),
        embedding_model=os.environ["EMBEDDING_MODEL"],
        embedding_dim=int(os.environ["EMBEDDING_DIM"]),
    )


settings = load_settings()