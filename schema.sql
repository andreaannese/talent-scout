-- Talent Scout schema. Safe to run many times (the weekly job runs it before each scrape).
create table if not exists people (
  key text primary key,
  name text not null,
  birth_year int,
  age int,
  age_band text,                 -- minor | 18-20 | adult | unknown
  achievement text,
  age_at_achievement int,
  category text,                 -- tech | science | art | business | sport | other
  score int,
  country text,
  website text,
  contact_email text,
  contact_type text,
  source text,
  source_url text,
  found_at timestamptz default now(),
  -- managed by you in the UI, never overwritten by the scraper:
  status text not null default 'new',   -- new | shortlisted | contacted | rejected
  notes text,
  attio_record_id text,
  updated_at timestamptz default now()
);
create table if not exists seen (            -- memory: everything ever screened
  key text primary key,
  seen_at timestamptz default now()
);
create index if not exists people_score_idx on people (score desc);
create index if not exists people_status_idx on people (status);

-- Lock tables: only the server key (GitHub Actions) and signed-in UI users can access.
alter table people enable row level security;
alter table seen enable row level security;
do $$ begin
  if not exists (select 1 from pg_policies where tablename='people' and policyname='signed_in_full_access') then
    create policy signed_in_full_access on people for all to authenticated using (true) with check (true);
  end if;
end $$;

-- Make sure the API can use the tables, then refresh its cache of tables.
grant usage on schema public to service_role, authenticated;
grant all on table people, seen to service_role;
grant select, insert, update, delete on table people to authenticated;
notify pgrst, 'reload schema';

-- v2: remember which model screened each item ('keyword' items get rescreened by AI later)
alter table seen add column if not exists scored_by text;
notify pgrst, 'reload schema';

-- v3: login allowlist. Only emails in allowed_emails can see or edit data.
create table if not exists allowed_emails (
  email text primary key,
  added_at timestamptz default now()
);
alter table allowed_emails enable row level security;
-- first authorised emails are added once by hand (Access tab or SQL editor), not stored in this file

create or replace function public.is_allowed() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from allowed_emails
                 where lower(email) = lower(coalesce(auth.jwt() ->> 'email', '')));
$$;
revoke execute on function public.is_allowed() from public, anon;
grant execute on function public.is_allowed() to authenticated;

drop policy if exists signed_in_full_access on people;
drop policy if exists allowed_full_access on people;
create policy allowed_full_access on people for all to authenticated
  using (public.is_allowed()) with check (public.is_allowed());

drop policy if exists allowed_manage on allowed_emails;
create policy allowed_manage on allowed_emails for all to authenticated
  using (public.is_allowed()) with check (public.is_allowed());
grant select, insert, delete on table allowed_emails to authenticated;
grant all on table allowed_emails to service_role;

-- keep updated_at fresh when someone edits status/notes in the UI
create or replace function public.touch_updated_at() returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end $$;
drop trigger if exists people_touch on people;
create trigger people_touch before update on people for each row execute function public.touch_updated_at();
notify pgrst, 'reload schema';

-- v4: one row per scraper run, shown in the interface's Progress tab
create table if not exists runs (
  id bigint generated always as identity primary key,
  started_at timestamptz, finished_at timestamptz,
  discovered int, scored int, kept int, backlog int,
  models text, stop_reason text
);
alter table runs enable row level security;
grant all on table runs to service_role;
grant select on table runs to authenticated;
drop policy if exists allowed_read on runs;
create policy allowed_read on runs for select to authenticated using (public.is_allowed());
notify pgrst, 'reload schema';
