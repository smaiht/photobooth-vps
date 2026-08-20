ALTER TABLE ai_image_jobs
    ADD COLUMN provider_task_id text
        CHECK (provider_task_id IS NULL OR btrim(provider_task_id) <> ''),
    ADD COLUMN next_poll_at timestamptz,
    ADD COLUMN provider_deadline_at timestamptz;

CREATE UNIQUE INDEX ai_image_jobs_provider_task_uidx
    ON ai_image_jobs (provider_task_id)
    WHERE provider_task_id IS NOT NULL;

CREATE INDEX ai_image_jobs_poll_idx
    ON ai_image_jobs (next_poll_at)
    WHERE status = 'processing' AND provider_task_id IS NOT NULL;
