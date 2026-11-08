-- 1. GoTrue cannot scan banned_until = 'infinity' (it reads the column into a time.Time), nor NULL token or
--    timestamp columns: with the tombstone as inserted in migration 8, GET /auth/v1/admin/users answered 500 for the
--    whole project and /token, /recover, /otp for its email did too. Use a far-future date, real timestamps and empty
--    strings, as GoTrue itself writes.
update auth.users set
  banned_until = '9999-12-31 00:00:00+00', created_at = coalesce(created_at, now()), updated_at = coalesce(updated_at, now()),
  confirmation_token = '', recovery_token = '', email_change_token_new = '', email_change = '',
  email_change_token_current = '', phone_change = '', phone_change_token = '', reauthentication_token = ''
where id = '00000000-0000-0000-0000-00000000dead';

-- 2. A token can outlive the erase of its account by up to its TTL. Every api_private write refuses a caller who has
--    no live profile (erased, or deleted) with `account_erased`, which the API maps to 401, instead of failing on a
--    foreign key (500).
create function api_private.require_live_account() returns uuid
  language plpgsql stable security definer set search_path = ''
  as $$
  declare uid uuid := auth.uid();
  begin
    if uid is null then raise exception 'not_authenticated' using errcode = 'P0001'; end if;
    if not exists (select 1 from public.profiles p where p.id = uid and p.erased_at is null) then
      raise exception 'account_erased' using errcode = 'P0001';
    end if;
    return uid;
  end $$;
revoke all on function api_private.require_live_account() from public, anon;
grant execute on function api_private.require_live_account() to authenticated;

create or replace function api_private.file_report(p_run_id uuid, p_reason text) returns uuid
  language plpgsql security definer set search_path = ''
  as $$
  declare
    uid uuid := api_private.require_live_account();
    rid uuid;
  begin
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

create or replace function api_private.create_run_request(
  p_config_json jsonb, p_config_hash text, p_mode text, p_visibility text,
  p_cap_steps integer, p_cap_tokens bigint, p_cap_wall_s integer, p_game text,
  p_parent_checkpoint_id uuid default null, p_fork_patch jsonb default null)
  returns uuid
  language plpgsql security definer set search_path = ''
  as $$
  declare
    uid uuid := api_private.require_live_account();
    cfg_id uuid;
    run_id uuid;
  begin
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
    if not exists (select 1 from public.profiles p where p.id = uid and p.erased_at is null) then
      raise exception 'account_erased' using errcode = 'P0001';        -- re-checked under the lock
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
