"""Load data/laptops_warehouse.xlsx into the laptops and shop_policies tables.

Cleaning rule: never guess. A value that is missing or not plausible is stored
as NULL, and the seller's original text stays in `description`.
"""
import os
import re
from collections import Counter
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from openpyxl import load_workbook

PROJECT_DIR = Path(__file__).resolve().parent.parent
XLSX_PATH = PROJECT_DIR / "data" / "laptops_warehouse.xlsx"

LAPTOP_COLUMNS = [
    "offer_no", "brand", "model", "cpu", "ram_gb", "storage_gb", "screen_inch",
    "resolution", "gpu", "os", "country", "price_uzs", "stock_qty",
    "warranty_months", "delivery_days", "offer_valid_until", "description", "source_url",
]
POLICY_COLUMNS = ["policy_id", "topic", "rule"]

# Plausible ranges. A value outside the range is treated as an input error.
RAM_RANGE = (4, 256)          # GB
STORAGE_RANGE = (64, 16384)   # GB
SCREEN_RANGE = (10, 19)       # inches
WARRANTY_RANGE = (1, 60)      # months
DELIVERY_RANGE = (1, 60)      # days

MIN_PATTERN = re.compile(r"min\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
WXH_PATTERN = re.compile(r"(\d{3,4})\s*[xх×]\s*(\d{3,4})", re.IGNORECASE)
RESOLUTION_NAMES = {
    "fhd": "1920x1080",
    "full hd": "1920x1080",
    "wuxga": "1920x1200",
    "wqxga": "2560x1600",
    "ultra hd": "3840x2160",
}

stats = Counter()


def get_conninfo() -> str:
    load_dotenv(PROJECT_DIR / ".env")
    return psycopg.conninfo.make_conninfo(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


def read_sheet(workbook, sheet_name: str, expected_columns: list[str]) -> list[dict]:
    rows = workbook[sheet_name].iter_rows(values_only=True)
    header = list(next(rows))
    if header != expected_columns:
        raise ValueError(f"{sheet_name}: unexpected columns {header}")
    return [dict(zip(header, row)) for row in rows if any(row)]


def clean_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def clean_spec(value, valid_range: tuple[float, float], column: str) -> tuple[float | None, bool]:
    """Return (number, is_min). 'min 16' -> (16, True). Implausible -> (None, False)."""
    if value is None or str(value).strip() == "":
        return None, False

    is_min = False
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        match = MIN_PATTERN.fullmatch(str(value).strip())
        if match is None:
            stats[f"{column}: unreadable -> NULL"] += 1
            return None, False
        number = float(match.group(1))
        is_min = True

    low, high = valid_range
    if not low <= number <= high:
        stats[f"{column}: {value!r} out of range -> NULL"] += 1
        return None, False
    if is_min:
        stats[f"{column}: 'min N' -> N, is_min=true"] += 1
    return number, is_min


def clean_term(value, valid_range: tuple[int, int], column: str) -> int | None:
    low, high = valid_range
    if value is None:
        return None
    if not low <= int(value) <= high:
        stats[f"{column}: {value} out of range -> NULL"] += 1
        return None
    return int(value)


def clean_price(value) -> int:
    price = Decimal(str(value))
    rounded = int(price.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if rounded != price:
        stats["price_uzs: rounded to whole soum"] += 1
    if rounded <= 0:
        raise ValueError(f"price must be positive, got {value}")
    return rounded


def clean_resolution(value) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    match = WXH_PATTERN.fullmatch(text)
    if match:
        return f"{match.group(1)}x{match.group(2)}"
    if text.lower() in RESOLUTION_NAMES:
        return RESOLUTION_NAMES[text.lower()]
    if any(sign in text for sign in "<>≥≤"):
        stats["resolution: size class, not a resolution -> NULL"] += 1
        return None
    return text


def clean_laptop(row: dict) -> dict:
    ram_gb, ram_is_min = clean_spec(row["ram_gb"], RAM_RANGE, "ram_gb")
    storage_gb, storage_is_min = clean_spec(row["storage_gb"], STORAGE_RANGE, "storage_gb")
    screen_inch, screen_is_min = clean_spec(row["screen_inch"], SCREEN_RANGE, "screen_inch")

    stock_qty = int(row["stock_qty"])
    if stock_qty < 0:
        raise ValueError(f"{row['offer_no']}: negative stock {stock_qty}")

    return {
        "offer_no": clean_text(row["offer_no"]),
        "brand": clean_text(row["brand"]),
        "model": clean_text(row["model"]),
        "cpu": clean_text(row["cpu"]),
        "ram_gb": int(ram_gb) if ram_gb is not None else None,
        "ram_is_min": ram_is_min,
        "storage_gb": int(storage_gb) if storage_gb is not None else None,
        "storage_is_min": storage_is_min,
        "screen_inch": screen_inch,
        "screen_is_min": screen_is_min,
        "resolution": clean_resolution(row["resolution"]),
        "gpu": clean_text(row["gpu"]),
        "os": clean_text(row["os"]),
        "country": clean_text(row["country"]),
        "price_uzs": clean_price(row["price_uzs"]),
        "stock_qty": stock_qty,
        "warranty_months": clean_term(row["warranty_months"], WARRANTY_RANGE, "warranty_months"),
        "delivery_days": clean_term(row["delivery_days"], DELIVERY_RANGE, "delivery_days"),
        "offer_valid_until": date.fromisoformat(str(row["offer_valid_until"]).strip()),
        "description": clean_text(row["description"]),
        "source_url": clean_text(row["source_url"]),
    }


UPSERT_LAPTOP = """
INSERT INTO laptops (
    offer_no, brand, model, cpu, ram_gb, ram_is_min, storage_gb, storage_is_min,
    screen_inch, screen_is_min, resolution, gpu, os, country, price_uzs, stock_qty,
    warranty_months, delivery_days, offer_valid_until, description, source_url
) VALUES (
    %(offer_no)s, %(brand)s, %(model)s, %(cpu)s, %(ram_gb)s, %(ram_is_min)s,
    %(storage_gb)s, %(storage_is_min)s, %(screen_inch)s, %(screen_is_min)s,
    %(resolution)s, %(gpu)s, %(os)s, %(country)s, %(price_uzs)s, %(stock_qty)s,
    %(warranty_months)s, %(delivery_days)s, %(offer_valid_until)s,
    %(description)s, %(source_url)s
)
ON CONFLICT (offer_no) DO UPDATE SET
    brand = EXCLUDED.brand, model = EXCLUDED.model, cpu = EXCLUDED.cpu,
    ram_gb = EXCLUDED.ram_gb, ram_is_min = EXCLUDED.ram_is_min,
    storage_gb = EXCLUDED.storage_gb, storage_is_min = EXCLUDED.storage_is_min,
    screen_inch = EXCLUDED.screen_inch, screen_is_min = EXCLUDED.screen_is_min,
    resolution = EXCLUDED.resolution, gpu = EXCLUDED.gpu, os = EXCLUDED.os,
    country = EXCLUDED.country, price_uzs = EXCLUDED.price_uzs,
    stock_qty = EXCLUDED.stock_qty, warranty_months = EXCLUDED.warranty_months,
    delivery_days = EXCLUDED.delivery_days,
    offer_valid_until = EXCLUDED.offer_valid_until,
    description = EXCLUDED.description, source_url = EXCLUDED.source_url
"""

UPSERT_POLICY = """
INSERT INTO shop_policies (policy_id, topic, rule_text)
VALUES (%(policy_id)s, %(topic)s, %(rule_text)s)
ON CONFLICT (policy_id) DO UPDATE SET
    topic = EXCLUDED.topic, rule_text = EXCLUDED.rule_text
"""


def main() -> None:
    workbook = load_workbook(XLSX_PATH, read_only=True, data_only=True)
    laptops = [clean_laptop(row) for row in read_sheet(workbook, "Laptops", LAPTOP_COLUMNS)]
    policies = [
        {
            "policy_id": clean_text(row["policy_id"]),
            "topic": clean_text(row["topic"]),
            "rule_text": clean_text(row["rule"]),
        }
        for row in read_sheet(workbook, "Shop_Policies", POLICY_COLUMNS)
    ]

    with psycopg.connect(get_conninfo()) as conn:
        with conn.cursor() as cur:
            cur.executemany(UPSERT_LAPTOP, laptops)
            cur.executemany(UPSERT_POLICY, policies)

    print(f"laptops:       {len(laptops)} rows")
    print(f"shop_policies: {len(policies)} rows")
    print("cleaning report:")
    for reason, count in sorted(stats.items()):
        print(f"  {count:4d}  {reason}")


if __name__ == "__main__":
    main()