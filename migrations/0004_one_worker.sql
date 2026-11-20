-- One worker is the rule (issue #187): no leases, so no lease columns.
-- 0002 re-adds lease_token on every run of migrate.py; this drops it again, which is harmless.
alter table jobs drop column if exists lease_token;
alter table jobs drop column if exists leased_until;
drop index if exists jobs_state_lease_idx;
create index if not exists jobs_state_created_idx on jobs (state, created_at);
