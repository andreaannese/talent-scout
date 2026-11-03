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
