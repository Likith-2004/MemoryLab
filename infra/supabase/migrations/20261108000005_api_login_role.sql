-- The API connects as `memorylab_api`, not as the postgres superuser. It holds no table privileges of its own
-- and does not bypass RLS: every request switches to `anon` or `authenticated` with `set local role`
-- (api.db.Database.apply_identity). A code path that forgets to do so fails with permission denied
-- instead of running with BYPASSRLS. The password is set outside the repository (local dev: seed.sql).

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'memorylab_api') then
    create role memorylab_api login noinherit nobypassrls nosuperuser nocreatedb nocreaterole noreplication;
  end if;
end $$;

grant anon, authenticated to memorylab_api;
