-- GET /runs/{id}/config is readable by anyone who can see the run (public and unlisted runs are readable anonymously).
grant execute on function api_private.run_config(uuid) to anon;
