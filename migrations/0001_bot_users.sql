CREATE TABLE bot_users (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider text NOT NULL CHECK (provider IN ('telegram', 'max')),
    provider_user_id text NOT NULL CHECK (btrim(provider_user_id) <> ''),
    username text,
    first_name text,
    last_name text,
    profile jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(profile) = 'object'),
    first_start_parameter text,
    current_start_parameter text,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_user_id)
);

CREATE TABLE bot_start_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES bot_users(id) ON DELETE CASCADE,
    start_parameter text,
    provider_update_id text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX bot_start_events_update_uidx
    ON bot_start_events (user_id, provider_update_id)
    WHERE provider_update_id IS NOT NULL;

CREATE INDEX bot_start_events_user_created_idx
    ON bot_start_events (user_id, created_at DESC);
