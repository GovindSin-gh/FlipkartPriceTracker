-- Flipkart Price Tracker: run this once in Supabase > SQL Editor

create extension if not exists "pgcrypto";

create table if not exists public.price_trackers (
  id            uuid primary key default gen_random_uuid(),
  product_url   text not null,
  product_title text,
  target_price  numeric not null check (target_price > 0),
  latest_price  numeric,
  initial_price numeric,
  status        varchar(20) not null default 'ACTIVE'
                check (status in ('ACTIVE', 'ALERT_SENT', 'RESOLVED')),
  created_at    timestamp default now(),
  updated_at    timestamp default now()
);

create index if not exists idx_price_trackers_status on public.price_trackers (status);

-- Keep updated_at fresh on every update
create or replace function public.set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_price_trackers_updated on public.price_trackers;
create trigger trg_price_trackers_updated
before update on public.price_trackers
for each row execute function public.set_updated_at();

-- Row Level Security
-- The dashboard uses the public ANON key, so we allow the anon role to read,
-- insert and update. The backend (GitHub Actions) should use the SERVICE_ROLE
-- key, which bypasses RLS entirely.
alter table public.price_trackers enable row level security;

drop policy if exists "anon read"   on public.price_trackers;
drop policy if exists "anon insert" on public.price_trackers;
drop policy if exists "anon update" on public.price_trackers;

create policy "anon read"   on public.price_trackers for select to anon using (true);
create policy "anon insert" on public.price_trackers for insert to anon
  with check (status = 'ACTIVE');
create policy "anon update" on public.price_trackers for update to anon
  using (true) with check (status in ('ACTIVE', 'ALERT_SENT', 'RESOLVED'));

-- No delete policy on purpose: the dashboard cannot delete rows.
