CREATE TABLE IF NOT EXISTS conversations (
    conversation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations (conversation_id) ON DELETE CASCADE,
    role            text NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content         text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS messages_conversation_idx
    ON messages (conversation_id, created_at);

CREATE TABLE IF NOT EXISTS pending_actions (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations (conversation_id) ON DELETE CASCADE,
    action_type     text NOT NULL CHECK (action_type IN ('create_booking', 'change_booking', 'cancel_booking')),
    payload         jsonb NOT NULL,
    summary_text    text NOT NULL,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'confirmed', 'rejected', 'expired', 'superseded')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    resolved_at     timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS pending_actions_one_open_idx
    ON pending_actions (conversation_id)
    WHERE status = 'pending';