-- DELETE /runs/{id}: owners soft-delete through this function; they have no UPDATE grant on deleted_at.
-- Artifact purge (after 7 days) is a service job.
create function public.soft_delete_run(p_run_id uuid) returns boolean
  language plpgsql security definer set search_path = ''
  as $$
  declare n integer;
  begin
    update public.runs set deleted_at = coalesce(deleted_at, now())
      where id = p_run_id and owner_id = auth.uid();
    get diagnostics n = row_count;
    return n > 0;
  end $$;
revoke all on function public.soft_delete_run(uuid) from public, anon;
grant execute on function public.soft_delete_run(uuid) to authenticated;
