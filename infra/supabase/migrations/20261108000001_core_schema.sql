-- Memory Lab core schema. RLS is enabled and forced on every table.
-- Writes that matter (status, counters, scores, memory, events) are made only by service_role.
-- A user JWT can create a run request and change title/visibility of its own run; nothing else.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- identity

create table public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  handle text unique check (handle ~ '^[a-z0-9_]{3,24}$'),
  role text not null default 'user' check (role in ('user', 'admin')),
  created_at timestamptz not null default now()
);

-- Role checks read profiles.role, never JWT claims. SECURITY DEFINER avoids policy recursion.
create function public.is_admin() returns boolean
  language sql stable security definer set search_path = ''
  as $$ select exists (select 1 from public.profiles where id = auth.uid() and role = 'admin') $$;

create function public.handle_new_user() returns trigger
  language plpgsql security definer set search_path = ''
  as $$ begin insert into public.profiles (id) values (new.id); return new; end $$;

create trigger on_auth_user_created after insert on auth.users
  for each row execute function public.handle_new_user();

-- ---------------------------------------------------------------- catalogue

create table public.games (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  rom_sha256 text not null check (rom_sha256 ~ '^[0-9a-f]{64}$'),
  licence text not null,
  state_map_version text not null,
  public boolean not null default false
);

create table public.start_states (
  id uuid primary key default gen_random_uuid(),
  game_id uuid not null references public.games (id) on delete cascade,
  savestate_sha256 text not null check (savestate_sha256 ~ '^[0-9a-f]{64}$'),
  label text not null
);

create table public.presets (
  id text not null,
  version integer not null,
  text_sha256 text not null check (text_sha256 ~ '^[0-9a-f]{64}$'),
  text text not null,
  label text not null,
  is_adaptation boolean not null default false,
  primary key (id, version)
);

-- ---------------------------------------------------------------- runs

create table public.run_configs (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references public.profiles (id) on delete cascade,
  config_json jsonb not null,
  config_hash text not null check (config_hash ~ '^[0-9a-f]{64}$'),
  mode text not null check (mode in ('demo', 'benchmark'))
);

create table public.runs (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references public.profiles (id) on delete cascade,
  config_id uuid not null references public.run_configs (id),
  parent_checkpoint_id uuid,
  title text check (char_length(title) <= 120),
  status text not null default 'queued'
    check (status in ('queued', 'running', 'completed', 'capped', 'failed', 'killed', 'stopped')),
  visibility text not null default 'private' check (visibility in ('private', 'unlisted', 'public')),
  harness_version text,
  model_id text,
  provider text,
  cap_steps integer not null check (cap_steps > 0),
  cap_tokens bigint not null check (cap_tokens > 0),
  cap_wall_s integer not null check (cap_wall_s > 0),
  steps integer not null default 0,
  tokens bigint not null default 0,
  started_at timestamptz,
  ended_at timestamptz,
  end_reason text,
  created_at timestamptz not null default now(),
  deleted_at timestamptz
);
create index runs_owner_idx on public.runs (owner_id, created_at desc);

create table public.run_events_index (
  run_id uuid not null references public.runs (id) on delete cascade,
  seq bigint not null,
  type text not null,
  step integer,
  ts timestamptz not null,
  blob_offset bigint not null,
  primary key (run_id, seq)
);

create table public.memory_files (
  run_id uuid not null references public.runs (id) on delete cascade,
  path text not null,
  version integer not null,
  step integer not null,
  content text not null,
  chars integer not null,
  deleted boolean not null default false,
  primary key (run_id, path, version)
);

create table public.checkpoints (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs (id) on delete cascade,
  step integer not null,
  savestate_key text not null,
  memory_version integer not null,
  transcript_offset bigint not null,
  config_hash text not null
);

create table public.run_scores (
  run_id uuid primary key references public.runs (id) on delete cascade,
  progress_score integer not null,
  steps integer not null,
  tokens_in bigint not null,
  tokens_out bigint not null,
  memory_ops integer not null,
  loops integer not null,
  est_cost_usd numeric(12, 6) not null,
  verified boolean not null default false
);

create table public.experiments (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references public.profiles (id) on delete cascade,
  title text not null check (char_length(title) <= 120),
  variable text not null,
  run_ids uuid[] not null default '{}',
  visibility text not null default 'private' check (visibility in ('private', 'unlisted', 'public'))
);

create table public.audit_log (
  id bigint generated always as identity primary key,
  actor_id uuid,
  action text not null,
  target text,
  ip_hash text,
  ts timestamptz not null default now()
);

create table public.reports (
  id uuid primary key default gen_random_uuid(),
  reporter_id uuid not null references public.profiles (id) on delete cascade,
  run_id uuid not null references public.runs (id) on delete cascade,
  reason text not null check (char_length(reason) <= 1000),
  status text not null default 'open' check (status in ('open', 'actioned', 'dismissed'))
);

-- ---------------------------------------------------------------- visibility helpers

-- One definition of "can this caller see this run" shared by the run-following tables.
create function public.run_visible(p_run_id uuid) returns boolean
  language sql stable security definer set search_path = ''
  as $$
    select exists (
      select 1 from public.runs r
      where r.id = p_run_id
        and (r.owner_id = auth.uid() or public.is_admin()
             or (r.visibility <> 'private' and r.deleted_at is null))
    )
  $$;

