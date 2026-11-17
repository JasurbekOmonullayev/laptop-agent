"""Booking tools. The LLM can only PREPARE an action; code executes it after the client's explicit "yes".

Flow: prepare_* validates and saves a summary in pending_actions -> the agent shows it -> the client answers ->
confirm_pending() runs the stored action in one transaction, with the laptop row locked.
"""
import re
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from app.config import BOOKING_HOLD_HOURS, MAX_UNITS_PER_BOOKING, SUMMARY_VALID_MINUTES
from app.db import connect

TASHKENT = ZoneInfo("Asia/Tashkent")
OFFER_NO = re.compile(r"^SO\d+$")
BOOKING_ID = re.compile(r"^BK-[0-9A-F]{8}$")
PHONE = re.compile(r"^\+998\d{9}$")

# Whole-message answers only: "ha, lekin 2 ta qiling" is not a yes.
YES = {"ha", "xa", "ha tasdiqlayman", "tasdiqlayman", "ha roziman", "roziman", "yes", "yes i confirm", "i confirm",
       "confirm", "ok", "да", "да подтверждаю", "подтверждаю", "согласен", "согласна", "ок"}
NO = {"yo'q", "yoq", "yo'q kerak emas", "kerak emas", "no", "cancel", "нет", "не надо", "отмена"}


class BookingError(Exception):
    """A rule was broken. The message is shown to the LLM, which explains it to the client."""


# ---------- small helpers ----------

