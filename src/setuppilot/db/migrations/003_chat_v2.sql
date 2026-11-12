-- The v2 chat index: whole code files with line ranges, word search next to the vectors, the
-- repo card, the commit that was read, and who may see a private repo.
-- The old index has no line numbers and only the first lines of code, so it is emptied here.
-- Index each repo again (setuppilot index <url>, or the Chat page).
-- To go back to the v1 code, run this, then index each repo again:
--   alter table chat_chunks drop column fts, drop column words, drop column kind,
--     drop column start_line, drop column end_line;
--   alter table chat_repos drop column is_private, drop column owner_github_id,
--     drop column card, drop column embedding_model, drop column commit_sha;
--   delete from schema_migrations where version = '003_chat_v2.sql';
delete from chat_repos;

alter table chat_repos
    add column commit_sha text not null,
    add column embedding_model text not null,
    add column card jsonb not null,
    -- Set only for a private repo: the GitHub user who may see it.
    add column owner_github_id bigint;
-- Generated, so it can never disagree with owner_github_id.
alter table chat_repos
    add column is_private boolean generated always as (owner_github_id is not null) stored;

alter table chat_chunks
    add column start_line integer not null,
    add column end_line integer not null,
    add column kind text not null,
    -- Written by setuppilot.chat.search.tokens, which splits camelCase and snake_case.
    add column words text not null,
    -- Lines start at 1; 0 and 0 mean text that is not lines of one file (the file tree).
    add constraint chat_chunks_lines check (
        (start_line = 0 and end_line = 0) or (start_line >= 1 and end_line >= start_line)
    );
alter table chat_chunks
    add column fts tsvector generated always as (to_tsvector('simple', words)) stored;
create index chat_chunks_fts_idx on chat_chunks using gin (fts);
