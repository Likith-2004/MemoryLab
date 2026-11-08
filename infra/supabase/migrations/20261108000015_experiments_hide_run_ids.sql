-- run_ids can hold the owner's private and unlisted run ids; a public experiment's row was listable through the Data API
-- with the raw array. Column privileges: everyone can read the descriptive columns, nobody can read run_ids directly.
-- The runs of an experiment come from api_private.experiment_runs (filtered per viewer); the owner's own list and run
-- set from the readers below.

revoke select on public.experiments from anon, authenticated;
grant select (id, owner_id, title, variable, visibility, created_at) on public.experiments to anon, authenticated;

create function api_private.my_experiments(p_limit integer)
  returns table (id uuid, title text, variable text, visibility text, n_runs integer, created_at timestamptz)
  language sql stable security definer set search_path = ''
  as $$
    select e.id, e.title, e.variable, e.visibility, cardinality(e.run_ids), e.created_at
    from public.experiments e where e.owner_id = auth.uid()
    order by e.created_at desc, e.id desc limit least(greatest(p_limit, 1), 100)
  $$;

create function api_private.my_experiment_run_ids(p_id uuid) returns uuid[]
  language sql stable security definer set search_path = ''
  as $$ select e.run_ids from public.experiments e where e.id = p_id and e.owner_id = auth.uid() $$;

revoke all on function api_private.my_experiments(integer) from public, anon;
revoke all on function api_private.my_experiment_run_ids(uuid) from public, anon;
grant execute on function api_private.my_experiments(integer) to authenticated;
grant execute on function api_private.my_experiment_run_ids(uuid) to authenticated;
