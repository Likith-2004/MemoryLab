-- Admin kill/breaker are external calls. The audit row is committed as 'requested' before the call and its outcome
-- (ok / failed / unknown: a timeout after the request was sent) is recorded afterwards. audit_log is append-only:
-- the one allowed change is requested -> ok | failed | unknown.

alter table public.audit_log add column outcome text not null default 'ok'
  check (outcome in ('requested', 'ok', 'failed', 'unknown'));

create function public.audit_log_guard() returns trigger
  language plpgsql set search_path = ''
  as $$
  begin
    if tg_op = 'DELETE' then raise exception 'audit_append_only' using errcode = 'P0001'; end if;
    if old.outcome <> 'requested' or new.outcome = 'requested'
       or (new.actor_id, new.action, new.target, new.ip_hash, new.reason, new.ts) is distinct from
          (old.actor_id, old.action, old.target, old.ip_hash, old.reason, old.ts) then
      raise exception 'audit_append_only' using errcode = 'P0001';
    end if;
    return new;
  end $$;
create trigger audit_log_guard before update or delete on public.audit_log
  for each row execute function public.audit_log_guard();

drop function api_private.admin_audit(text, text, text, text);
create function api_private.admin_audit(p_action text, p_target text, p_reason text, p_ip_hash text) returns bigint
  language plpgsql security definer set search_path = ''
  as $$
  declare
    uid uuid := api_private.require_admin();
    aid bigint;
  begin
    if p_reason is null or char_length(btrim(p_reason)) not between 1 and 500 then
      raise exception 'reason_required' using errcode = 'P0001';
    end if;
    insert into public.audit_log (actor_id, action, target, ip_hash, reason, outcome)
      values (uid, p_action, p_target, p_ip_hash, p_reason, 'requested') returning id into aid;
    return aid;
  end $$;

-- Only the admin who made the request can close it out.
create function api_private.admin_audit_outcome(p_id bigint, p_outcome text) returns void
  language plpgsql security definer set search_path = ''
  as $$
  declare uid uuid := api_private.require_admin();
  begin
    if p_outcome not in ('ok', 'failed', 'unknown') then raise exception 'bad_outcome' using errcode = 'P0001'; end if;
    update public.audit_log set outcome = p_outcome where id = p_id and actor_id = uid and outcome = 'requested';
    if not found then raise exception 'audit_not_found' using errcode = 'P0001'; end if;
  end $$;

revoke all on function api_private.admin_audit(text, text, text, text) from public, anon;
revoke all on function api_private.admin_audit_outcome(bigint, text) from public, anon;
grant execute on function api_private.admin_audit(text, text, text, text) to authenticated;
grant execute on function api_private.admin_audit_outcome(bigint, text) to authenticated;