-- Benchmark rule: a run that is on the leaderboard (verified) cannot be made private again.
create function public.runs_guard_update() returns trigger
  language plpgsql security definer set search_path = ''
  as $$
  begin
    if auth.role() = 'service_role' then return new; end if;
    if new.visibility = 'private' and old.visibility <> 'private'
       and exists (select 1 from public.run_scores s where s.run_id = old.id and s.verified) then
      raise exception 'benchmark_visibility_locked' using errcode = 'P0001';
    end if;
    return new;
  end $$;
create trigger runs_guard_update before update on public.runs
  for each row execute function public.runs_guard_update();

-- ---------------------------------------------------------------- RLS

do $$
declare t text;
begin
  for t in select unnest(array['profiles','games','start_states','presets','run_configs','runs',
    'run_events_index','memory_files','checkpoints','run_scores','experiments','audit_log','reports'])
  loop
    execute format('alter table public.%I enable row level security', t);
    execute format('alter table public.%I force row level security', t);
    execute format('revoke all on public.%I from anon, authenticated', t);
  end loop;
end $$;

-- profiles: read/update self; admin read all
grant select on public.profiles to authenticated;
grant update (handle) on public.profiles to authenticated;
create policy profiles_read on public.profiles for select to authenticated
  using (id = auth.uid() or public.is_admin());
create policy profiles_update_self on public.profiles for update to authenticated
  using (id = auth.uid()) with check (id = auth.uid());

-- games / start_states: read if public (service writes)
grant select on public.games, public.start_states to anon, authenticated;
create policy games_read on public.games for select to anon, authenticated using (public);
create policy start_states_read on public.start_states for select to anon, authenticated
  using (exists (select 1 from public.games g where g.id = game_id and g.public));

-- presets: read all
grant select on public.presets to anon, authenticated;
create policy presets_read on public.presets for select to anon, authenticated using (true);

-- run_configs: owner creates; read if the owner or a linked run is visible
grant select on public.run_configs to anon, authenticated;
grant insert (owner_id, config_json, config_hash, mode) on public.run_configs to authenticated;
create policy run_configs_read on public.run_configs for select to anon, authenticated
  using (owner_id = auth.uid() or public.is_admin()
         or exists (select 1 from public.runs r
                    where r.config_id = run_configs.id and public.run_visible(r.id)));
create policy run_configs_insert on public.run_configs for insert to authenticated
  with check (owner_id = auth.uid());

-- runs: owner reads all of theirs; others read if not private; user creates a queued request
-- and edits title/visibility only. status/counters are service-written.
grant select on public.runs to anon, authenticated;
grant insert (owner_id, config_id, parent_checkpoint_id, title, visibility, cap_steps, cap_tokens, cap_wall_s)
  on public.runs to authenticated;
grant update (title, visibility) on public.runs to authenticated;
create policy runs_read on public.runs for select to anon, authenticated
  using (owner_id = auth.uid() or public.is_admin()
         or (visibility <> 'private' and deleted_at is null));
create policy runs_insert on public.runs for insert to authenticated
  with check (owner_id = auth.uid() and status = 'queued');
create policy runs_update_owner on public.runs for update to authenticated
  using (owner_id = auth.uid() and deleted_at is null)
  with check (owner_id = auth.uid());

-- run-following tables: select only, via run_visible()
grant select on public.run_events_index, public.memory_files, public.checkpoints, public.run_scores
  to anon, authenticated;
create policy run_events_index_read on public.run_events_index for select to anon, authenticated
  using (public.run_visible(run_id));
create policy memory_files_read on public.memory_files for select to anon, authenticated
  using (public.run_visible(run_id));
create policy checkpoints_read on public.checkpoints for select to anon, authenticated
  using (public.run_visible(run_id));
create policy run_scores_read on public.run_scores for select to anon, authenticated
  using (public.run_visible(run_id));

-- experiments: owner write; read per visibility
grant select on public.experiments to anon, authenticated;
grant insert (owner_id, title, variable, run_ids, visibility) on public.experiments to authenticated;
grant update (title, run_ids, visibility) on public.experiments to authenticated;
create policy experiments_read on public.experiments for select to anon, authenticated
  using (owner_id = auth.uid() or public.is_admin() or visibility <> 'private');
create policy experiments_insert on public.experiments for insert to authenticated
  with check (owner_id = auth.uid());
create policy experiments_update on public.experiments for update to authenticated
  using (owner_id = auth.uid()) with check (owner_id = auth.uid());

-- audit_log: admin read only; service inserts
grant select on public.audit_log to authenticated;
create policy audit_log_read on public.audit_log for select to authenticated using (public.is_admin());

-- reports: reporter reads own, admin all; user files a report for a run they can see
grant select on public.reports to authenticated;
grant insert (reporter_id, run_id, reason) on public.reports to authenticated;
create policy reports_read on public.reports for select to authenticated
  using (reporter_id = auth.uid() or public.is_admin());
create policy reports_insert on public.reports for insert to authenticated
  with check (reporter_id = auth.uid() and public.run_visible(run_id));

-- service_role bypasses RLS (BYPASSRLS) but still needs table privileges
grant all on all tables in schema public to service_role;
grant usage, select on all sequences in schema public to service_role;
