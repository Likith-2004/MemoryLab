-- Local development only (runs on `supabase db reset`, never as a migration): a fixed password for the API's
-- login role so the default MEMORYLAB_DB_URL works against the local stack. Production sets its own secret.
alter role memorylab_api password 'memorylab-api-local';
