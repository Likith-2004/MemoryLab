-- M6-2: "unlisted" means reachable by link, not listable. Through the Data API a non-owner now sees only
-- public runs in public.runs. The API looks a single run up by id through api_private.get_visible_run, which
-- also returns unlisted runs. run_visible() (used by the child tables, which are looked up by run id)
-- keeps unlisted visible.

drop policy runs_read on public.runs;
create policy runs_read on public.runs for select to anon, authenticated
  using (owner_id = auth.uid() or public.is_admin()
         or (visibility = 'public' and deleted_at is null));

create function api_private.get_visible_run(p_run_id uuid) returns setof public.runs
  language sql stable security definer set search_path = ''
  as $$
    select r.* from public.runs r
    where r.id = p_run_id
      and (r.owner_id = auth.uid() or public.is_admin()
           or (r.visibility in ('unlisted', 'public') and r.deleted_at is null))
  $$;

revoke all on function api_private.get_visible_run(uuid) from public;
grant usage on schema api_private to anon;
grant execute on function api_private.get_visible_run(uuid) to anon, authenticated;

-- A run's configuration (arbitrary JSON) follows the same rule: listing it through the Data API would
-- reveal an unlisted run's settings and its config id. Only the owner, an admin, or a public run's viewers.
drop policy run_configs_read on public.run_configs;
create policy run_configs_read on public.run_configs for select to anon, authenticated
  using (owner_id = auth.uid() or public.is_admin()
         or exists (select 1 from public.runs r
                    where r.config_id = run_configs.id and r.visibility = 'public' and r.deleted_at is null));
