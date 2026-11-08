-- M6-2, second half: the tables that follow a run (events index, memory files, checkpoints, scores) are
-- listable through the Data API, so their policies must use the same rule as public.runs: owner, admin, or a
-- public run. run_visible() also admits unlisted runs; row policies evaluate it per row, which made it a
-- listing path (`GET /rest/v1/memory_files` returned an unlisted run's memory). It stays for lookups by id
-- (file_report, parent checkpoint).
--
-- Unlisted runs are still served by link: the API reads their children through the api_private readers below,
-- which re-check api_private.get_visible_run(run) inside.

create function public.run_listable(p_run_id uuid) returns boolean
  language sql stable security definer set search_path = ''
  as $$
    select exists (
      select 1 from public.runs r
      where r.id = p_run_id
        and (r.owner_id = auth.uid() or public.is_admin()
             or (r.visibility = 'public' and r.deleted_at is null))
    )
  $$;

drop policy run_events_index_read on public.run_events_index;
drop policy memory_files_read on public.memory_files;
drop policy checkpoints_read on public.checkpoints;
drop policy run_scores_read on public.run_scores;
create policy run_events_index_read on public.run_events_index for select to anon, authenticated
  using (public.run_listable(run_id));
create policy memory_files_read on public.memory_files for select to anon, authenticated
  using (public.run_listable(run_id));
create policy checkpoints_read on public.checkpoints for select to anon, authenticated
  using (public.run_listable(run_id));
create policy run_scores_read on public.run_scores for select to anon, authenticated
  using (public.run_listable(run_id));

-- Readers for link-only (unlisted) runs. Same visibility as the API's lookup: get_visible_run, and not deleted.
create function api_private.run_memory_tree(p_run_id uuid, p_at_step integer)
  returns table (path text, chars integer, version integer, deleted boolean)
  language sql stable security definer set search_path = ''
  as $$
    select distinct on (m.path) m.path, m.chars, m.version, m.deleted
    from public.memory_files m
    where m.run_id = p_run_id and m.step <= p_at_step
      and exists (select 1 from api_private.get_visible_run(p_run_id) v where v.deleted_at is null)
    order by m.path, m.version desc
  $$;

create function api_private.run_memory_version(p_run_id uuid, p_path text, p_version integer)
  returns table (content text, deleted boolean)
  language sql stable security definer set search_path = ''
  as $$
    select m.content, m.deleted from public.memory_files m
    where m.run_id = p_run_id and m.path = p_path and (p_version is null or m.version = p_version)
      and exists (select 1 from api_private.get_visible_run(p_run_id) v where v.deleted_at is null)
    order by m.version desc limit 1
  $$;

create function api_private.run_checkpoints(p_run_id uuid)
  returns table (id uuid, step integer, memory_version integer, config_hash text)
  language sql stable security definer set search_path = ''
  as $$
    select c.id, c.step, c.memory_version, c.config_hash from public.checkpoints c
    where c.run_id = p_run_id
      and exists (select 1 from api_private.get_visible_run(p_run_id) v where v.deleted_at is null)
    order by c.step
  $$;

create function api_private.run_score(p_run_id uuid) returns setof public.run_scores
  language sql stable security definer set search_path = ''
  as $$
    select s.* from public.run_scores s
    where s.run_id = p_run_id
      and exists (select 1 from api_private.get_visible_run(p_run_id) v where v.deleted_at is null)
  $$;

revoke all on function api_private.run_memory_tree(uuid, integer) from public;
revoke all on function api_private.run_memory_version(uuid, text, integer) from public;
revoke all on function api_private.run_checkpoints(uuid) from public;
revoke all on function api_private.run_score(uuid) from public;
grant execute on function api_private.run_memory_tree(uuid, integer) to anon, authenticated;
grant execute on function api_private.run_memory_version(uuid, text, integer) to anon, authenticated;
grant execute on function api_private.run_checkpoints(uuid) to anon, authenticated;
grant execute on function api_private.run_score(uuid) to anon, authenticated;
