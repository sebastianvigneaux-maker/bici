create table if not exists public.training_sessions (
  id uuid primary key,
  user_id uuid not null references auth.users (id) on delete cascade,
  started_at timestamptz not null,
  updated_at timestamptz not null default now(),
  payload jsonb not null check (jsonb_typeof(payload) = 'object')
);

create index if not exists training_sessions_user_started_idx
  on public.training_sessions (user_id, started_at desc);

alter table public.training_sessions enable row level security;

revoke all on table public.training_sessions from anon, authenticated;
grant select, insert, update, delete on table public.training_sessions to authenticated;

drop policy if exists "Users can read their own training sessions" on public.training_sessions;
create policy "Users can read their own training sessions"
  on public.training_sessions for select to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists "Users can create their own training sessions" on public.training_sessions;
create policy "Users can create their own training sessions"
  on public.training_sessions for insert to authenticated
  with check ((select auth.uid()) = user_id);

drop policy if exists "Users can update their own training sessions" on public.training_sessions;
create policy "Users can update their own training sessions"
  on public.training_sessions for update to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

drop policy if exists "Users can delete their own training sessions" on public.training_sessions;
create policy "Users can delete their own training sessions"
  on public.training_sessions for delete to authenticated
  using ((select auth.uid()) = user_id);;
