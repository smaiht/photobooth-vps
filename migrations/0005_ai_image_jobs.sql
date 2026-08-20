CREATE TABLE ai_image_jobs (
    id uuid PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES bot_users(id),
    event_name text NOT NULL CHECK (btrim(event_name) <> ''),
    conversation_id text NOT NULL CHECK (btrim(conversation_id) <> ''),
    source_message_id text,
    choice_message_id text,
    source_suffix text NOT NULL CHECK (btrim(source_suffix) <> ''),
    result_suffix text,
    status text NOT NULL DEFAULT 'receiving' CHECK (status IN (
        'receiving',
        'awaiting_template',
        'queued',
        'processing',
        'ready',
        'printing',
        'print_submitted',
        'failed',
        'cancelled'
    )),
    template_id text,
    template_label text,
    prompt text,
    created_at timestamptz NOT NULL DEFAULT now(),
    selected_at timestamptz,
    queued_at timestamptz,
    processing_at timestamptz,
    ready_at timestamptz,
    delivered_at timestamptz,
    print_requested_at timestamptz,
    closed_at timestamptz,
    close_reason text,
    last_error text
);

CREATE UNIQUE INDEX ai_image_jobs_one_active_per_user_uidx
    ON ai_image_jobs (user_id)
    WHERE status IN (
        'receiving',
        'awaiting_template',
        'queued',
        'processing'
    );

CREATE INDEX ai_image_jobs_queue_idx
    ON ai_image_jobs (queued_at, created_at)
    WHERE status = 'queued';

CREATE INDEX ai_image_jobs_undelivered_idx
    ON ai_image_jobs (ready_at)
    WHERE status = 'ready' AND delivered_at IS NULL;
