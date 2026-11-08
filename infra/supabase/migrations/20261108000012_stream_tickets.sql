-- Live-stream tickets: a single-use, 60 s capability that keeps JWTs out of WebSocket URLs. Only the sha256 of the
-- ticket is stored. user_id is null for an anonymous viewer of a public/unlisted run. Redeeming is one atomic
-- DELETE ... RETURNING, so a ticket works once even under concurrent connects.

create table public.stream_tickets (
  token_sha256 text primary key check (token_sha256 ~ '^[0-9a-f]{64}$'),
  run_id uuid not null references public.runs (id) on delete cascade,
  user_id uuid references public.profiles (id) on delete cascade,
  expires_at timestamptz not null
);
create index stream_tickets_expiry_idx on public.stream_tickets (expires_at);
alter table public.stream_tickets enable row level security;
alter table public.stream_tickets force row level security;
revoke all on public.stream_tickets from anon, authenticated;
grant all on public.stream_tickets to service_role;

create function api_private.issue_stream_ticket(p_run_id uuid, p_token_sha256 text) returns void
  language plpgsql security definer set search_path = ''
  as $$
  begin
    if auth.uid() is not null then perform api_private.require_live_account(); end if;   -- an erased account: 401, not an FK error
    if not exists (select 1 from api_private.get_visible_run(p_run_id) v where v.deleted_at is null) then
      raise exception 'run_not_found' using errcode = 'P0001';
    end if;
    delete from public.stream_tickets where expires_at < now();
    insert into public.stream_tickets (token_sha256, run_id, user_id, expires_at)
      values (p_token_sha256, p_run_id, auth.uid(), now() + interval '60 seconds');
  end $$;

-- Consumes the ticket whether or not it is still valid; returns it only if it was.
create function api_private.redeem_stream_ticket(p_token_sha256 text) returns table (run_id uuid, user_id uuid)
  language sql security definer set search_path = ''
  as $$
    with taken as (
      delete from public.stream_tickets t where t.token_sha256 = p_token_sha256 returning t.run_id, t.user_id, t.expires_at
    )
    select taken.run_id, taken.user_id from taken where taken.expires_at > now()
  $$;

revoke all on function api_private.issue_stream_ticket(uuid, text) from public;
revoke all on function api_private.redeem_stream_ticket(text) from public;
grant execute on function api_private.issue_stream_ticket(uuid, text) to anon, authenticated;
grant execute on function api_private.redeem_stream_ticket(text) to anon, authenticated;
