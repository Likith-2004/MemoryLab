-- GET /runs/{id}/export: the status and bundle hash of a run the caller may see. run_ingest_state is
-- service-only, so the API reads the hash through this function.

create function api_private.run_bundle(p_run_id uuid) returns table (status text, bundle_sha256 text)
  language sql stable security definer set search_path = ''
  as $$
    select r.status, s.bundle_sha256
    from public.runs r left join public.run_ingest_state s on s.run_id = r.id
    where r.id = p_run_id
      and exists (select 1 from api_private.get_visible_run(p_run_id) v where v.deleted_at is null)
  $$;

revoke all on function api_private.run_bundle(uuid) from public;
grant execute on function api_private.run_bundle(uuid) to anon, authenticated;