def normalize_answer(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'").replace("ʻ", "'")
    text = re.sub(r"[^\w\s']", " ", text)
    return " ".join(text.split())


def is_yes(text: str) -> bool:
    return normalize_answer(text) in YES


def is_no(text: str) -> bool:
    return normalize_answer(text) in NO


def normalize_phone(phone: str) -> str:
    phone = re.sub(r"[\s\-()]", "", phone or "")
    return "+" + phone if phone.startswith("998") else phone


def format_uzs(amount: int) -> str:
    return f"{amount:,} UZS"


def format_time(moment) -> str:
    return moment.astimezone(TASHKENT).strftime("%Y-%m-%d %H:%M") + " (Tashkent time)"


def log_status(conn, booking_id: str, old: str | None, new: str, actor: str, note: str) -> None:
    conn.execute(
        "INSERT INTO booking_status_log (booking_id, old_status, new_status, actor, note) VALUES (%s, %s, %s, %s, %s)",
        [booking_id, old, new, actor, note],
    )


def expire_overdue(conn) -> None:
    """P03: a booking not collected within the hold period expires. Runs before every booking operation."""
    rows = conn.execute(
        "UPDATE bookings SET status = 'expired' WHERE status = 'confirmed' AND expires_at <= now() RETURNING booking_id"
    ).fetchall()
    for row in rows:
        log_status(conn, row["booking_id"], "confirmed", "expired", "system", "hold period ended")


# ---------- the rules, checked when preparing AND again when executing ----------

def lock_laptops(conn, offer_nos: set[str]) -> None:
    """Row lock: a second booking of the same laptop waits here until the first transaction ends.
    Sorted order, so two transactions never wait for each other (no deadlock)."""
    for offer_no in sorted(offer_nos):
        conn.execute("SELECT 1 FROM laptops WHERE offer_no = %s FOR UPDATE", [offer_no])


def check_request(conn, offer_no: str, quantity, phone: str, old: dict | None = None) -> dict:
    """Return the laptop if `quantity` units can be booked for `phone`; otherwise raise BookingError.
    `old` is the booking being changed: its units are counted as free for the new version."""
    if not OFFER_NO.match(offer_no):
        raise BookingError(f"'{offer_no}' is not a valid offer number.")
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise BookingError("Quantity must be a whole number.")
    if quantity < 1:
        raise BookingError("Quantity must be at least 1.")
    if quantity > MAX_UNITS_PER_BOOKING:
        raise BookingError(f"Shop rule P04: at most {MAX_UNITS_PER_BOOKING} units of one model per booking. "
                           f"Ask the client to lower the quantity. Never suggest splitting into several bookings.")

    laptop = conn.execute(
        """SELECT l.offer_no, l.brand, l.model, l.price_uzs, l.offer_valid_until, a.available_qty
           FROM laptops l JOIN laptop_availability a USING (offer_no) WHERE l.offer_no = %s""",
        [offer_no],
    ).fetchone()
    if laptop is None:
        raise BookingError(f"{offer_no} is not in our warehouse.")
    if laptop["offer_valid_until"] < conn.execute("SELECT current_date AS d").fetchone()["d"]:
        raise BookingError(f"The offer {offer_no} is no longer valid.")

    same_laptop = old is not None and old["offer_no"] == offer_no
    available = laptop["available_qty"] + (old["quantity"] if same_laptop else 0)
    if quantity > available:
        raise BookingError(f"Only {available} unit(s) of {offer_no} are available.")

    # P04 per client: units this phone already holds of this model, plus the new ones.
    held = conn.execute(
        """SELECT coalesce(sum(quantity), 0) AS units FROM bookings
           WHERE client_phone = %s AND offer_no = %s AND status = 'confirmed' AND expires_at > now()
             AND booking_id IS DISTINCT FROM %s""",
        [phone, offer_no, old["booking_id"] if old else None],
    ).fetchone()["units"]
    if held + quantity > MAX_UNITS_PER_BOOKING:
        raise BookingError(f"Shop rule P04: this client already holds {held} unit(s) of {offer_no}; "
                           f"the limit is {MAX_UNITS_PER_BOOKING} per model. Ask the client to lower the quantity.")
    return laptop


def find_booking(conn, booking_id: str, phone: str, lock: bool = False) -> dict:
    """The booking only if the phone matches: nobody sees or cancels someone else's booking."""
    booking_id = booking_id.strip().upper()
    if not BOOKING_ID.match(booking_id) or not PHONE.match(phone):
        raise BookingError("Give a booking ID like BK-1A2B3C4D and the phone number used for the booking.")
    row = conn.execute(
        f"""SELECT b.*, l.brand, l.model FROM bookings b JOIN laptops l USING (offer_no)
            WHERE b.booking_id = %s AND b.client_phone = %s {"FOR UPDATE OF b" if lock else ""}""",
        [booking_id, phone],
    ).fetchone()
    if row is None:
        raise BookingError("No booking with this ID and phone number.")
    return row


def booking_card(row: dict) -> dict:
    return {
        "booking_id": row["booking_id"],
        "laptop": f"{row['brand']} {row['model']} ({row['offer_no']})",
        "quantity": row["quantity"],
        "unit_price": format_uzs(row["unit_price_uzs"]),
        "total": format_uzs(row["total_uzs"]),
        "status": row["status"],
        "hold_ends": format_time(row["expires_at"]),
    }


def privacy_notice(conn) -> str:
    return conn.execute("SELECT rule_text FROM shop_policies WHERE policy_id = 'P10'").fetchone()["rule_text"]


def save_pending(conn, conversation_id: str, action_type: str, payload: dict, summary: dict) -> None:
    """Only one open action per conversation: a new summary replaces the old one."""
    conn.execute(
        """UPDATE pending_actions SET status = 'superseded', resolved_at = now()
           WHERE conversation_id = %s AND status = 'pending'""",
        [conversation_id],
    )
    conn.execute(
        """INSERT INTO pending_actions (conversation_id, action_type, payload, summary_text, expires_at)
           VALUES (%s, %s, %s, %s, now() + make_interval(mins => %s))""",
        [conversation_id, action_type, Jsonb(payload), str(summary), SUMMARY_VALID_MINUTES],
    )


def awaiting(summary: dict, conn, with_privacy: bool) -> dict:
    result = {
        "status": "awaiting_confirmation",
        "summary": summary,
        "note": "Show every line of this summary in the client's language and ask for an explicit yes. "
                "Nothing is done until the client answers yes.",
    }
    if with_privacy:
        result["privacy_notice"] = "Shop rule P10: " + privacy_notice(conn) + " The client's yes is also consent."
    return result


# ---------- tools the LLM can call ----------

def prepare_booking(conversation_id: str, offer_no: str, quantity: int, client_name: str, client_phone: str) -> dict:
    offer_no = offer_no.strip().upper()
    name = " ".join((client_name or "").split())
    phone = normalize_phone(client_phone)
    try:
        if not name:
            raise BookingError("The client's full name is needed.")
        if not PHONE.match(phone):
            raise BookingError("The phone number must look like +998XXXXXXXXX.")
        with connect() as conn:
            expire_overdue(conn)
            laptop = check_request(conn, offer_no, quantity, phone)
            quantity = int(quantity)
            payload = {"offer_no": offer_no, "quantity": quantity, "client_name": name,
                       "client_phone": phone, "unit_price_uzs": laptop["price_uzs"]}
            summary = {
                "action": "new booking",
                "laptop": f"{laptop['brand']} {laptop['model']} ({offer_no})",
                "quantity": quantity,
                "unit_price": format_uzs(laptop["price_uzs"]),
                "total": format_uzs(laptop["price_uzs"] * quantity),
                "name": name,
                "phone": phone,
                "hold": f"{BOOKING_HOLD_HOURS} hours from confirmation (shop rule P03)",
            }
            save_pending(conn, conversation_id, "create_booking", payload, summary)
            return awaiting(summary, conn, with_privacy=True)
    except BookingError as error:
        return {"error": str(error)}


def prepare_change(conversation_id: str, booking_id: str, client_phone: str,
                   new_offer_no: str | None = None, new_quantity: int | None = None) -> dict:
    phone = normalize_phone(client_phone)
    try:
        with connect() as conn:
            expire_overdue(conn)
            old = find_booking(conn, booking_id, phone)
            if old["status"] != "confirmed":
                raise BookingError(f"Booking {old['booking_id']} is {old['status']} and cannot be changed.")
            offer_no = (new_offer_no or old["offer_no"]).strip().upper()
            quantity = new_quantity or old["quantity"]
            laptop = check_request(conn, offer_no, quantity, phone, old=old)
            quantity = int(quantity)
            if offer_no == old["offer_no"] and quantity == old["quantity"]:
                raise BookingError("The new booking would be the same as the old one.")
            payload = {"booking_id": old["booking_id"], "client_phone": phone, "offer_no": offer_no,
                       "quantity": quantity, "unit_price_uzs": laptop["price_uzs"]}
            summary = {
                "action": "change booking",
                "old": booking_card(old),
                "new": {
                    "laptop": f"{laptop['brand']} {laptop['model']} ({offer_no})",
                    "quantity": quantity,
                    "unit_price": format_uzs(laptop["price_uzs"]),
                    "total": format_uzs(laptop["price_uzs"] * quantity),
                    "hold": f"{BOOKING_HOLD_HOURS} hours from confirmation (shop rule P03)",
                },
            }
            save_pending(conn, conversation_id, "change_booking", payload, summary)
            return awaiting(summary, conn, with_privacy=False)
    except BookingError as error:
        return {"error": str(error)}


def prepare_cancellation(conversation_id: str, booking_id: str, client_phone: str) -> dict:
    phone = normalize_phone(client_phone)
    try:
        with connect() as conn:
            expire_overdue(conn)
            booking = find_booking(conn, booking_id, phone)
            if booking["status"] != "confirmed":
                raise BookingError(f"Booking {booking['booking_id']} is already {booking['status']}.")
            payload = {"booking_id": booking["booking_id"], "client_phone": phone}
            summary = {"action": "cancel booking", **booking_card(booking)}
            save_pending(conn, conversation_id, "cancel_booking", payload, summary)
            return awaiting(summary, conn, with_privacy=False)
    except BookingError as error:
        return {"error": str(error)}


def get_booking(booking_id: str, client_phone: str) -> dict:
    try:
        with connect() as conn:
            expire_overdue(conn)
            return {"booking": booking_card(find_booking(conn, booking_id, normalize_phone(client_phone)))}
    except BookingError as error:
        return {"error": str(error)}


# ---------- execution: called by code (agent.py) on the client's message, never by the LLM ----------

def insert_booking(conn, payload: dict, client_name: str, conversation_id: str, client_message: str) -> dict:
    return conn.execute(
        """INSERT INTO bookings (offer_no, conversation_id, quantity, client_name, client_phone,
                                 unit_price_uzs, total_uzs, expires_at, confirmation_message)
           VALUES (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(hours => %s), %s)
           RETURNING *""",
        [payload["offer_no"], conversation_id, payload["quantity"], client_name, payload["client_phone"],
         payload["unit_price_uzs"], payload["unit_price_uzs"] * payload["quantity"],
         BOOKING_HOLD_HOURS, client_message],
    ).fetchone()


def check_price(laptop: dict, payload: dict) -> None:
    if laptop["price_uzs"] != payload["unit_price_uzs"]:
        raise BookingError("The price changed after the summary was shown. Prepare a new summary.")


def execute_create(conn, payload: dict, conversation_id: str, client_message: str) -> dict:
    lock_laptops(conn, {payload["offer_no"]})
    laptop = check_request(conn, payload["offer_no"], payload["quantity"], payload["client_phone"])
    check_price(laptop, payload)
    row = insert_booking(conn, payload, payload["client_name"], conversation_id, client_message)
    log_status(conn, row["booking_id"], None, "confirmed", "client", "created after explicit confirmation")
    return {"booking_id": row["booking_id"], "hold_ends": format_time(row["expires_at"]),
            "total": format_uzs(row["total_uzs"])}


def execute_cancel(conn, payload: dict, conversation_id: str, client_message: str) -> dict:
    booking = find_booking(conn, payload["booking_id"], payload["client_phone"], lock=True)
    if booking["status"] != "confirmed":
        raise BookingError(f"Booking {booking['booking_id']} is already {booking['status']}.")
    conn.execute("UPDATE bookings SET status = 'cancelled', cancelled_at = now() WHERE booking_id = %s",
                 [booking["booking_id"]])
    log_status(conn, booking["booking_id"], "confirmed", "cancelled", "client", "cancelled after explicit confirmation")
    return {"cancelled": booking["booking_id"]}


def execute_change(conn, payload: dict, conversation_id: str, client_message: str) -> dict:
    """Change = cancel the old booking and create the new one in the same transaction: both or neither."""
    old = find_booking(conn, payload["booking_id"], payload["client_phone"], lock=True)
    if old["status"] != "confirmed":
        raise BookingError(f"Booking {old['booking_id']} is {old['status']} and cannot be changed.")
    lock_laptops(conn, {old["offer_no"], payload["offer_no"]})
    laptop = check_request(conn, payload["offer_no"], payload["quantity"], payload["client_phone"], old=old)
    check_price(laptop, payload)
    conn.execute("UPDATE bookings SET status = 'cancelled', cancelled_at = now() WHERE booking_id = %s",
                 [old["booking_id"]])
    new = insert_booking(conn, payload, old["client_name"], conversation_id, client_message)
    log_status(conn, old["booking_id"], "confirmed", "cancelled", "client", f"replaced by {new['booking_id']}")
    log_status(conn, new["booking_id"], None, "confirmed", "client", f"replaces {old['booking_id']}")
    return {"old_booking_id": old["booking_id"], "booking_id": new["booking_id"],
            "hold_ends": format_time(new["expires_at"]), "total": format_uzs(new["total_uzs"])}


EXECUTORS = {"create_booking": execute_create, "cancel_booking": execute_cancel, "change_booking": execute_change}


def open_action(conn, conversation_id: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM pending_actions WHERE conversation_id = %s AND status = 'pending' FOR UPDATE",
        [conversation_id],
    ).fetchone()


def has_pending(conversation_id: str) -> bool:
    with connect() as conn:
        return open_action(conn, conversation_id) is not None


def confirm_pending(conversation_id: str, client_message: str) -> dict:
    """Run the stored action. The check for an explicit yes is here, in code, not in the prompt."""
    if not is_yes(client_message):
        return {"done": False, "reason": "The client's message is not an explicit yes."}
    with connect() as conn:
        expire_overdue(conn)
        action = open_action(conn, conversation_id)
        if action is None:
            return {"done": False, "reason": "There is nothing waiting for confirmation."}
        if action["expires_at"] <= conn.execute("SELECT now() AS t").fetchone()["t"]:
            resolve(conn, action, "expired")
            return {"done": False, "reason": f"The summary is older than {SUMMARY_VALID_MINUTES} minutes. "
                                             "Prepare it again and show the new summary."}
        try:
            with conn.transaction():  # savepoint: if a rule fails now, none of the action's writes stay
                result = EXECUTORS[action["action_type"]](conn, action["payload"], conversation_id, client_message)
        except BookingError as error:
            resolve(conn, action, "expired")
            return {"done": False, "reason": str(error)}
        resolve(conn, action, "confirmed")
        return {"done": True, "action": action["action_type"], **result}


def reject_pending(conversation_id: str) -> dict:
    with connect() as conn:
        action = open_action(conn, conversation_id)
        if action is None:
            return {"done": False, "reason": "There is nothing waiting for confirmation."}
        resolve(conn, action, "rejected")
        return {"done": True, "action": "rejected", "rejected": action["action_type"]}


def resolve(conn, action: dict, status: str) -> None:
    conn.execute("UPDATE pending_actions SET status = %s, resolved_at = now() WHERE id = %s", [status, action["id"]])


# ---------- what the LLM sees ----------

PHONE_SCHEMA = {"type": "string", "description": "Client's phone, +998XXXXXXXXX"}
BOOKING_ID_SCHEMA = {"type": "string", "description": "e.g. BK-1A2B3C4D"}

TOOLS = {
    "prepare_booking": {
        "function": prepare_booking,
        "needs_conversation": True,
        "schema": {
            "name": "prepare_booking",
            "description": (
                "Check a new booking and prepare its summary. It does NOT book: the booking is made by the system "
                "only after the client answers yes to the summary. Call it when you know the offer number, "
                "quantity, full name and phone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "offer_no": {"type": "string"},
                    "quantity": {"type": "integer", "minimum": 1},
                    "client_name": {"type": "string", "description": "Full name"},
                    "client_phone": PHONE_SCHEMA,
                },
                "required": ["offer_no", "quantity", "client_name", "client_phone"],
            },
        },
    },
    "prepare_change": {
        "function": prepare_change,
        "needs_conversation": True,
        "schema": {
            "name": "prepare_change",
            "description": (
                "Prepare a change of an existing booking (another laptop and/or quantity). Shows old and new side "
                "by side. Nothing changes until the client answers yes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "booking_id": BOOKING_ID_SCHEMA,
                    "client_phone": PHONE_SCHEMA,
                    "new_offer_no": {"type": "string", "description": "Omit to keep the same laptop"},
                    "new_quantity": {"type": "integer", "minimum": 1, "description": "Omit to keep the quantity"},
                },
                "required": ["booking_id", "client_phone"],
            },
        },
    },
    "prepare_cancellation": {
        "function": prepare_cancellation,
        "needs_conversation": True,
        "schema": {
            "name": "prepare_cancellation",
            "description": "Prepare the cancellation of a booking. Nothing is cancelled until the client answers yes.",
            "parameters": {
                "type": "object",
                "properties": {"booking_id": BOOKING_ID_SCHEMA, "client_phone": PHONE_SCHEMA},
                "required": ["booking_id", "client_phone"],
            },
        },
    },
    "get_booking": {
        "function": get_booking,
        "needs_conversation": False,
        "schema": {
            "name": "get_booking",
            "description": "Status and details of a booking, by booking ID and the phone number used for it.",
            "parameters": {
                "type": "object",
                "properties": {"booking_id": BOOKING_ID_SCHEMA, "client_phone": PHONE_SCHEMA},
                "required": ["booking_id", "client_phone"],
            },
        },
    },
}