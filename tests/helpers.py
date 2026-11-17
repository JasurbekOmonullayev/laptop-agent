"""Small helpers shared by the tests. Imported after conftest.py has switched to the test database."""
import json

from app.db import connect


def query(sql: str, params=()) -> list[dict]:
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def available(offer_no: str) -> int:
    return query("SELECT available_qty FROM laptop_availability WHERE offer_no = %s", [offer_no])[0]["available_qty"]


def booking_count() -> int:
    return query("SELECT count(*) AS n FROM bookings")[0]["n"]


# ---------- a scripted LLM, so agent tests need no network and always behave the same ----------

def llm_text(content: str) -> dict:
    return {"role": "assistant", "content": content}


def llm_tool_call(name: str, **arguments) -> dict:
    call = {"id": f"call_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}
    return {"role": "assistant", "content": "", "tool_calls": [call]}
    