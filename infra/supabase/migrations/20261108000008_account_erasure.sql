-- DELETE /me support. The accounts service (service role) anonymises verified public runs by moving them to a
-- tombstone profile and soft-deletes everything else before the auth user is removed. Without that, the
-- profiles -> runs ON DELETE CASCADE would delete the public benchmark runs along with the account.

-- The tombstone is an auth user nobody can sign in as: no password, no confirmed email, banned.
insert into auth.users (id, instance_id, aud, role, email, banned_until)
  values ('00000000-0000-0000-0000-00000000dead', '00000000-0000-0000-0000-000000000000', 'authenticated',
          'authenticated', 'erased-user@memorylab.invalid', 'infinity')
  on conflict (id) do nothing;

alter table public.profiles add column erased_at timestamptz;
update public.profiles set erased_at = now() where id = '00000000-0000-0000-0000-00000000dead';

-- A signed-in token outlives the erase by up to its TTL: an erased account must not be able to start a run.
create or replace function api_private.create_run_request(
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
    insert into public.runs (owner_id, config_id, parent_checkpoint_id, visibility, cap_steps, cap_tokens, cap_wall_s)
      values (uid, cfg_id, p_parent_checkpoint_id, p_visibility, p_cap_steps, p_cap_tokens, p_cap_wall_s)
      returning id into run_id;
    return run_id;
  end $$;

-- The tombstone owns every anonymised run; deleting its profile would cascade-delete them.
create function public.protect_tombstone() returns trigger
  language plpgsql set search_path = ''
  as $$
  begin
    if old.id = '00000000-0000-0000-0000-00000000dead' then
      raise exception 'tombstone_protected' using errcode = 'P0001';
    end if;
    return old;
  end $$;
create trigger protect_tombstone before delete on public.profiles
  for each row execute function public.protect_tombstone();
