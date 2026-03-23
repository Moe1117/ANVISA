-- ============================================================
-- ANVISA Monitor — Supabase Schema
-- Run this in your Supabase SQL editor (Brazil project)
-- ============================================================

-- Publications: raw scraped and classified ANVISA/DOU items
create table if not exists anvisa_publications (
  id                  uuid primary key default gen_random_uuid(),
  title               text not null,
  url                 text unique,
  source              text,                        -- 'anvisa_news' | 'dou' | 'anvisa_suplementos_page'
  pub_date            timestamptz,
  publication_number  text,                        -- e.g. 'RDC 243' or 'IN 28'
  publication_type    text,                        -- 'RDC' | 'IN' | 'DOU_notice' | etc.
  change_type         text,                        -- 'addition' | 'removal' | 'dose_modification' | 'none'
  summary_pt          text,
  summary_en          text,
  urgency             text default 'low',          -- 'high' | 'medium' | 'low'
  amends_document     text,                        -- e.g. 'IN 28/2018'
  effective_date      date,
  raw_text            text,
  is_relevant         boolean default false,
  processed_at        timestamptz default now()
);

-- Ingredient changes: one row per ingredient per publication
create table if not exists anvisa_ingredient_changes (
  id                  uuid primary key default gen_random_uuid(),
  publication_id      uuid references anvisa_publications(id) on delete set null,
  publication_number  text,
  pub_date            timestamptz,
  ingredient_name_pt  text not null,
  ingredient_name_en  text,
  change_type         text not null,               -- 'added' | 'removed' | 'modified'
  category            text,                        -- 'vitamins' | 'minerals' | 'plant_extracts' | etc.
  max_dose            text,
  dose_unit           text,
  change_detail       text,
  source_url          text,
  processed_at        timestamptz default now()
);

-- Scrape run log: one row per GitHub Actions execution
create table if not exists anvisa_scrape_runs (
  id                          uuid primary key default gen_random_uuid(),
  run_id                      text unique not null,
  run_at                      timestamptz default now(),
  status                      text,                -- 'success' | 'partial_failure' | 'failure'
  sources_scraped             text[],
  total_publications_found    int default 0,
  relevant_count              int default 0,
  ingredient_changes_written  int default 0,
  error_message               text,
  dry_run                     boolean default false
);

-- Indexes
create index if not exists idx_publications_pub_date    on anvisa_publications(pub_date desc);
create index if not exists idx_publications_urgency     on anvisa_publications(urgency);
create index if not exists idx_publications_change_type on anvisa_publications(change_type);
create index if not exists idx_ingredient_changes_name  on anvisa_ingredient_changes(ingredient_name_pt);
create index if not exists idx_ingredient_changes_type  on anvisa_ingredient_changes(change_type);
create index if not exists idx_scrape_runs_run_at       on anvisa_scrape_runs(run_at desc);

-- RLS: service key bypasses this; anon users can only read
alter table anvisa_publications       enable row level security;
alter table anvisa_ingredient_changes enable row level security;
alter table anvisa_scrape_runs        enable row level security;

create policy "Public read publications"
  on anvisa_publications for select using (true);

create policy "Public read ingredient changes"
  on anvisa_ingredient_changes for select using (true);

create policy "No public read scrape runs"
  on anvisa_scrape_runs for select using (false);
