"""Shop policy tool: the shop's own rules (P01-P10), read from the database in this call."""
import re

from app.db import connect

POLICY_ID = re.compile(r"^P\d{2}$")


def get_shop_policies(policy_id: str | None = None) -> dict:
    with connect() as conn:
        if policy_id is None:
            rows = conn.execute("SELECT policy_id, topic, rule_text FROM shop_policies ORDER BY policy_id").fetchall()
            return {"policies": rows}

        policy_id = policy_id.strip().upper()
        if not POLICY_ID.match(policy_id):
            return {"error": f"'{policy_id}' is not a valid policy id (format: P01 to P10)."}
        row = conn.execute(
            "SELECT policy_id, topic, rule_text FROM shop_policies WHERE policy_id = %s", [policy_id]
        ).fetchone()
    if row is None:
        return {"found": False, "policy_id": policy_id}
    return {"found": True, "policy": row}


TOOLS = {
    "get_shop_policies": {
        "function": get_shop_policies,
        "schema": {
            "name": "get_shop_policies",
            "description": (
                "The shop's own rules: working hours, how booking works, booking hold period, booking limits, "
                "cancelling, payment methods, prices and discounts, pickup and delivery, warranty, personal data. "
                "Call it for any question about how the shop works. Without policy_id it returns all rules. "
                "Quote the policy id (e.g. P06) in the answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {"policy_id": {"type": "string", "description": "Optional, e.g. P03"}},
            },
        },
    },
}