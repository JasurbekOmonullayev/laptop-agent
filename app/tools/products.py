"""Product tools: search, details, comparison. Every number comes from the database in this call."""
import re

from app.db import connect

OFFER_NO = re.compile(r"^SO\d+$")
DEFAULT_LIMIT = 5
MAX_LIMIT = 10
DESCRIPTION_CHARS = 1200

# A dedicated RTX/GTX graphics card below this price is implausible in our data (the next cheapest is 6,000,000 UZS).
GAMING_GPU = re.compile(r"\b(RTX|GTX)\b", re.IGNORECASE)
IMPLAUSIBLE_BELOW_UZS = 5_000_000

BASE_QUERY = """
SELECT l.*, a.available_qty
FROM laptops l
JOIN laptop_availability a USING (offer_no)
"""


def format_uzs(amount: int) -> str:
    return f"{amount:,} UZS"


def format_spec(value, is_min: bool, unit: str) -> str:
    if value is None:
        return "not specified"
    number = f"{value:g}" if isinstance(value, float) else str(value)
    return f"at least {number} {unit}" if is_min else f"{number} {unit}"


def to_card(row: dict, with_description: bool = False) -> dict:
    """Turn a database row into what the LLM sees. Formatting is done here, not by the LLM."""
    card = {
        "offer_no": row["offer_no"],
        "brand": row["brand"],
        "model": row["model"],
        "price": format_uzs(row["price_uzs"]),
        "available_units": row["available_qty"],
        "cpu": row["cpu"] or "not specified",
        "ram": format_spec(row["ram_gb"], row["ram_is_min"], "GB"),
        "storage": format_spec(row["storage_gb"], row["storage_is_min"], "GB"),
        "screen": format_spec(
            float(row["screen_inch"]) if row["screen_inch"] is not None else None,
            row["screen_is_min"], "inch",
        ),
        "resolution": row["resolution"] or "not specified",
        "gpu": row["gpu"] or "not specified",
        "os": row["os"] or "not specified",
        "warranty": f"{row['warranty_months']} months" if row["warranty_months"] else "not specified",
        "delivery": f"{row['delivery_days']} days" if row["delivery_days"] else "not specified",
    }
    if row["gpu"] and GAMING_GPU.search(row["gpu"]) and row["price_uzs"] < IMPLAUSIBLE_BELOW_UZS:
        card["price_warning"] = ("Unusually low price for this hardware. Tell the client to confirm the price with "
                                 "the shop; do not call it a good deal.")
    if with_description:
        card["country"] = row["country"]
        card["offer_valid_until"] = row["offer_valid_until"].isoformat()
        card["seller_description"] = row["description"][:DESCRIPTION_CHARS]
        card["source_url"] = row["source_url"]
    return card


def build_filters(args: dict) -> list[tuple[str, list]]:
    """Each requested criterion becomes one SQL condition with its parameters."""
    filters = []
    if args.get("brand"):
        filters.append(("l.brand ILIKE %s", [args["brand"]]))
    if args.get("max_price_uzs") is not None:
        filters.append(("l.price_uzs <= %s", [int(args["max_price_uzs"])]))
    if args.get("min_price_uzs") is not None:
        filters.append(("l.price_uzs >= %s", [int(args["min_price_uzs"])]))
    if args.get("min_ram_gb") is not None:
        filters.append(("l.ram_gb >= %s", [int(args["min_ram_gb"])]))
    if args.get("min_storage_gb") is not None:
        filters.append(("l.storage_gb >= %s", [int(args["min_storage_gb"])]))
    if args.get("min_screen_inch") is not None:
        filters.append(("l.screen_inch >= %s", [float(args["min_screen_inch"])]))
    if args.get("max_screen_inch") is not None:
        filters.append(("l.screen_inch <= %s", [float(args["max_screen_inch"])]))
    if args.get("text"):
        # Free text is matched against the structured columns AND the seller's description,
        # so a spec that is missing from the columns can still be found.
        filters.append((
            "concat_ws(' ', l.model, l.cpu, l.gpu, l.os, l.description) ILIKE %s",
            [f"%{args['text']}%"],
        ))
    return filters


