-- The dream job's memory changes are counted apart from the agent's own: a dreaming arm must not look like an agent that writes more.
alter table public.run_scores add column dream_ops integer not null default 0;
