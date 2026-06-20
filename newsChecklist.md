# TITAN V12.0 News Integration — Master Checklist

**Status:** V12 news implementation committed on `news` branch (checklist synced post-commit)

**Sources:** `news.md` (full spec), repo gap analysis (partial news in `sector_priority.py` / `sector_audit.py`; missing all `src/news_*.py`; schema conflict on `global_news_snapshots`)

**Agreed defaults baked in:**

- [x] Keep macro `global_news_snapshots` (existing `sector_priority` schema); add `news_feed`, `news_sentiment_cache`, `symbol_news_snapshots` (per-symbol cache — news.md's per-symbol snapshot schema lives here, not in macro table)
- [x] VADER default; FinBERT opt-in; CI VADER-only
- [x] Full integration: audit + store + CI; **defer** `sector_priority` refactor (Integration 3.6 / 3.7)
- [x] `requirements-news.txt` or optional extras for `torch` / `transformers`
- [x] Phase 0: check local `news` branch before greenfield
- [x] Do **not** commit `stocks-484613-*.json`; harden `.gitignore`

---

## Phase 0 — Design Decisions & Pre-Flight

- [x] **Confirm plan with user before any implementation**
  - [x] Present this checklist; obtain explicit approval to proceed phase-by-phase
  - [x] Mark tasks in progress / complete one at a time per project workflow

- [x] **Confirmation point 1 — Schema strategy (macro vs per-symbol)**
  - [x] Keep existing macro `global_news_snapshots` table shape used by `sector_priority.py`
    - [x] Columns: `refreshed_at`, `item_count`, `fetch_status`, `refresh_error`, `news_items`, `sector_scores` (insert-only macro snapshots)
    - [x] Env: `TITAN_NEWS_SNAPSHOT_TABLE=global_news_snapshots` (default)
  - [x] Add new per-symbol table `symbol_news_snapshots` using news.md aggregate snapshot schema (not the macro table)
    - [x] Do **not** overwrite macro table with news.md `global_news_snapshots` per-symbol schema
  - [x] Add `news_feed` + `news_sentiment_cache` as specified
  - [x] Document schema conflict resolution in migration comments / docs

- [x] **Confirmation point 2 — Sentiment model strategy**
  - [x] Default runtime model: VADER (`TITAN_SENTIMENT_MODEL=vader`)
  - [x] FinBERT / `compute_sentiment_transformers` opt-in only (local / prod with GPU optional)
  - [x] GitHub Actions `news_fetch.yml`: VADER-only (no `torch` / `transformers` install)
  - [x] `requirements-news.txt` or `[finbert]` optional extras for heavy deps

- [ ] **Confirmation point 3 — Integration scope**
  - [x] **In scope:** `news_client`, `news_sentiment`, `news_store`, `news_audit`, `sector_audit` enrichment, `brain.py` prompt, `analysis_store` fields, SQL migrations, scripts, CI workflow, tests
  - [ ] **Deferred (3.6 / 3.7):** `sector_priority.py` news blending refactor
    - [ ] Do not implement `TITAN_NEWS_BLEND_WEIGHT` / `TITAN_NEWS_BLEND_CAP` composite scoring changes yet
    - [ ] Do not add `intent_score_news_blended` to audit yet
    - [ ] Preserve existing `_apply_global_news_correlation` / macro snapshot path until explicit follow-up

- [x] **Confirmation point 4 — Reuse vs greenfield**
  - [x] Check local `news` branch before creating modules from scratch
    - [x] `git branch -a` — confirm `news` exists
    - [x] `git diff main..news --stat` — note branch is behind main and removes news code (not a drop-in module source)
    - [x] `git ls-tree -r news --name-only | grep news` — inventory any reusable artifacts (e.g. `tests/test_proxy_news_endpoints.mjs`)
  - [x] Decision: cherry-pick / adapt vs greenfield `src/news_*.py`

- [ ] **Security & repo hygiene**
  - [x] Do **not** commit `stocks-484613-faf268c56fd3.json` (untracked GCP service account key)
  - [x] Harden `.gitignore`
    - [x] Add pattern `stocks-484613-*.json` (or `*-*.json` service-account keys if broader policy agreed)
    - [x] Add `*.json` credential patterns if not already covered
    - [x] Verify no secrets in staged files before any commit

- [x] **Architecture alignment review**
  - [x] External sources: NewsAPI, Finnhub, RSS (Bloomberg, Reuters, CNBC-TV18, Moneycontrol, ET Markets), optional web scraping path
  - [x] Pipeline: fetch → normalize → dedupe → sentiment → store → audit correlation → Gemini narrative
  - [x] Non-blocking news failures must not halt sector analysis runs

---

## Phase 1 — Database Schema (`sql/create_news_tables.sql`)

- [x] **Create migration file**
  - [x] Path: `sql/create_news_tables.sql`
  - [x] Deployment command documented: `psql -h <SUPABASE_HOST> -U postgres -d postgres -f sql/create_news_tables.sql`
  - [x] Alternative: Supabase SQL Editor one-shot run

- [x] **Table: `news_feed`**
  - [x] `CREATE TABLE news_feed`
  - [x] Column: `id BIGSERIAL PRIMARY KEY`
  - [x] Column: `symbol VARCHAR(20) NOT NULL`
  - [x] Column: `exchange VARCHAR(10) NOT NULL DEFAULT 'NSE'`
  - [x] Column: `title TEXT NOT NULL`
  - [x] Column: `url TEXT NOT NULL UNIQUE`
  - [x] Column: `source VARCHAR(50) NOT NULL`
  - [x] Column: `published_at TIMESTAMP WITH TIME ZONE NOT NULL`
  - [x] Column: `fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()`
  - [x] Column: `sentiment VARCHAR(20) NOT NULL DEFAULT 'neutral'`
  - [x] Column: `sentiment_score FLOAT DEFAULT 0.0`
  - [x] Column: `sentiment_model VARCHAR(50) DEFAULT 'vader'`
  - [x] Column: `relevance_score FLOAT DEFAULT 0.5`
  - [x] Column: `is_duplicate BOOLEAN DEFAULT FALSE`
  - [x] Column: `duplicate_of_id BIGINT REFERENCES news_feed(id)`
  - [x] Column: `summary TEXT`
  - [x] Column: `event_type VARCHAR(50)`
  - [x] Column: `impact_level VARCHAR(20) DEFAULT 'medium'`
  - [x] Column: `created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()`
  - [x] Column: `updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()`
  - [x] Index: `idx_news_symbol_published ON news_feed(symbol, published_at DESC)`
  - [x] Index: `idx_news_source ON news_feed(source)`
  - [x] Index: `idx_news_fetched ON news_feed(fetched_at DESC)`
  - [x] Index: `idx_news_url ON news_feed(url)`
  - [x] Index: `idx_news_exchange ON news_feed(exchange)`
  - [x] Constraint: `url` UNIQUE (deduplication key)
  - [x] Constraint: `duplicate_of_id` FK → `news_feed(id)`
  - [x] Document allowed values
    - [x] `exchange`: `NSE`, `BSE`
    - [x] `sentiment`: `positive`, `negative`, `neutral`, `mixed`
    - [x] `sentiment_model`: `vader`, `finbert`
    - [x] `impact_level`: `high`, `medium`, `low`
    - [x] `event_type`: `earnings`, `acquisition`, `regulatory`, `dividend`, `general`
    - [x] `source` examples: `newsapi`, `finnhub`, `rss:moneycontrol`, etc.

- [x] **Table: `news_sentiment_cache`**
  - [x] `CREATE TABLE news_sentiment_cache`
  - [x] Column: `id BIGSERIAL PRIMARY KEY`
  - [x] Column: `news_id BIGINT UNIQUE NOT NULL REFERENCES news_feed(id) ON DELETE CASCADE`
  - [x] Column: `title_hash VARCHAR(64) NOT NULL`
  - [x] Column: `content_hash VARCHAR(64)`
  - [x] Column: `sentiment VARCHAR(20)`
  - [x] Column: `sentiment_score FLOAT`
  - [x] Column: `confidence FLOAT`
  - [x] Column: `model_used VARCHAR(50)`
  - [x] Column: `computation_time_ms FLOAT`
  - [x] Column: `created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()`
  - [x] Index: `idx_sentiment_cache_hash ON news_sentiment_cache(title_hash)`
  - [x] Index: `idx_sentiment_cache_news ON news_sentiment_cache(news_id)`
  - [x] Constraint: `news_id` UNIQUE
  - [x] Constraint: `news_id` FK ON DELETE CASCADE

- [x] **Table: `symbol_news_snapshots`** (per-symbol; news.md schema, renamed from spec's per-symbol `global_news_snapshots`)
  - [x] `CREATE TABLE symbol_news_snapshots`
  - [x] Column: `id BIGSERIAL PRIMARY KEY`
  - [x] Column: `snapshot_at TIMESTAMP WITH TIME ZONE NOT NULL`
  - [x] Column: `symbol VARCHAR(20) NOT NULL`
  - [x] Column: `news_count INT DEFAULT 0`
  - [x] Column: `recent_news_items JSONB`
  - [x] Column: `aggregate_sentiment VARCHAR(20)`
  - [x] Column: `aggregate_score FLOAT`
  - [x] Column: `sentiment_trend FLOAT` (spec column type; implementation may map trend label separately in JSON)
  - [x] Column: `top_drivers JSONB`
  - [x] Column: `event_alerts JSONB`
  - [x] Column: `ttl_seconds INT DEFAULT 7200`
  - [x] Column: `created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()`
  - [x] Index: `idx_symbol_snapshots_symbol_time ON symbol_news_snapshots(symbol, snapshot_at DESC)`
  - [x] Example payload shape documented
    - [x] `symbol`, `snapshot_at`, `news_count`, `aggregate_sentiment`, `aggregate_score`, `sentiment_trend`
    - [x] `recent_news_items[]`: `title`, `source`, `published_at`, `sentiment`, `sentiment_score`
    - [x] `top_drivers[]`: `headline`, `impact_contribution`

- [x] **Preserve macro table: `global_news_snapshots`**
  - [x] Do **not** migrate macro table to per-symbol schema
  - [x] Verify existing inserts from `sector_priority.refresh_global_news_snapshot` still work
  - [x] If macro table missing in Supabase, create **macro** schema separately (not news.md per-symbol DDL)

- [x] **Extend existing table: `symbol_daily_features`**
  - [x] `ALTER TABLE symbol_daily_features ADD COLUMN IF NOT EXISTS news_correlation JSONB`
  - [x] `ALTER TABLE symbol_daily_features ADD COLUMN IF NOT EXISTS news_sentiment_aggregate VARCHAR(20)`
  - [x] `ALTER TABLE symbol_daily_features ADD COLUMN IF NOT EXISTS news_sentiment_score FLOAT`
  - [x] `ALTER TABLE symbol_daily_features ADD COLUMN IF NOT EXISTS news_sentiment_trend VARCHAR(20)`
  - [x] `ALTER TABLE symbol_daily_features ADD COLUMN IF NOT EXISTS news_count INT`
  - [x] Index: `idx_symbol_features_news_sentiment ON symbol_daily_features(symbol, news_sentiment_aggregate)`
  - [x] Document `news_correlation` JSONB shape
    - [x] `driver`, `affected_metric`, `affected_theme`, `direction`, `confidence`
    - [x] `evidence.top_headlines.stock[]`, `.local[]`, `.global[]`
    - [x] `evidence.net_news_impact_score`
    - [x] `driver_source`, `stock_news_fetched_count`, `stock_news_coverage`, `available`

- [x] **Grants / RLS (news.md has no explicit GRANT statements)**
  - [x] Verify Supabase service-role key used by TITAN can INSERT/SELECT/DELETE on new tables
  - [x] Review RLS policies if enabled (anon vs service role)
  - [x] Document any required `GRANT` / policy SQL if production requires it

- [ ] **Run migration in Supabase**
  - [x] Execute `sql/create_news_tables.sql` in staging
  - [ ] Execute in production after staging validation
  - [x] Smoke-test insert/select on all four table groups (`news_feed`, `news_sentiment_cache`, `symbol_news_snapshots`, macro `global_news_snapshots`)

---

## Phase 2 — Core Modules (`src/`)

### 2.1 Dependencies

- [x] **Base `requirements.txt` additions**
  - [x] `newsapi-python>=0.1.1`
  - [x] `finnhub-python>=1.2.14`
  - [x] `feedparser>=6.0.10`
  - [x] `requests>=2.32.0`
  - [x] `nltk>=3.8.1`

- [x] **Optional heavy deps (`requirements-news.txt` or extras)**
  - [x] `transformers>=4.40.0`
  - [x] `torch>=2.0.0`
  - [x] Document install: `pip install -r requirements-news.txt` for FinBERT path
  - [x] CI workflow explicitly skips these (VADER-only)

- [x] **NLTK data bootstrap**
  - [x] Ensure `vader_lexicon` downloaded on first VADER use (or document manual `nltk.download`)

### 2.2 `src/news_client.py` — Fetcher & Normalization

- [x] **Create module** `src/news_client.py`

- [x] **Function: `fetch_news_from_newsapi`**
  - [x] Signature: `(symbol, exchange="NSE", lookback_hours=24, api_key=None) -> list[dict]`
  - [x] Read `NEWSAPI_API_KEY` from env when `api_key` is None
  - [x] Respect free tier: up to 100 articles/day
  - [x] Filter by lookback window (`lookback_hours`)
  - [x] Return normalized news items with metadata

- [x] **Function: `fetch_news_from_finnhub`**
  - [x] Signature: `(symbol, api_key=None, lookback_hours=24) -> list[dict]`
  - [x] Read `FINNHUB_API_KEY` from env when `api_key` is None
  - [x] Indian market / real-time focus
  - [x] Handle rate limit 429 with backoff (see Risks)

- [x] **Function: `fetch_news_from_rss_feeds`**
  - [x] Signature: `(feeds=None, symbol="", lookback_hours=24) -> list[dict]`
  - [x] Default feeds from `TITAN_NEWS_FEEDS` env (comma-separated URLs)
  - [x] Support Bloomberg, Reuters, CNBC-TV18, Moneycontrol, ET Markets
  - [x] Parse RSS/Atom via `feedparser`

- [x] **Function: `fetch_all_news_for_symbol`**
  - [x] Signature: `(symbol, exchange="NSE", cfg=None) -> list[dict]`
  - [x] Orchestrate all sources (parallel fetch pattern per Appendix E: `ThreadPoolExecutor(max_workers=3)`)
  - [x] Apply `TITAN_NEWS_FETCH_LIMIT` (default 40) cap per symbol
  - [x] Apply `TITAN_NEWS_MAX_AGE_HOURS` (default 36) freshness filter
  - [x] Deduplicate via `deduplicate_news_items`
  - [x] Rank by relevance

- [x] **Function: `normalize_news_item`**
  - [x] Signature: `(raw, symbol, exchange, source) -> dict`
  - [x] Map source-specific fields (NewsAPI `publishedAt`, nested `source.name`, etc.)
  - [x] Output keys align with `news_feed` columns: `symbol`, `exchange`, `title`, `url`, `source`, `published_at`, `summary`, `relevance_score`, etc.
  - [x] Compute `relevance_score` (0.0–1.0); symbol keyword match → high score (>0.8 in tests)

- [x] **Function: `deduplicate_news_items`**
  - [x] Signature: `(items) -> list[dict]`
  - [x] Dedupe by URL (primary)
  - [x] Dedupe by title hash (secondary)
  - [x] Preserve first occurrence

### 2.3 `src/news_sentiment.py` — Sentiment Analysis

- [x] **Create module** `src/news_sentiment.py`

- [x] **Function: `compute_sentiment_vader`**
  - [x] Signature: `(text) -> dict`
  - [x] Return keys: `sentiment` (`positive`/`negative`/`neutral`/`mixed`), `score` (float −1..+1)
  - [x] ~1 ms per item; default path

- [x] **Function: `compute_sentiment_transformers`**
  - [x] Signature: `(text, model_name="ProsusAI/finbert") -> dict`
  - [x] Optional FinBERT / DistilBERT-Financial path (~0.5 s/item GPU; ~5 s CPU)
  - [x] Guard import of `transformers`/`torch`; fail gracefully with clear error if extras not installed
  - [x] Return sentiment + score + confidence + model metadata

- [x] **Function: `aggregate_sentiment`**
  - [x] Signature: `(items, weight_by_relevance=True) -> dict`
  - [x] Return keys: `aggregate_sentiment`, `aggregate_score`
  - [x] Weighted mode: use `relevance_score` per item
  - [x] Unweighted mode: equal average

- [x] **Function: `extract_event_type`**
  - [x] Signature: `(title, text="") -> str`
  - [x] Buckets: `earnings`, `acquisition`, `regulatory`, `dividend`, `general`

- [x] **Function: `extract_company_entities`**
  - [x] Signature: `(text) -> list[str]`
  - [x] Extract company names / tickers from body text

- [x] **Model selection via env**
  - [x] Read `TITAN_SENTIMENT_MODEL` (`vader` default | `finbert`)
  - [x] Route scoring in store/fetch pipeline accordingly

### 2.4 `src/news_store.py` — Supabase Persistence

- [x] **Create module** `src/news_store.py`

- [x] **Function: `store_news_items`**
  - [x] Signature: `(cfg, items) -> dict[str, int]`
  - [x] Insert into `news_feed` with deduplication
  - [x] Handle duplicate URL via upsert / on_conflict (Appendix C: normal constraint violation → skip)
  - [x] Return counts: `inserted`, `duplicates_skipped` (and any `updated` if upsert)
  - [x] Populate `news_sentiment_cache` when sentiment computed
  - [x] Set `is_duplicate` / `duplicate_of_id` via `mark_news_as_duplicate` when detected (title-hash via `news_sentiment_cache`)

- [x] **Function: `get_recent_news_for_symbol`**
  - [x] Signature: `(cfg, symbol, exchange="NSE", lookback_hours=None, limit=20) -> list[dict]`
  - [x] Default `lookback_hours` from `TITAN_NEWS_MAX_AGE_HOURS` (36)
  - [x] Order by `published_at DESC`
  - [x] Respect `limit`

- [x] **Function: `get_symbol_news_snapshot`**
  - [x] Signature: `(cfg, symbol, force_refresh=False) -> dict`
  - [x] Read/write **`symbol_news_snapshots`** table (not macro `global_news_snapshots`)
  - [x] Honor `TITAN_NEWS_SNAPSHOT_TTL_HOURS` (default 2) / `ttl_seconds` (default 7200)
  - [x] Build aggregates: `news_count`, `recent_news_items`, `aggregate_sentiment`, `aggregate_score`, `sentiment_trend`, `top_drivers`, `event_alerts`
  - [x] Return cached row if fresh and `force_refresh=False`

- [x] **Function: `mark_news_as_duplicate`**
  - [x] Signature: `(cfg, news_id, duplicate_of_id) -> None`
  - [x] Set `is_duplicate=TRUE`, link `duplicate_of_id`

- [x] **Function: `cleanup_old_news`**
  - [x] Signature: `(cfg, older_than_hours=72) -> dict[str, int]`
  - [x] Delete/prune stale rows from `news_feed` (and cascade cache)
  - [x] Return `deleted` count

### 2.5 `src/news_audit.py` — Quality Checks & Correlation

- [x] **Create module** `src/news_audit.py`

- [x] **Function: `validate_news_payload`**
  - [x] Signature: `(audit) -> tuple[bool, list[str]]`
  - [x] Validate enriched audit dict fields/types before Gemini / persist

- [x] **Function: `compute_news_sentiment_trend`**
  - [x] Signature: `(cfg, symbol, window_hours=24) -> dict`
  - [x] Return keys: `trend` (e.g. `strengthening`), `trend_score` (float)

- [x] **Function: `correlate_news_with_price_move`**
  - [x] Signature: `(cfg, symbol, audit_data) -> dict`
  - [x] Compare sentiment direction vs `return_1d_pct` from audit
  - [x] Return keys: `aligned` (bool)
  - [x] When not aligned: `contradiction_strength`, `possible_reason`

- [x] **Function: `extract_news_drivers`**
  - [x] Signature: `(items, limit=3) -> list[dict]`
  - [x] Rank by `impact_level × relevance_score`
  - [x] Respect `TITAN_NEWS_DRIVER_LIMIT` (default 3)

---

## Phase 3 — Integration Points

### 3.1 `src/sector_audit.py` — Per-Symbol News Enrichment Hook

- [x] **Insert enrichment block after `_refresh_symbol_scoring_outputs(audit)`**
  - [x] Primary hook location: ~line 2284 (per-symbol processing path)
  - [x] Also verify batch path ~line 2607 (`_refresh_symbol_scoring_outputs(r["audit"])`) if enrichment needed there too

- [x] **Imports (inside try block)**
  - [x] `from news_store import get_recent_news_for_symbol`
  - [x] `from news_sentiment import aggregate_sentiment`
  - [x] `from news_audit import compute_news_sentiment_trend, correlate_news_with_price_move`

- [x] **Env-driven parameters**
  - [x] `lookback_hours = int(os.environ.get("TITAN_NEWS_MAX_AGE_HOURS", 36))`
  - [x] `driver_limit = int(os.environ.get("TITAN_NEWS_DRIVER_LIMIT", 3))`

- [x] **Fetch & enrich when news exists**
  - [x] Call `get_recent_news_for_symbol(cfg, symbol, exchange, lookback_hours=..., limit=driver_limit * 2)`
  - [x] Set `audit["recent_news"] = recent_news[:driver_limit]`
  - [x] Aggregate: `audit["news_sentiment_aggregate"]`, `audit["news_sentiment_score"]`, `audit["news_count"]`
  - [x] Trend: `audit["news_sentiment_trend"]`, `audit["news_sentiment_trend_score"]`
  - [x] Price alignment: `audit["news_price_alignment"]`
  - [x] On misalignment: `audit["news_price_contradiction"]`, `audit["news_price_contradiction_reason"]`

- [x] **Defaults when no news**
  - [x] `audit["recent_news"] = []`
  - [x] `audit["news_count"] = 0`
  - [x] `audit["news_sentiment_aggregate"] = "neutral"`
  - [x] `audit["news_sentiment_score"] = 0.0`

- [x] **Error handling (non-blocking)**
  - [x] Wrap in `try/except`
  - [x] `logger.warning(f"News enrichment failed for {inst.symbol}: {e}")`
  - [x] Set `audit["news_error"] = str(e)`
  - [x] Do not raise; sector run continues

- [x] **Coexistence with existing macro correlation**
  - [x] Keep `_apply_global_news_correlation` (~line 2609) and `news_correlation` macro path intact
  - [x] New per-symbol fields (`recent_news`, `news_sentiment_*`, `news_price_*`) complement — do not remove macro evidence

### 3.2 `src/brain.py` — Gemini System Prompt

- [x] **Update `TITAN_V12_SYSTEM_INSTRUCTION` (~line 40)**
  - [x] Title: "Titan V12.0 Forensic Analyst with News Intelligence"
  - [x] Protocol bullet: analyze market structure, positioning, risk **and recent financial news**
  - [x] Protocol bullet: cite top drivers (headline, source, recency) when news available
  - [x] Protocol bullet: flag sentiment/price contradictions (e.g. positive earnings but stock down)
  - [x] Protocol bullet: highlight event catalysts (earnings beats, regulatory approvals, M&A)
  - [x] Protocol bullet: mention news-technical alignment / divergence
  - [x] Retain compliance bullets: no investment advice, no Buy/Sell/Target/SL/Stop Loss
  - [x] Retain: single concise post <280 chars when standalone
  - [x] Retain: mental policy compliance check

- [x] **Verify JSON payload auto-includes news fields**
  - [x] `recent_news`, `news_sentiment_aggregate`, `news_sentiment_score`
  - [x] `news_sentiment_trend`, `news_price_alignment`, `news_price_contradiction`
  - [x] Existing `news_correlation` from macro path still serialized

### 3.3 `src/analysis_store.py` — Persist News Fields

- [x] **Extend symbol daily feature row builder**
  - [x] Persist `news_correlation` (already in `tape_extras` — verify top-level columns too after migration)
  - [x] Add `news_sentiment_aggregate` from audit
  - [x] Add `news_sentiment_score` from audit
  - [x] Add `news_sentiment_trend` from audit
  - [x] Add `news_count` from audit
  - [x] Include new audit enrichment fields in upsert to `symbol_daily_features`

- [x] **Reconcile / analytics paths**
  - [x] Ensure reconcile fetch includes new columns where applicable
  - [x] `_parse_news_direction` / `_news_summary_from_audit` compatible with enriched payloads

### 3.4 `main.py` — symbol news fetch (decoupled from Titan run)

- [x] **Removed `--news-refresh`** — symbol fetch is only via `scripts/fetch_news_batch.py` / `news_fetch.yml` (UI button or schedule)
- [x] **Titan run** reads `news_feed` / `symbol_news_snapshots` via `_enrich_audit_with_symbol_news` only
- [x] **UI:** `docs/index.html` **Fetch symbol news** → `news_fetch.yml`; macro **Refresh Global News** unchanged
- [x] **Proxy:** `news_fetch.yml` in `ALLOWED_WORKFLOWS`

### 3.5 **DEFERRED — `src/sector_priority.py` (Integration 3.6 / 3.7)**

- [ ] **Mark deferred — do not implement in V12.0 initial pass**
  - [ ] Skip `TITAN_NEWS_BLEND_WEIGHT` (3.5) × `news_sentiment_score` blending
  - [ ] Skip `TITAN_NEWS_BLEND_CAP` (3) clipping
  - [ ] Skip composite: `(technical_score * 0.9) + (news_contribution * 0.1)`
  - [ ] Skip `audit["intent_score_news_blended"]`
  - [ ] Document follow-up task for priority ranking news influence

---

## Phase 4 — Scheduler, Scripts & CI/CD

### 4.1 GitHub Actions: `.github/workflows/news_fetch.yml`

- [x] **Create workflow file**
  - [x] `name: Fetch News and Update Cache`

- [x] **Triggers**
  - [x] Schedule cron: `'30 3,5,7,9,11 * * 1-5'` (every 2 h, 09:00–17:00 IST = 03:30–11:30 UTC, Mon–Fri)
  - [x] `workflow_dispatch` for manual runs

- [x] **Job: `fetch-news`**
  - [x] `runs-on: ubuntu-latest`
  - [x] `timeout-minutes: 30`

- [x] **Step: Checkout**
  - [x] `actions/checkout@v4`

- [x] **Step: Set up Python**
  - [x] `actions/setup-python@v4`
  - [x] `python-version: '3.10'`

- [x] **Step: Install dependencies (VADER-only)**
  - [x] `pip install -r requirements.txt`
  - [x] `pip install newsapi-python finnhub-python feedparser nltk`
  - [x] Do **not** install `torch` / `transformers` in CI

- [x] **Step: Load config from Supabase**
  - [x] Run: `python scripts/load_ci_config_from_supabase.py` (bootstrap: `SUPABASE_ACCESS_TOKEN` or `SUPABASE_KEY` + `SUPABASE_URL`)

- [x] **Step: Fetch and cache news**
  - [x] Config from `titan_secrets` via loader (not per-key GHA secrets)
  - [x] Env vars: `TITAN_NEWS_MAX_AGE_HOURS=36`, `TITAN_NEWS_FETCH_LIMIT=40`
  - [x] Run: `python scripts/fetch_news_batch.py --sectors all --refresh-snapshots`

- [x] **Step: Cleanup old news (>72h)**
  - [x] Uses env populated by Supabase loader step
  - [x] Run: `python scripts/cleanup_news.py --older-than-hours 72`

- [x] **Step: Report status**
  - [x] `if: always()`
  - [x] Echo completion + check `news_feed`, `symbol_news_snapshots` (and macro `global_news_snapshots`)

- [x] **Independence from `run_titan_now.yml`**
  - [x] Separate schedule; no coupling to sector audit workflow

### 4.2 `scripts/fetch_news_batch.py`

- [x] **Create script with shebang** `#!/usr/bin/env python3`

- [x] **Docstring usage examples**
  - [x] `python scripts/fetch_news_batch.py --sectors all --refresh-snapshots`
  - [x] `python scripts/fetch_news_batch.py --sectors defence,banking`

- [x] **Path bootstrap**
  - [x] `ROOT = Path(__file__).resolve().parent.parent`
  - [x] `sys.path.insert(0, str(ROOT / "src"))`

- [x] **Imports**
  - [x] `config_loader.load_config`
  - [x] `sector_registry.list_active_sector_ids`, `load_sector_instruments`
  - [x] `news_client.fetch_all_news_for_symbol`
  - [x] `news_store.store_news_items`, `get_symbol_news_snapshot`

- [x] **Logging**
  - [x] `logging.basicConfig(level=INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")`

- [x] **Function: `fetch_and_store_for_symbol(cfg, symbol, exchange, refresh_snapshots) -> dict`**
  - [x] Fetch via `fetch_all_news_for_symbol`
  - [x] Store via `store_news_items`
  - [x] If `refresh_snapshots` and `inserted > 0`: `get_symbol_news_snapshot(cfg, symbol, force_refresh=True)`
  - [x] Return dict: `symbol`, `fetched`, `stored`, `duplicates`, `error`

- [x] **CLI arguments**
  - [x] `--sectors` (default `all`; comma-separated IDs)
  - [x] `--refresh-snapshots` (flag)
  - [x] `--workers` (int, default 4; tunable to 8 per Appendix D)

- [x] **`main()` logic**
  - [x] Load config
  - [x] Resolve sector IDs (`all` → `list_active_sector_ids(include_unknown=False)`)
  - [x] Collect `(symbol, exchange, sector_id)` pairs from `load_sector_instruments`
  - [x] Parallel `ThreadPoolExecutor(max_workers=args.workers)`
  - [x] Per-future timeout 30 s
  - [x] Aggregate totals; log failed symbols (first 10)
  - [x] Exit code 0 if no failures else 1

### 4.3 `scripts/cleanup_news.py`

- [x] **Create script**

- [x] **Docstring usage**
  - [x] `python scripts/cleanup_news.py --older-than-hours 72`

- [x] **CLI argument**
  - [x] `--older-than-hours` (int, default 72)

- [x] **Implementation**
  - [x] Call `cleanup_old_news(cfg, older_than_hours=args.older_than_hours)`
  - [x] Log deleted count
  - [x] Return exit code 0

### 4.4 Operational schedule summary

- [x] Fetcher runs ~every 2 h during market hours (~12 fetches/day max per Appendix F)
- [x] Retention default 72 h (3 days) via cleanup step
- [x] Snapshot TTL default 2 h (`TITAN_NEWS_SNAPSHOT_TTL_HOURS` / `ttl_seconds=7200`)

---

## Phase 5 — Testing Strategy

### 5.1 `tests/test_news_client.py`

- [x] **Create file**

- [x] **`test_normalize_news_item_newsapi`**
  - [x] NewsAPI raw dict with nested `source`, `publishedAt`, `description`
  - [x] Assert `symbol`, `exchange`, `title`, `url`, `source=="newsapi"`
  - [x] Assert `relevance_score > 0.8`

- [x] **`test_deduplicate_news_items`**
  - [x] Duplicate title different URL → one removed (2 of 3 remain)
  - [x] Assert URLs `example.com/1` and `example.com/3` preserved

- [x] **`test_fetch_news_live_newsapi`**
  - [x] `@pytest.mark.skipif(not os.environ.get("NEWSAPI_API_KEY"))`
  - [x] Live fetch for `INFY` / `NSE`
  - [x] Assert items non-empty; all have `symbol`, `title`; all symbols match

### 5.2 `tests/test_news_sentiment.py`

- [x] **Create file**

- [x] **`test_sentiment_vader_positive`**
  - [x] Text: profit jumps / beating estimates
  - [x] Assert `sentiment == "positive"`, `score > 0.3`

- [x] **`test_sentiment_vader_negative`**
  - [x] Text: crashes / regulatory action
  - [x] Assert `sentiment == "negative"`, `score < -0.3`

- [x] **`test_sentiment_vader_neutral`**
  - [x] Mixed performance text
  - [x] Assert `sentiment in ["neutral", "mixed"]`, `abs(score) < 0.2`

- [x] **`test_aggregate_sentiment_weighted`**
  - [x] High-relevance positive + low-relevance negative → net positive
  - [x] Assert `aggregate_sentiment == "positive"`, `aggregate_score > 0.3`

- [x] **`test_aggregate_sentiment_equal_weight`**
  - [x] Equal relevance ±0.8 → neutral
  - [x] Assert `aggregate_sentiment == "neutral"`, `abs(aggregate_score) < 0.1`

### 5.3 `tests/test_sector_audit_with_news.py`

- [x] **Create file**

- [x] **`test_store_and_retrieve_news`**
  - [x] `@pytest.mark.skipif(not SUPABASE_URL)`
  - [x] Insert `TESTSTOCK` fixture via `store_news_items`
  - [x] Retrieve via `get_recent_news_for_symbol` with 1 h lookback
  - [x] Assert title match

- [x] **`test_aggregate_sentiment_from_db`**
  - [x] `@pytest.mark.skipif(not SUPABASE_URL)`
  - [x] Fetch `INFY` news (limit 10); if non-empty, aggregate
  - [x] Assert keys present; score in [-1.0, 1.0]

### 5.4 Additional tests (recommended from integration scope)

- [ ] **`tests/test_news_store.py`** (if not merged into above)
  - [ ] Snapshot TTL cache hit/miss for `symbol_news_snapshots` (covered partially in `test_sector_audit_with_news.py`)
  - [ ] `cleanup_old_news` deletion counts (mocked Supabase)

- [x] **`tests/test_news_audit.py`**
  - [x] `validate_news_payload` valid/invalid cases
  - [x] `correlate_news_with_price_move` aligned vs contradiction paths
  - [x] `extract_news_drivers` ranking by impact × relevance

- [x] **Extend `tests/test_sector_audit.py`**
  - [x] News enrichment hook sets fields on mock news
  - [x] Enrichment failure sets `news_error` without raising

- [x] **Extend `tests/test_analysis_store.py`**
  - [x] New columns persisted in `symbol_daily_features` row

### 5.5 Test execution

- [x] Run unit tests locally (commands TBD in project workflow)
- [ ] Run integration tests when Supabase configured
- [ ] CI: unit tests VADER-only; skip FinBERT live tests

---

## Phase 6 — Environment, Documentation & Deployment

### 6.1 Appendix A — Environment Variables

- [x] **API keys**
  - [x] `NEWSAPI_API_KEY` — https://newsapi.org
  - [x] `FINNHUB_API_KEY` — https://finnhub.io

- [x] **News sources**
  - [x] `TITAN_NEWS_FEEDS` — comma-separated RSS URLs

- [x] **Freshness & caching**
  - [x] `TITAN_NEWS_MAX_AGE_HOURS=36`
  - [x] `TITAN_NEWS_SNAPSHOT_TTL_HOURS=2`
  - [x] `TITAN_NEWS_SNAPSHOT_TABLE=global_news_snapshots` (macro table name)
  - [x] Add `TITAN_SYMBOL_NEWS_SNAPSHOT_TABLE=symbol_news_snapshots` (if env-driven per-symbol table)

- [x] **Limits**
  - [x] `TITAN_NEWS_FETCH_LIMIT=40`
  - [x] `TITAN_NEWS_DRIVER_LIMIT=3`

- [ ] **Blending (deferred — document only)**
  - [ ] `TITAN_NEWS_BLEND_WEIGHT=3.5`
  - [ ] `TITAN_NEWS_BLEND_CAP=3`

- [x] **Sentiment model**
  - [x] `TITAN_SENTIMENT_MODEL=vader` (default) | `finbert`

- [ ] **Existing stock-news vars in `config/.env.example` (preserve / wire)**
  - [ ] `TITAN_STOCK_NEWS_ENABLE_NSE=true`
  - [ ] `TITAN_STOCK_NEWS_FETCH_LIMIT=8`
  - [ ] `TITAN_STOCK_NEWS_MIN_RELEVANCE=0.35`
  - [ ] `TITAN_STOCK_NEWS_NEGATIVE_KEYWORDS=...`
  - [ ] `TITAN_STOCK_NEWS_POSITIVE_KEYWORDS=...`
  - [ ] `TITAN_STOCK_NEWS_QUALITY_SOURCES=...`

- [x] **Update `config/.env.example`**
  - [x] Uncomment/add all news vars above with comments
  - [x] Add `NEWSAPI_API_KEY`, `FINNHUB_API_KEY`, `TITAN_SENTIMENT_MODEL`

- [x] **Supabase-stored config (`public.titan_secrets`)** — supersedes per-key GitHub secrets for news/CI
  - [x] Table: `key_name`, `value`, `updated_at`, `description` — `sql/create_titan_secrets.sql`
  - [x] Keys: `NEWSAPI_API_KEY`, `FINNHUB_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`, `TITAN_NEWS_FEEDS`
  - [x] Runtime: `src/news_config.load_news_runtime_config()` (env first, Supabase fallback, 60s TTL cache)
  - [x] Local upsert from `.env`: `scripts/apply_titan_secrets_migration.py` (requires `SUPABASE_ACCESS_TOKEN`)
  - [x] CI loader: `scripts/load_ci_config_from_supabase.py` in `news_fetch.yml`
  - [x] **Optional minimal GHA bootstrap** (one secret): `SUPABASE_ACCESS_TOKEN` *or* `SUPABASE_KEY` + `SUPABASE_URL`
  - [x] ~~GitHub Actions secrets~~ `NEWSAPI_API_KEY`, `FINNHUB_API_KEY`, `TITAN_NEWS_FEEDS` — moved to Supabase
  - [x] Breeze token stays in `session_config` id=1 (`breeze_session_token`) — separate from news secrets

### 6.2 Documentation files

- [x] Keep `news.md` / `TITAN_NEWS_INTEGRATION_PLAN.md` as spec reference
- [x] Update deployment docs with news workflow (if project has README / deep-dive — only when behavior changes)
- [x] Document macro vs `symbol_news_snapshots` table split
- [x] Document optional FinBERT install path (`requirements-news.txt`)
- [x] Document Appendix E pattern for adding custom sources (e.g. Twitter/X)

### 6.3 Pre-Deployment Checklist (from news.md)

- [x] All Python modules created and tested locally
- [x] Supabase tables created via SQL migration
- [x] Environment variables configured in Supabase `titan_secrets` (optional minimal GHA bootstrap only)
- [x] Dependencies added to `requirements.txt` (+ optional `requirements-news.txt`)
- [x] `.env.example` updated with news variables
- [x] Unit tests passing locally
- [ ] Integration tests passing (if Supabase configured)

### 6.4 Deployment Steps (from news.md)

- [x] Create Supabase tables (run SQL schema)
- [x] Update `requirements.txt` with news dependencies
- [x] Add modules to `src/`: `news_client.py`, `news_sentiment.py`, `news_store.py`, `news_audit.py`
- [x] Add scripts: `fetch_news_batch.py`, `cleanup_news.py`
- [x] Create `.github/workflows/news_fetch.yml`
- [x] Modify `sector_audit.py` news enrichment hook
- [x] Modify `brain.py` system prompt
- [x] Add tests under `tests/`
- [x] Update `.env.example` and documentation
- [ ] Test in GitHub Actions (dry-run with limited symbols e.g. `--sectors defence`)
- [ ] Monitor logs; adjust tuning knobs if needed (blend weight deferred)
- [ ] Enable production schedule

### 6.5 Post-Deployment Monitoring (from news.md)

- [ ] Check GitHub Actions logs for `news_fetch.yml`
- [ ] Verify Supabase tables populating (`news_feed`, `symbol_news_snapshots`, macro `global_news_snapshots`)
- [ ] Monitor sector audit enrichment (audit dicts contain news fields)
- [ ] Verify Gemini narratives mention news drivers
- [ ] Check sentiment accuracy against manual samples
- [ ] Deferred: adjust `TITAN_NEWS_BLEND_WEIGHT` when priority blending implemented

---

## Risks & Mitigations

- [x] **NewsAPI 401 — invalid API key**
  - [x] Verify `NEWSAPI_API_KEY` in Supabase `titan_secrets` / local `.env`

- [x] **Finnhub 429 — rate limit**
  - [x] Implement exponential backoff between requests
  - [x] Increase fetch interval (cron already 2 h)
  - [x] Limit parallel workers if needed

- [x] **Duplicate URL constraint violation**
  - [x] Treat as normal; use upsert/on_conflict in `store_news_items`
  - [x] Increment `duplicates_skipped`

- [x] **Sentiment computation timeout (FinBERT slow)**
  - [x] Default VADER in prod/CI
  - [x] Reduce batch size if using FinBERT
  - [x] Cache via `news_sentiment_cache` + title hash

- [x] **Missing news in audit**
  - [x] Verify `news_fetch.yml` ran successfully
  - [x] Check Supabase row counts per symbol
  - [x] Confirm enrichment hook not skipped on errors silently (check `news_error`)

- [x] **Blank news fields in Gemini output**
  - [x] Ensure sentiment computation does not fail silently
  - [x] Validate payload via `validate_news_payload`

- [x] **Contradiction flags never trigger**
  - [x] Review `correlate_news_with_price_move` thresholds vs `return_1d_pct`
  - [x] Test `news_price_contradiction_reason` logic

- [x] **Schema conflict macro vs per-symbol snapshots**
  - [x] Never write per-symbol aggregates to macro `global_news_snapshots`
  - [x] Use `symbol_news_snapshots` exclusively for `get_symbol_news_snapshot`

- [x] **API quota exhaustion**
  - [x] NewsAPI: 100 articles/day free; 500/day with paid key
  - [x] Finnhub: 60 calls/min free tier
  - [x] RSS: no hard limit; respect ToS caching rules

- [x] **Credential leak**
  - [x] Gitignore `stocks-484613-*.json` and similar
  - [x] Never commit API keys or service account JSON

- [x] **RSS / GDPR compliance (Appendix F)**
  - [x] Verify feed ToS allow caching
  - [x] Store metadata only (title, url, summary) — not full article body unless licensed
  - [x] Do not store user IP / PII

- [x] **Performance tuning knobs (Appendix D)**
  - [x] `fetch_news_batch.py --workers 4` (default) → 8 for faster runs
  - [x] `TITAN_NEWS_SNAPSHOT_TTL_HOURS=2` → 4–6 for less recomputation
  - [x] VADER ~1 ms/item vs FinBERT ~0.5 s GPU / ~5 s CPU

---

## Appendix Coverage — Future / Optional

- [ ] **Appendix E — Custom news source extension**
  - [ ] Add `fetch_news_from_<source>()` in `news_client.py`
  - [ ] Register in `fetch_all_news_for_symbol` ThreadPoolExecutor futures map
  - [ ] Add credentials to `.env.example` + GitHub secrets

- [ ] **Appendix B — Sample enriched audit payload validation**
  - [ ] End-to-end test audit matches shape: `recent_news[]` with `id`, `impact_level`, `event_type`
  - [ ] Fields: `news_sentiment_trend_score`, `news_price_alignment`, `news_count`

---

**End of checklist.** Unchecked items: deferred `sector_priority` blending (3.6/3.7), production deploy/monitoring, GHA dry-run, optional `test_news_store.py`, legacy stock-news env vars. Finnhub 403 (forbidden) on some symbols — note only; not fixed in V12 pass.
