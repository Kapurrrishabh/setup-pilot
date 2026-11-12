-- Who may see each run. A run that reads its repo through the GitHub sign-in belongs to the
-- GitHub user who started it, and only they see its repo name, folder and steps. Null for a
-- public run, which anyone may see; runs saved before this were all public.
-- To go back to the v1 code, run:
--   alter table runs drop column owner_github_id;
--   delete from schema_migrations where version = '004_run_access.sql';
alter table runs add column owner_github_id bigint;
