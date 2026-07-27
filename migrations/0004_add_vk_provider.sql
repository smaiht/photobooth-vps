ALTER TABLE bot_users
    DROP CONSTRAINT bot_users_provider_check;

ALTER TABLE bot_users
    ADD CONSTRAINT bot_users_provider_check
    CHECK (provider IN ('telegram', 'vk', 'max'));