def search_laptops(**args) -> dict:
    limit = min(int(args.get("limit") or DEFAULT_LIMIT), MAX_LIMIT)
    sort = "l.price_uzs DESC" if args.get("sort") == "price_desc" else "l.price_uzs ASC"
    filters = build_filters(args)
    sellable = "a.available_qty > 0 AND l.offer_valid_until >= current_date"

    where = " AND ".join([sellable] + [sql for sql, _ in filters])
    params = [p for _, values in filters for p in values]

    with connect() as conn:
        rows = conn.execute(f"{BASE_QUERY} WHERE {where} ORDER BY {sort} LIMIT %s", params + [limit]).fetchall()
        if rows or not filters:
            return {"matches": [to_card(r) for r in rows]}

        # Nothing matches every criterion: offer the laptops that match the most criteria.
        score = " + ".join(f"coalesce(({sql})::int, 0)" for sql, _ in filters)
        target_price = args.get("max_price_uzs") or args.get("min_price_uzs") or 0
        rows = conn.execute(
            f"""{BASE_QUERY} WHERE {sellable}
                ORDER BY ({score}) DESC, abs(l.price_uzs - %s) ASC
                LIMIT %s""",
            params + [int(target_price), limit],
        ).fetchall()
    return {
        "matches": [],
        "note": "No laptop matches all criteria. These are the closest ALTERNATIVES; say so clearly.",
        "alternatives": [to_card(r) for r in rows],
    }


def get_laptop(offer_no: str) -> dict:
    offer_no = offer_no.strip().upper()
    if not OFFER_NO.match(offer_no):
        return {"error": f"'{offer_no}' is not a valid offer number (format: SO followed by digits)."}
    with connect() as conn:
        row = conn.execute(f"{BASE_QUERY} WHERE l.offer_no = %s", [offer_no]).fetchone()
    if row is None:
        return {"found": False, "offer_no": offer_no, "note": "This offer number is not in our warehouse."}
    return {"found": True, "laptop": to_card(row, with_description=True)}


def compare_laptops(offer_nos: list[str]) -> dict:
    if not 2 <= len(offer_nos) <= 4:
        return {"error": "Give between 2 and 4 offer numbers to compare."}
    return {"laptops": [get_laptop(offer_no) for offer_no in offer_nos]}


TOOLS = {
    "search_laptops": {
        "function": search_laptops,
        "schema": {
            "name": "search_laptops",
            "description": (
                "Search laptops that are in stock. Use for any request to find, filter or recommend laptops. "
                "Returns at most 10. Prices are in UZS."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "brand": {"type": "string", "description": "e.g. HP, Lenovo, Asus, Apple"},
                    "max_price_uzs": {"type": "integer"},
                    "min_price_uzs": {"type": "integer"},
                    "min_ram_gb": {"type": "integer"},
                    "min_storage_gb": {"type": "integer"},
                    "min_screen_inch": {"type": "number"},
                    "max_screen_inch": {"type": "number"},
                    "text": {"type": "string", "description": "A word to find in model, CPU, GPU or description, e.g. 'RTX', 'MacBook Air', 'i7'"},
                    "sort": {"type": "string", "enum": ["price_asc", "price_desc"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
            },
        },
    },
    "get_laptop": {
        "function": get_laptop,
        "schema": {
            "name": "get_laptop",
            "description": "Full details, price and available units of one laptop by its offer number (SO followed by digits).",
            "parameters": {
                "type": "object",
                "properties": {"offer_no": {"type": "string"}},
                "required": ["offer_no"],
            },
        },
    },
    "compare_laptops": {
        "function": compare_laptops,
        "schema": {
            "name": "compare_laptops",
            "description": "Details of 2 to 4 laptops side by side, for comparison questions.",
            "parameters": {
                "type": "object",
                "properties": {"offer_nos": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4}},
                "required": ["offer_nos"],
            },
        },
    },
}
