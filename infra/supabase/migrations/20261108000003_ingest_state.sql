-- Ingest persistence. Everything here is service-role only (no grants to anon/authenticated).

-- runs.status follows the lifecycle in runlog.lifecycle (the first schema listed a shorter set).
alter table public.runs drop constraint runs_status_check;
alter table public.runs add constraint runs_status_check check (status in (
  'queued', 'starting', 'running', 'session_boundary', 'dreaming', 'stopping',
  'completed', 'capped', 'failed', 'killed'));

-- Columns the ingest service records per event / memory row.
alter table public.run_events_index add column segment integer not null default 0;
alter table public.memory_files add column rev integer;

create table public.run_ingest_state (
  run_id uuid primary key references public.runs (id) on delete cascade,
  next_batch_no jsonb not null default '{"worker": 1, "orchestrator": 1}',
  event_count integer not null default 0,
  bytes_total bigint not null default 0,
  frames integer not null default 0,
  score integer not null default 0,
  closed boolean not null default false,
  manifest_sealed boolean not null default false,
  last_ack jsonb not null default '{}',
  bundle_sha256 text check (bundle_sha256 ~ '^[0-9a-f]{64}$')
);
alter table public.run_ingest_state enable row level security;
alter table public.run_ingest_state force row level security;
revoke all on public.run_ingest_state from anon, authenticated;
grant all on public.run_ingest_state to service_role;
