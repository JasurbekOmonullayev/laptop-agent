CREATE TABLE IF NOT EXISTS bookings (
    booking_id           text PRIMARY KEY DEFAULT ('BK-' || upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 8))),
    offer_no             text NOT NULL REFERENCES laptops (offer_no),
    conversation_id      uuid REFERENCES conversations (conversation_id) ON DELETE SET NULL,
    quantity             integer NOT NULL CHECK (quantity BETWEEN 1 AND 3),
    client_name          text NOT NULL CHECK (length(trim(client_name)) > 0),
    client_phone         text NOT NULL CHECK (client_phone ~ '^\+998[0-9]{9}$'),
    unit_price_uzs       bigint NOT NULL CHECK (unit_price_uzs > 0),
    total_uzs            bigint NOT NULL,
    status               text NOT NULL DEFAULT 'confirmed'
                         CHECK (status IN ('confirmed', 'cancelled', 'expired', 'completed')),
    created_at           timestamptz NOT NULL DEFAULT now(),
    expires_at           timestamptz NOT NULL,
    cancelled_at         timestamptz,
    confirmation_message text NOT NULL,
    CONSTRAINT bookings_total_check CHECK (total_uzs = unit_price_uzs * quantity),
    CONSTRAINT bookings_cancelled_at_check CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS bookings_active_idx
    ON bookings (offer_no)
    WHERE status = 'confirmed';

CREATE INDEX IF NOT EXISTS bookings_phone_idx
    ON bookings (client_phone);

CREATE TABLE IF NOT EXISTS booking_status_log (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    booking_id  text NOT NULL REFERENCES bookings (booking_id),
    old_status  text,
    new_status  text NOT NULL
                CHECK (new_status IN ('confirmed', 'cancelled', 'expired', 'completed')),
    actor       text NOT NULL CHECK (actor IN ('client', 'agent', 'system')),
    note        text,
    changed_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS booking_status_log_booking_idx
    ON booking_status_log (booking_id, changed_at);

CREATE OR REPLACE VIEW laptop_availability AS
SELECT
    l.offer_no,
    l.stock_qty,
    COALESCE(SUM(b.quantity), 0)               AS held_qty,
    l.stock_qty - COALESCE(SUM(b.quantity), 0) AS available_qty
FROM laptops l
LEFT JOIN bookings b
       ON b.offer_no = l.offer_no
      AND b.status = 'confirmed'
      AND b.expires_at > now()
GROUP BY l.offer_no, l.stock_qty;