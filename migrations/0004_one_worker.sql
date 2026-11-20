-- One worker is the rule (#205): no leases, so no lease columns.
-- 0001 no longer creates them and 0002 (which added lease_token) is deleted; this cleans up
-- databases created before that. Safe to re-run.
alter table jobs drop column if exists lease_token;
alter table jobs drop column if exists leased_until;
drop index if exists jobs_state_lease_idx;
create index if not exists jobs_state_created_idx on jobs (state, created_at);
