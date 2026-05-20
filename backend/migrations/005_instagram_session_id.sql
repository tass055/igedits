-- Migration 005: Add session ID auth method to instagram_credentials
-- Allows users to connect via browser session cookie instead of username/password,
-- bypassing IP-based login challenges on server deployments.

ALTER TABLE instagram_credentials
    ADD COLUMN IF NOT EXISTS enc_session_id TEXT,
    ADD COLUMN IF NOT EXISTS auth_method VARCHAR(20) NOT NULL DEFAULT 'password';

ALTER TABLE instagram_credentials
    ALTER COLUMN enc_password DROP NOT NULL;
