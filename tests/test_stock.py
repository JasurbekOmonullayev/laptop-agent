"""Required test 1: stock never goes negative, even when clients book the last units at the same moment."""
import threading

from app.tools import bookings
from helpers import available, booking_count, query


def new_conversation() -> str:
    return str(query("INSERT INTO conversations DEFAULT VALUES RETURNING conversation_id")[0]["conversation_id"])


def test_cannot_book_more_than_available(conversation):
    result = bookings.prepare_booking(conversation, "SO1000003", 3, "Ali Valiyev", "+998901234567")
    assert result["status"] == "awaiting_confirmation"
    result = bookings.prepare_booking(conversation, "SO1000001", 2, "Ali Valiyev", "+998901234567")
    assert "Only 1 unit" in result["error"]


def test_out_of_stock_laptop_cannot_be_booked(conversation):
    result = bookings.prepare_booking(conversation, "SO1000000", 1, "Ali Valiyev", "+998901234567")
    assert "Only 0 unit" in result["error"]
    assert booking_count() == 0


def test_last_unit_goes_to_the_first_yes():
    """Two clients saw a summary for the last unit; only the first "yes" gets it."""
    first, second = new_conversation(), new_conversation()
    bookings.prepare_booking(first, "SO1000001", 1, "Ali Valiyev", "+998901111111")
    bookings.prepare_booking(second, "SO1000001", 1, "Vali Aliyev", "+998902222222")

    assert bookings.confirm_pending(first, "ha")["done"] is True
    late = bookings.confirm_pending(second, "ha")
    assert late["done"] is False and "Only 0 unit" in late["reason"]
    assert available("SO1000001") == 0


def test_parallel_clients_never_oversell():
    """15 clients press "yes" at the same moment for a laptop with 3 units: exactly 3 bookings are made."""
    conversations = [new_conversation() for _ in range(15)]
    for i, conversation in enumerate(conversations):
        result = bookings.prepare_booking(conversation, "SO1000003", 1, f"Client {i}", f"+99890{i:07d}")
        assert result["status"] == "awaiting_confirmation"

    start = threading.Barrier(len(conversations))
    results = []

    def press_yes(conversation):
        start.wait()  # all threads continue together, so the transactions really overlap
        results.append(bookings.confirm_pending(conversation, "ha"))

    threads = [threading.Thread(target=press_yes, args=(c,)) for c in conversations]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(result["done"] for result in results) == 3
    assert available("SO1000003") == 0
    assert query("SELECT count(*) AS n FROM laptop_availability WHERE available_qty < 0")[0]["n"] == 0


def test_change_does_not_oversell(conversation):
    """Changing a booking to another laptop must respect that laptop's stock too."""
    bookings.prepare_booking(conversation, "SO1000010", 1, "Ali Valiyev", "+998901234567")
    booking_id = bookings.confirm_pending(conversation, "ha")["booking_id"]

    result = bookings.prepare_change(conversation, booking_id, "+998901234567", new_offer_no="SO1000001", new_quantity=2)
    assert "Only 1 unit" in result["error"]
    