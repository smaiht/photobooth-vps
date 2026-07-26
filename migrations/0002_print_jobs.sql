CREATE TABLE print_jobs (
    id uuid PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES bot_users(id),
    event_name text NOT NULL CHECK (btrim(event_name) <> ''),
    conversation_id text NOT NULL CHECK (btrim(conversation_id) <> ''),
    source_message_id text,
    choice_message_id text,
    status text NOT NULL DEFAULT 'processing' CHECK (status IN (
        'processing',
        'awaiting_choice',
        'awaiting_authorization',
        'authorized',
        'dispatching',
        'queued',
        'failed',
        'cancelled'
    )),
    print_mode text CHECK (print_mode IN ('fit', 'fill')),
    authorization_kind text CHECK (authorization_kind IN (
        'allowlist',
        'event',
        'credit',
        'payment',
        'cashier'
    )),
    command_id uuid UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    selected_at timestamptz,
    authorized_at timestamptz,
    queued_at timestamptz,
    closed_at timestamptz,
    close_reason text,
    last_error text
);

CREATE UNIQUE INDEX print_jobs_one_open_per_user_uidx
    ON print_jobs (user_id)
    WHERE status IN (
        'processing',
        'awaiting_choice',
        'awaiting_authorization',
        'authorized',
        'dispatching'
    );

CREATE INDEX print_jobs_user_event_queued_idx
    ON print_jobs (user_id, event_name, queued_at DESC)
    WHERE status = 'queued';

CREATE INDEX print_jobs_created_idx
    ON print_jobs (created_at DESC);
