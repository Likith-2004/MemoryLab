-- Admin moderation: every action is audited with a reason; a report moves open -> actioned | dismissed once;
-- a run taken off the leaderboard stays off.

alter table public.audit_log add column reason text check (char_length(reason) <= 500);
alter table public.reports
  add column resolved_at timestamptz,
  add column resolved_by uuid,
  add column resolution_reason text check (char_length(resolution_reason) <= 500);
alter table public.run_scores
  add column unverified_at timestamptz,
  add column unverified_by uuid,
  add column unverified_reason text check (char_length(unverified_reason) <= 500);

-- The only legal report change is open -> actioned | dismissed (with who and why); the rest is immutable. A trigger,
-- so it holds for every writer.
create function public.reports_guard() returns trigger
  language plpgsql set search_path = ''
  as $$
  begin
    if (new.reporter_id, new.run_id, new.reason, new.created_at) is distinct from
       (old.reporter_id, old.run_id, old.reason, old.created_at) then
      raise exception 'report_immutable' using errcode = 'P0001';
    end if;
    if new.status is distinct from old.status then
      if old.status <> 'open' or new.status not in ('actioned', 'dismissed') then
        raise exception 'report_bad_transition' using errcode = 'P0001';
      end if;
      if new.resolved_at is null or new.resolved_by is null or new.resolution_reason is null then
        raise exception 'report_resolution_incomplete' using errcode = 'P0001';
      end if;
    elsif (new.resolved_at, new.resolved_by, new.resolution_reason) is distinct from
          (old.resolved_at, old.resolved_by, old.resolution_reason) then
      raise exception 'report_bad_transition' using errcode = 'P0001';
    end if;
    return new;
  end $$;
create trigger reports_guard before update on public.reports
  for each row execute function public.reports_guard();

-- A run an admin took off the leaderboard cannot be put back by the scoring service by accident.
create function public.run_scores_guard() returns trigger
  language plpgsql set search_path = ''
  as $$
  begin
    if old.unverified_at is not null and new.verified then
      raise exception 'unverified_locked' using errcode = 'P0001';
    end if;
    return new;
  end $$;
create trigger run_scores_guard before update on public.run_scores
  for each row execute function public.run_scores_guard();

create function api_private.require_admin() returns uuid
  language plpgsql stable security definer set search_path = ''
  as $$
  declare uid uuid := api_private.require_live_account();
  begin
    if not public.is_admin() then raise exception 'not_admin' using errcode = 'P0001'; end if;
    return uid;
  end $$;

create function api_private.admin_audit(p_action text, p_target text, p_reason text, p_ip_hash text) returns void
  language plpgsql security definer set search_path = ''
  as $$
  declare uid uuid := api_private.require_admin();
  begin
    if p_reason is null or char_length(btrim(p_reason)) not between 1 and 500 then
      raise exception 'reason_required' using errcode = 'P0001';
    end if;
    insert into public.audit_log (actor_id, action, target, ip_hash, reason)
      values (uid, p_action, p_target, p_ip_hash, p_reason);
  end $$;

create function api_private.admin_resolve_report(p_id uuid, p_status text, p_reason text, p_ip_hash text) returns void
  language plpgsql security definer set search_path = ''
  as $$
  declare
    uid uuid := api_private.require_admin();
    current text;
  begin
    if p_status not in ('actioned', 'dismissed') then raise exception 'report_bad_transition' using errcode = 'P0001'; end if;
    if p_reason is null or char_length(btrim(p_reason)) not between 1 and 500 then
      raise exception 'reason_required' using errcode = 'P0001';
    end if;
    select status into current from public.reports where id = p_id for update;
    if not found then raise exception 'report_not_found' using errcode = 'P0001'; end if;
    if current <> 'open' then raise exception 'report_closed' using errcode = 'P0001'; end if;
    update public.reports set status = p_status, resolved_at = now(), resolved_by = uid, resolution_reason = p_reason
      where id = p_id;
    insert into public.audit_log (actor_id, action, target, ip_hash, reason)
      values (uid, 'report.' || p_status, p_id::text, p_ip_hash, p_reason);
  end $$;

create function api_private.admin_unverify(p_run_id uuid, p_reason text, p_ip_hash text) returns void
  language plpgsql security definer set search_path = ''
  as $$
  declare uid uuid := api_private.require_admin();
  begin
    if p_reason is null or char_length(btrim(p_reason)) not between 1 and 500 then
      raise exception 'reason_required' using errcode = 'P0001';
    end if;
    update public.run_scores set verified = false, unverified_at = now(), unverified_by = uid, unverified_reason = p_reason
      where run_id = p_run_id and verified;
    if not found then
      if exists (select 1 from public.run_scores where run_id = p_run_id) then
        raise exception 'not_verified' using errcode = 'P0001';
      end if;
      raise exception 'run_not_found' using errcode = 'P0001';
    end if;
    insert into public.audit_log (actor_id, action, target, ip_hash, reason)
      values (uid, 'leaderboard.unverify', p_run_id::text, p_ip_hash, p_reason);
  end $$;

revoke all on function api_private.require_admin() from public, anon;
revoke all on function api_private.admin_audit(text, text, text, text) from public, anon;
revoke all on function api_private.admin_resolve_report(uuid, text, text, text) from public, anon;
revoke all on function api_private.admin_unverify(uuid, text, text) from public, anon;
grant execute on function api_private.admin_audit(text, text, text, text) to authenticated;
grant execute on function api_private.admin_resolve_report(uuid, text, text, text) to authenticated;
grant execute on function api_private.admin_unverify(uuid, text, text) to authenticated;
