-- Experiments: a set of the owner's runs that differ in one declared variable.
-- run_ids is an array (no foreign key), so ownership is enforced by a trigger that applies to every writer,
-- the service role included. Unlisted means link-only here too: the table lists public experiments to others, and
-- api_private.get_visible_experiment serves one by id.

alter table public.experiments add column created_at timestamptz not null default now();
alter table public.experiments add constraint experiments_variable_len check (char_length(variable) between 1 and 64);
alter table public.experiments add constraint experiments_run_ids_max check (cardinality(run_ids) <= 50);

create function public.experiments_guard() returns trigger
  language plpgsql security definer set search_path = ''
  as $$
  declare added uuid[];
  begin
    if tg_op = 'UPDATE' and new.owner_id <> old.owner_id then
      raise exception 'experiment_owner_immutable' using errcode = 'P0001';
    end if;
    if (select count(distinct x) from unnest(new.run_ids) x) <> cardinality(new.run_ids) then
      raise exception 'experiment_duplicate_runs' using errcode = 'P0001';
    end if;
    -- only runs being added are checked: a run the owner deleted later may stay in an old experiment
    added := array(select unnest(new.run_ids) except select unnest(case when tg_op = 'UPDATE' then old.run_ids else '{}'::uuid[] end));
    if exists (select 1 from unnest(added) a
               where not exists (select 1 from public.runs r
                                 where r.id = a and r.owner_id = new.owner_id and r.deleted_at is null)) then
      raise exception 'experiment_run_not_owned' using errcode = 'P0001';
    end if;
    return new;
  end $$;
create trigger experiments_guard before insert or update on public.experiments
  for each row execute function public.experiments_guard();

drop policy experiments_read on public.experiments;
create policy experiments_read on public.experiments for select to anon, authenticated
  using (owner_id = auth.uid() or public.is_admin() or visibility = 'public');

create function api_private.get_visible_experiment(p_id uuid) returns setof public.experiments
  language sql stable security definer set search_path = ''
  as $$
    select e.* from public.experiments e
    where e.id = p_id and (e.owner_id = auth.uid() or public.is_admin() or e.visibility in ('unlisted', 'public'))
  $$;

create function api_private.create_experiment(p_title text, p_variable text, p_run_ids uuid[], p_visibility text)
  returns uuid
  language plpgsql security definer set search_path = ''
  as $$
  declare
    uid uuid := api_private.require_live_account();
    eid uuid;
  begin
    perform pg_advisory_xact_lock(hashtextextended('experiment:' || uid::text, 0));
    if (select count(*) from public.experiments where owner_id = uid) >= 100 then
      raise exception 'too_many_experiments' using errcode = 'P0001';
    end if;
    insert into public.experiments (owner_id, title, variable, run_ids, visibility)
      values (uid, p_title, p_variable, p_run_ids, p_visibility) returning id into eid;
    return eid;
  end $$;

-- Owner-only. Null arguments leave a field alone. Missing and not-yours are the same answer.
create function api_private.update_experiment(p_id uuid, p_title text, p_visibility text, p_add uuid[], p_remove uuid[])
  returns void
  language plpgsql security definer set search_path = ''
  as $$
  declare uid uuid := api_private.require_live_account();
  begin
    perform 1 from public.experiments where id = p_id and owner_id = uid for update;
    if not found then raise exception 'experiment_not_found' using errcode = 'P0001'; end if;
    update public.experiments set
      title = coalesce(p_title, title),
      visibility = coalesce(p_visibility, visibility),
      run_ids = array(select x from unnest(run_ids) with ordinality as t(x, i)
                      where x <> all (coalesce(p_remove, '{}'::uuid[])) order by i)
                || array(select a from unnest(coalesce(p_add, '{}'::uuid[])) a where a <> all (run_ids))
    where id = p_id and owner_id = uid;
  end $$;

-- The runs of an experiment the caller may see, in the experiment's order. A run is shown to a viewer only if it is
-- listable for them (owner, admin, or public): a private run inside a public experiment does not leak, and an
-- unlisted run is not published by being put in one.
create function api_private.experiment_runs(p_id uuid)
  returns table (run_id uuid, pos bigint, status text, title text, model_id text, provider text, steps integer,
                 tokens bigint, ended_at timestamptz, progress_score integer, verified boolean)
  language sql stable security definer set search_path = ''
  as $$
    select r.id, t.i, r.status, r.title, r.model_id, r.provider, r.steps, r.tokens, r.ended_at, s.progress_score,
           coalesce(s.verified, false)
    from public.experiments e
    cross join lateral unnest(e.run_ids) with ordinality as t(rid, i)
    join public.runs r on r.id = t.rid
    left join public.run_scores s on s.run_id = r.id
    where e.id = p_id
      and (e.owner_id = auth.uid() or public.is_admin() or e.visibility in ('unlisted', 'public'))
      and public.run_listable(r.id)
      and r.deleted_at is null
    order by t.i
  $$;

revoke all on function api_private.get_visible_experiment(uuid) from public;
revoke all on function api_private.create_experiment(text, text, uuid[], text) from public, anon;
revoke all on function api_private.update_experiment(uuid, text, text, uuid[], uuid[]) from public, anon;
revoke all on function api_private.experiment_runs(uuid) from public;
grant execute on function api_private.get_visible_experiment(uuid) to anon, authenticated;
grant execute on function api_private.experiment_runs(uuid) to anon, authenticated;
grant execute on function api_private.create_experiment(text, text, uuid[], text) to authenticated;
grant execute on function api_private.update_experiment(uuid, text, text, uuid[], uuid[]) to authenticated;
