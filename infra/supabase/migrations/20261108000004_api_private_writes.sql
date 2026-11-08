-- Run/report creation goes through api_private functions, not direct table writes.
-- A user JWT could INSERT into run_configs/runs/reports/experiments through PostgREST and skip every API
-- check (validation, quotas, caps, one-active-run, game). Those grants are removed; the API calls the
-- functions below as the caller's own role, so auth.uid() is still the identity.

revoke insert on public.run_configs, public.runs, public.reports, public.experiments from authenticated;
revoke update on public.experiments from authenticated;
drop policy run_configs_insert on public.run_configs;
drop policy runs_insert on public.runs;
drop policy reports_insert on public.reports;
drop policy experiments_insert on public.experiments;
drop policy experiments_update on public.experiments;
-- runs keeps `update (title, visibility)`: runs_guard_update and the RLS policy guard it.

-- Backstop for the cap maxima (the same bounds as labharness Caps and create_run_request).
alter table public.runs
  add constraint runs_cap_steps_max check (cap_steps <= 100000),
  add constraint runs_cap_tokens_max check (cap_tokens <= 1000000000),
  add constraint runs_cap_wall_s_max check (cap_wall_s <= 86400);

alter table public.reports add column created_at timestamptz not null default now();
create unique index reports_one_open_per_reporter_run on public.reports (reporter_id, run_id)
  where status = 'open';

-- ---------------------------------------------------------------- api_private
-- This schema must NEVER be listed in [api] schemas (infra/supabase/config.toml): PostgREST would expose
-- its functions as /rpc endpoints. Only the API process, connecting as `authenticated`, calls them.

create schema api_private;
revoke all on schema api_private from public, anon;
grant usage on schema api_private to authenticated;
alter default privileges in schema api_private revoke execute on functions from public, anon;

create function api_private.create_run_request(
  p_config_json jsonb, p_config_hash text, p_mode text, p_visibility text,
  p_cap_steps integer, p_cap_tokens bigint, p_cap_wall_s integer, p_parent_checkpoint_id uuid default null)
  returns uuid
  language plpgsql security definer set search_path = ''
  as $$
  declare
    uid uuid := auth.uid();
    cfg_id uuid;
    run_id uuid;
  begin
    if uid is null then raise exception 'not_authenticated' using errcode = 'P0001'; end if;
    if p_cap_steps not between 1 and 100000 or p_cap_tokens not between 1 and 1000000000
       or p_cap_wall_s not between 1 and 86400 then
      raise exception 'caps_out_of_range' using errcode = 'P0001';
    end if;
    if p_config_json is null or octet_length(p_config_json::text) > 65536 then
      raise exception 'config_invalid' using errcode = 'P0001';
    end if;
    -- one create at a time per user: the active-run check and the insert are atomic with respect to it
    perform pg_advisory_xact_lock(hashtextextended('run:' || uid::text, 0));
    if exists (select 1 from public.runs r where r.owner_id = uid
               and r.status in ('queued', 'starting', 'running', 'session_boundary', 'dreaming', 'stopping')) then
      raise exception 'active_run_exists' using errcode = 'P0001';
    end if;
    if p_parent_checkpoint_id is not null and not exists (
         select 1 from public.checkpoints c where c.id = p_parent_checkpoint_id and public.run_visible(c.run_id)) then
      raise exception 'parent_checkpoint_invalid' using errcode = 'P0001';
    end if;
    insert into public.run_configs (owner_id, config_json, config_hash, mode)
      values (uid, p_config_json, p_config_hash, p_mode) returning id into cfg_id;
    insert into public.runs (owner_id, config_id, parent_checkpoint_id, visibility, cap_steps, cap_tokens, cap_wall_s)
      values (uid, cfg_id, p_parent_checkpoint_id, p_visibility, p_cap_steps, p_cap_tokens, p_cap_wall_s)
      returning id into run_id;
    return run_id;
  end $$;

-- Moves the caller's own still-queued run to failed when the orchestrator refused it.
create function api_private.abandon_run_request(p_run_id uuid) returns boolean
  language plpgsql security definer set search_path = ''
  as $$
  declare n integer;
  begin
    update public.runs set status = 'failed', end_reason = 'submit_refused', ended_at = now()
      where id = p_run_id and owner_id = auth.uid() and status = 'queued';
    get diagnostics n = row_count;
    return n > 0;
  end $$;

-- Reports: only for a run the caller can see, idempotent while open, 10 new reports per hour.
create function api_private.file_report(p_run_id uuid, p_reason text) returns uuid
  language plpgsql security definer set search_path = ''
  as $$
  declare
    uid uuid := auth.uid();
    rid uuid;
  begin
    if uid is null then raise exception 'not_authenticated' using errcode = 'P0001'; end if;
    if p_reason is null or char_length(p_reason) not between 1 and 1000 then
      raise exception 'reason_invalid' using errcode = 'P0001';
    end if;
    perform pg_advisory_xact_lock(hashtextextended('report:' || uid::text, 0));
    if not public.run_visible(p_run_id) then raise exception 'run_not_found' using errcode = 'P0001'; end if;
    if exists (select 1 from public.runs r where r.id = p_run_id and r.owner_id = uid) then
      raise exception 'cannot_report_own_run' using errcode = 'P0001';
    end if;
    select id into rid from public.reports where reporter_id = uid and run_id = p_run_id and status = 'open';
    if rid is not null then return rid; end if;
    if (select count(*) from public.reports where reporter_id = uid and created_at > now() - interval '1 hour') >= 10 then
      raise exception 'report_rate_limited' using errcode = 'P0001';
    end if;
    insert into public.reports (reporter_id, run_id, reason) values (uid, p_run_id, p_reason) returning id into rid;
    return rid;
  end $$;

revoke all on function api_private.create_run_request(jsonb, text, text, text, integer, bigint, integer, uuid)
  from public, anon;
revoke all on function api_private.abandon_run_request(uuid) from public, anon;
revoke all on function api_private.file_report(uuid, text) from public, anon;
grant execute on function api_private.create_run_request(jsonb, text, text, text, integer, bigint, integer, uuid)
  to authenticated;
grant execute on function api_private.abandon_run_request(uuid) to authenticated;
grant execute on function api_private.file_report(uuid, text) to authenticated;
