ALTER TABLE ai_image_jobs
    DROP COLUMN provider_task_id,
    DROP COLUMN next_poll_at,
    DROP COLUMN provider_deadline_at;
