"""Required test 3: booking status changes, each one logged with time and actor."""
from app.tools import bookings
from helpers import available, query

PHONE = "+998901234567"


def book(conversation, offer_no="SO1000010", quantity=1) -> str:
    bookings.prepare_booking(conversation, offer_no, quantity, "Ali Valiyev", PHONE)
    return bookings.confirm_pending(conversation, "ha")["booking_id"]


def status_log(booking_id: str) -> list[tuple]:
    rows = query("SELECT old_status, new_status, actor FROM booking_status_log WHERE booking_id = %s ORDER BY id",
                 [booking_id])
    return [(row["old_status"], row["new_status"], row["actor"]) for row in rows]


def test_new_booking_is_confirmed_and_logged(conversation):
    booking_id = book(conversation, quantity=2)
    booking = query("SELECT * FROM bookings WHERE booking_id = %s", [booking_id])[0]
    assert booking["status"] == "confirmed"
    assert booking["total_uzs"] == 2 * 12_000_000
    assert (booking["expires_at"] - booking["created_at"]).total_seconds() == 48 * 3600  # P03
    assert status_log(booking_id) == [(None, "confirmed", "client")]
    assert available("SO1000010") == 8


def test_cancel_releases_stock_and_is_logged(conversation):
    booking_id = book(conversation)
    bookings.prepare_cancellation(conversation, booking_id, PHONE)
    assert bookings.confirm_pending(conversation, "ha")["done"] is True

    booking = query("SELECT status, cancelled_at FROM bookings WHERE booking_id = %s", [booking_id])[0]
    assert booking["status"] == "cancelled" and booking["cancelled_at"] is not None
    assert status_log(booking_id)[-1] == ("confirmed", "cancelled", "client")
    assert available("SO1000010") == 10


def test_cancelled_booking_cannot_be_cancelled_again(conversation):
    booking_id = book(conversation)
    bookings.prepare_cancellation(conversation, booking_id, PHONE)
    bookings.confirm_pending(conversation, "ha")
    assert "already cancelled" in bookings.prepare_cancellation(conversation, booking_id, PHONE)["error"]


def test_hold_period_end_expires_the_booking(conversation):
    booking_id = book(conversation)
    query("UPDATE bookings SET expires_at = now() - interval '1 second' WHERE booking_id = %s RETURNING 1",
          [booking_id])
    assert available("SO1000010") == 10  # the view ignores an overdue hold at once

    bookings.get_booking(booking_id, PHONE)  # any booking operation closes overdue holds
    assert query("SELECT status FROM bookings WHERE booking_id = %s", [booking_id])[0]["status"] == "expired"
    assert status_log(booking_id)[-1] == ("confirmed", "expired", "system")


def test_change_cancels_old_and_creates_new_in_one_step(conversation):
    old_id = book(conversation, offer_no="SO1000010", quantity=1)
    bookings.prepare_change(conversation, old_id, PHONE, new_offer_no="SO1000003", new_quantity=2)
    result = bookings.confirm_pending(conversation, "yes")
    new_id = result["booking_id"]

    assert query("SELECT status FROM bookings WHERE booking_id = %s", [old_id])[0]["status"] == "cancelled"
    assert query("SELECT status, quantity FROM bookings WHERE booking_id = %s", [new_id])[0] == \
        {"status": "confirmed", "quantity": 2}
    assert status_log(old_id)[-1] == ("confirmed", "cancelled", "client")
    assert status_log(new_id) == [(None, "confirmed", "client")]
    assert available("SO1000010") == 10 and available("SO1000003") == 1


def test_only_the_owner_phone_sees_or_cancels(conversation):
    booking_id = book(conversation)
    assert "error" in bookings.get_booking(booking_id, "+998909999999")
    assert "error" in bookings.prepare_cancellation(conversation, booking_id, "+998909999999")
    assert bookings.get_booking(booking_id, PHONE)["booking"]["status"] == "confirmed"


def test_p04_limit_per_booking_and_per_client(conversation):
    assert "P04" in bookings.prepare_booking(conversation, "SO1000010", 4, "Ali Valiyev", PHONE)["error"]
    book(conversation, quantity=2)
    result = bookings.prepare_booking(conversation, "SO1000010", 2, "Ali Valiyev", PHONE)
    assert "already holds 2" in result["error"]
    