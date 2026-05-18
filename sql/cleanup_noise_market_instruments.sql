-- One-shot cleanup: deactivate rows that are not meaningful listed equities.
-- Rules mirror provider-universe-sync/src/equity_filter.py (strict ISIN path only;
-- EQUITY_FILTER_STRICT_ISIN=0 relaxed NSE tickers are not represented here).
--
-- 1) Run the preview SELECT.
-- 2) Optionally run the transaction block to soft-deactivate matching rows only.

-- === Preview: how many active rows look like noise ===
with noise as (
    select id, exchange, symbol, instrument_name, isin
    from public.market_instruments
    where is_active = true
      and (
            isin is null
         or btrim(isin) = ''
         or upper(btrim(isin)) !~ '^IN[A-Z0-9]{10}$'
         or upper(coalesce(instrument_name, '')) like '% INDEX%'
         or upper(coalesce(instrument_name, '')) like '%INDEX %'
         or upper(coalesce(instrument_name, '')) like '% ETF%'
         or upper(coalesce(instrument_name, '')) like '%ETF %'
         or upper(coalesce(instrument_name, '')) like '%MUTUAL FUND%'
         or upper(coalesce(instrument_name, '')) like '%DEBENTURE%'
         or upper(coalesce(instrument_name, '')) like '% NCD %'
         or upper(coalesce(instrument_name, '')) like '%REIT%'
         or upper(coalesce(instrument_name, '')) like '%INVIT%'
         or upper(coalesce(instrument_name, '')) like '% GOI %'
         or upper(coalesce(instrument_name, '')) like '%BEARER%'
         or upper(coalesce(instrument_name, '')) like '%WARRANT%'
      )
)
select count(*) as noise_row_count from noise;

-- Sample (optional):
-- select * from noise order by exchange, symbol limit 50;

-- === Apply: soft-deactivate noise rows and their sector maps only ===
-- begin;
-- create temp table noise_cleanup_ids on commit drop as
--     select id
--     from public.market_instruments
--     where is_active = true
--       and (
--             isin is null
--          or btrim(isin) = ''
--          or upper(btrim(isin)) !~ '^IN[A-Z0-9]{10}$'
--          or upper(coalesce(instrument_name, '')) like '% INDEX%'
--          or upper(coalesce(instrument_name, '')) like '%INDEX %'
--          or upper(coalesce(instrument_name, '')) like '% ETF%'
--          or upper(coalesce(instrument_name, '')) like '%ETF %'
--          or upper(coalesce(instrument_name, '')) like '%MUTUAL FUND%'
--          or upper(coalesce(instrument_name, '')) like '%DEBENTURE%'
--          or upper(coalesce(instrument_name, '')) like '% NCD %'
--          or upper(coalesce(instrument_name, '')) like '%REIT%'
--          or upper(coalesce(instrument_name, '')) like '%INVIT%'
--          or upper(coalesce(instrument_name, '')) like '% GOI %'
--          or upper(coalesce(instrument_name, '')) like '%BEARER%'
--          or upper(coalesce(instrument_name, '')) like '%WARRANT%'
--       );
--
-- update public.market_instruments m
--    set is_active = false,
--        updated_at = now()
--   where m.id in (select id from noise_cleanup_ids);
--
-- update public.instrument_sector_map s
--    set is_active = false,
--        updated_at = now()
--  where s.is_active = true
--    and s.instrument_id in (select id from noise_cleanup_ids);
-- commit;
