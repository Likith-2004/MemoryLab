-- Distinct checkpoint steps a run has uploaded blobs for (counted at upload time, so uploads never followed by an event count too).
alter table public.run_ingest_state add column checkpoints integer not null default 0;
