-- Forks: a run records its game and, when forked, the config diff it applied; the create function takes both.
-- api_private readers give the API a fork source's config and one of its checkpoints under get_visible_run.

alter table public.runs add column game text check (char_length(game) <= 32);
alter table public.runs add column fork_patch jsonb check (octet_length(fork_patch::text) <= 16384);

drop function api_private.create_run_request(jsonb, text, text, text, integer, bigint, integer, uuid);

create function api_private.create_run_request(
  p_config_json jsonb, p_config_hash text, p_mode text, p_visibility text,
  p_cap_steps integer, p_cap_tokens bigint, p_cap_wall_s integer, p_game text,
  p_parent_checkpoint_id uuid default null, p_fork_patch jsonb default null)
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
    if p_game is null or char_length(p_game) not between 1 and 32 then
      raise exception 'game_invalid' using errcode = 'P0001';
    end if;
    -- one create at a time per user: the active-run check and the insert are atomic with respect to it
    -- (the accounts service takes the same lock when it erases an account)
    perform pg_advisory_xact_lock(hashtextextended('run:' || uid::text, 0));
    if exists (select 1 from public.profiles p where p.id = uid and p.erased_at is not null) then
      raise exception 'account_erased' using errcode = 'P0001';
    end if;
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
    insert into public.runs (owner_id, config_id, parent_checkpoint_id, visibility, cap_steps, cap_tokens, cap_wall_s,
                             game, fork_patch)
      values (uid, cfg_id, p_parent_checkpoint_id, p_visibility, p_cap_steps, p_cap_tokens, p_cap_wall_s,
              p_game, p_fork_patch)
      returning id into run_id;
    return run_id;
  end $$;

revoke all on function api_private.create_run_request(jsonb, text, text, text, integer, bigint, integer, text, uuid, jsonb)
  from public, anon;
grant execute on function api_private.create_run_request(jsonb, text, text, text, integer, bigint, integer, text, uuid, jsonb)
  to authenticated;

-- A fork's source: the run's config and one checkpoint of that run, only if the caller may see the run.
create function api_private.run_config(p_run_id uuid)
  returns table (config_json jsonb, config_hash text, mode text)
  language sql stable security definer set search_path = ''
  as $$
    select c.config_json, c.config_hash, c.mode from public.run_configs c
    join public.runs r on r.config_id = c.id
    where r.id = p_run_id
      and exists (select 1 from api_private.get_visible_run(p_run_id) v where v.deleted_at is null)
  $$;

create function api_private.run_checkpoint(p_run_id uuid, p_checkpoint_id uuid)
  returns table (id uuid, step integer, memory_version integer, config_hash text)
  language sql stable security definer set search_path = ''
  as $$
    select c.id, c.step, c.memory_version, c.config_hash from public.checkpoints c
    where c.id = p_checkpoint_id and c.run_id = p_run_id
      and exists (select 1 from api_private.get_visible_run(p_run_id) v where v.deleted_at is null)
  $$;

revoke all on function api_private.run_config(uuid) from public;
revoke all on function api_private.run_checkpoint(uuid, uuid) from public;
grant execute on function api_private.run_config(uuid) to authenticated;
grant execute on function api_private.run_checkpoint(uuid, uuid) to authenticated;
