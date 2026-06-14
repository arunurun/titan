-- Titan free-NSE EOD feed tables (additive, idempotent). Safe to run multiple times.
-- Apply with: python scripts/apply_eod_feeds_migration.py  (needs SUPABASE_ACCESS_TOKEN)
-- Every table uses a natural-key primary key so ingestion can upsert idempotently.

-- 1) Delivery % / deliverable quantity (from sec_bhavdata_full).
create table if not exists public.delivery_daily (
    trade_date    date         not null,
    symbol        text         not null,
    series        text         not null default 'EQ',
    close_price   double precision,
    ttl_traded_qty bigint,
    deliv_qty     bigint,
    deliv_per     double precision,
    turnover_lacs double precision,
    source        text         not null default 'nse_sec_bhavdata_full',
    ingested_at   timestamptz  not null default now(),
    primary key (trade_date, symbol, series)
);
create index if not exists idx_delivery_daily_symbol on public.delivery_daily (symbol, trade_date desc);

-- 2) F&O ban list (one row per banned symbol per trade date).
create table if not exists public.fno_ban_daily (
    trade_date  date        not null,
    symbol      text        not null,
    source      text        not null default 'nse_fo_secban',
    ingested_at timestamptz not null default now(),
    primary key (trade_date, symbol)
);

-- 3) Futures OI / basis (front-month aggregate per underlying, from FO UDiFF bhavcopy).
create table if not exists public.futures_daily (
    trade_date     date        not null,
    symbol         text        not null,
    expiry_date    date        not null,
    close_price    double precision,
    settle_price   double precision,
    open_interest  bigint,
    change_in_oi   bigint,
    contracts_traded bigint,
    underlying_close double precision,
    basis          double precision,
    source         text        not null default 'nse_fo_udiff',
    ingested_at    timestamptz not null default now(),
    primary key (trade_date, symbol, expiry_date)
);
create index if not exists idx_futures_daily_symbol on public.futures_daily (symbol, trade_date desc);

-- 4) Institutional flow (FII/DII cash provisional — market-level aggregate, per roadmap).
create table if not exists public.institutional_flow (
    as_of_date  date        not null,
    segment     text        not null default 'cash',  -- cash | fii_derivatives
    fii_buy_crs double precision,
    fii_sell_crs double precision,
    fii_net_crs double precision,
    dii_buy_crs double precision,
    dii_sell_crs double precision,
    dii_net_crs double precision,
    source      text        not null default 'nse_fii_dii',
    ingested_at timestamptz not null default now(),
    primary key (as_of_date, segment)
);

-- 5) India VIX daily history (from ind_close_all index file).
create table if not exists public.india_vix_daily (
    trade_date  date        not null,
    open        double precision,
    high        double precision,
    low         double precision,
    close       double precision,
    prev_close  double precision,
    change_pct  double precision,
    source      text        not null default 'nse_ind_close_all',
    ingested_at timestamptz not null default now(),
    primary key (trade_date)
);

-- 6) Corporate actions / results calendar.
create table if not exists public.corporate_actions_calendar (
    symbol      text        not null,
    ex_date     date        not null,
    purpose     text        not null,        -- dividend / bonus / split / results / agm ...
    series      text,
    record_date date,
    bc_start_date date,
    bc_end_date date,
    details     text,
    source      text        not null default 'nse_corp_actions',
    ingested_at timestamptz not null default now(),
    primary key (symbol, ex_date, purpose)
);

-- Lightweight audit log for ingestion runs (so a failed feed never crashes the batch).
create table if not exists public.eod_feed_ingest_log (
    feed        text        not null,
    trade_date  date        not null,
    status      text        not null,        -- ok | empty | error
    row_count   integer     not null default 0,
    detail      text,
    ingested_at timestamptz not null default now(),
    primary key (feed, trade_date, ingested_at)
);
