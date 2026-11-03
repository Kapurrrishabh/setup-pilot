-- The repo chatbot's search index. Vectors come from all-MiniLM-L6-v2, which gives 384 numbers;
-- a different embedding model needs a new migration with its own size.
create extension if not exists vector;

create table if not exists chat_repos (
    key text primary key,
    url text not null,
    chunk_count integer not null check (chunk_count >= 0),
    indexed_at timestamptz not null default now()
);

create table if not exists chat_chunks (
    id bigint generated always as identity primary key,
    repo_key text not null references chat_repos (key) on delete cascade,
    path text not null,
    section text not null,
    text text not null,
    embedding vector(384) not null
);
create index if not exists chat_chunks_repo_key_idx on chat_chunks (repo_key);

alter table chat_repos enable row level security;
alter table chat_chunks enable row level security;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on chat_repos, chat_chunks from anon, authenticated;
    end if;
end $$;
