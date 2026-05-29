-- Add missing sectors with micro/small-cap-biased mappings.
-- Safe to rerun: uses ON CONFLICT upserts and only maps active instruments.
-- Includes: data_centre, electronics_ems, railways_transport_infra, renewables_clean_energy.

begin;

-- 1) Upsert missing sectors in sector catalog.
insert into public.sector_catalog (sector_key, sector_name, is_active, updated_at)
values
    ('data_centre', 'Data Centre', true, now()),
    ('electronics_ems', 'Electronics Ems', true, now()),
    ('railways_transport_infra', 'Railways Transport Infra', true, now()),
    ('renewables_clean_energy', 'Renewables Clean Energy', true, now())
on conflict (sector_key) do update
set
    sector_name = excluded.sector_name,
    is_active = true,
    updated_at = now();

-- 2) Upsert sector mappings from existing active instruments only.
with target_mappings (sector_key, symbol) as (
    values
        -- data_centre (micro/small-cap-biased)
        ('data_centre', 'ANANTRAJ'),
        ('data_centre', 'E2E'),
        ('data_centre', 'NETWEB'),

        -- electronics_ems (micro/small + one liquid anchor)
        ('electronics_ems', 'KAYNES'),
        ('electronics_ems', 'CYIENTDLM'),
        ('electronics_ems', 'AVALON'),
        ('electronics_ems', 'SYRMA'),
        ('electronics_ems', 'PGEL'),
        ('electronics_ems', 'IKIO'),
        ('electronics_ems', 'DCXINDIA'),
        ('electronics_ems', 'DIXON'),

        -- railways_transport_infra (micro/small + select anchors)
        ('railways_transport_infra', 'TITAGARH'),
        ('railways_transport_infra', 'TEXRAIL'),
        ('railways_transport_infra', 'JWL'),
        ('railways_transport_infra', 'IRCON'),
        ('railways_transport_infra', 'RAILTEL'),
        ('railways_transport_infra', 'RVNL'),
        ('railways_transport_infra', 'BEML'),

        -- renewables_clean_energy (micro/small + select anchors)
        ('renewables_clean_energy', 'KPIGREEN'),
        ('renewables_clean_energy', 'INOXWIND'),
        ('renewables_clean_energy', 'SURANASOL'),
        ('renewables_clean_energy', 'GENSOL'),
        ('renewables_clean_energy', 'ACMESOLAR'),
        ('renewables_clean_energy', 'WAAREEENER'),
        ('renewables_clean_energy', 'NTPCGREEN')
),
resolved as (
    select
        tm.sector_key,
        tm.symbol,
        sc.id as sector_id,
        mi.id as instrument_id,
        mi.exchange,
        row_number() over (
            partition by tm.sector_key, tm.symbol
            order by case when mi.exchange = 'NSE' then 0 else 1 end, mi.updated_at desc, mi.created_at desc
        ) as rn
    from target_mappings tm
    inner join public.sector_catalog sc
        on sc.sector_key = tm.sector_key
       and sc.is_active = true
    inner join public.market_instruments mi
        on mi.symbol = tm.symbol
       and mi.is_active = true
       and mi.exchange in ('NSE', 'BSE')
)
insert into public.instrument_sector_map (instrument_id, sector_id, source, is_active, updated_at)
select
    r.instrument_id,
    r.sector_id,
    'override' as source,
    true as is_active,
    now() as updated_at
from resolved r
where r.rn = 1
on conflict (instrument_id, sector_id) do update
set
    source = excluded.source,
    is_active = true,
    updated_at = now();

commit;
