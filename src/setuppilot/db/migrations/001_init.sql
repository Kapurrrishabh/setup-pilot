-- SetupPilot tables. Only the Flask server uses the database, as the table owner.
-- Row level security is on with no policies, so Supabase's public API keys can read nothing.

create table if not exists runs (
    id uuid primary key default gen_random_uuid(),
    repo_url text not null,
    dest_dir text not null,
    status text not null check (
        status in ('running', 'waiting_approval', 'paused', 'succeeded', 'failed', 'cancelled')
    ),
    error_type text,
    message text,
    created_at timestamptz not null default now(),
    finished_at timestamptz
);
create index if not exists runs_created_at_idx on runs (created_at desc);

create table if not exists steps (
    id bigint generated always as identity primary key,
    run_id uuid not null references runs (id) on delete cascade,
    name text not null,
    tool text,
    arguments jsonb not null,
    result_summary text not null,
    is_ok boolean not null,
    error_type text,
    exit_code integer,
    duration_ms integer not null check (duration_ms >= 0),
    -- Per-step numbers for the timeline. Run and day totals come from token_usage.
    tokens integer not null check (tokens >= 0),
    model text,
    created_at timestamptz not null default now()
);
create index if not exists steps_run_id_idx on steps (run_id, id);

create table if not exists token_usage (
    id bigint generated always as identity primary key,
    run_id uuid references runs (id) on delete set null,
    model text not null,
    input_tokens integer not null check (input_tokens >= 0),
    output_tokens integer not null check (output_tokens >= 0),
    is_cached boolean not null,
    created_at timestamptz not null
);
create index if not exists token_usage_created_at_idx on token_usage (created_at);
create index if not exists token_usage_run_id_idx on token_usage (run_id);

create table if not exists llm_cache (
    key text primary key,
    text text not null,
    input_tokens integer not null,
    output_tokens integer not null,
    created_at timestamptz not null default now()
);

create table if not exists metrics (
    id bigint generated always as identity primary key,
    run_id uuid references runs (id) on delete cascade,
    name text not null,
    value double precision not null,
    created_at timestamptz not null default now()
);
create index if not exists metrics_name_created_at_idx on metrics (name, created_at);

alter table runs enable row level security;
alter table steps enable row level security;
alter table token_usage enable row level security;
alter table llm_cache enable row level security;
alter table metrics enable row level security;

-- The anon and authenticated roles exist only on Supabase, not on a plain test Postgres.
do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on runs, steps, token_usage, llm_cache, metrics, schema_migrations
            from anon, authenticated;
        -- Tables made later (LangGraph checkpoints, the chatbot index) start private too.
        alter default privileges in schema public revoke all on tables from anon, authenticated;
    end if;
end $$;
