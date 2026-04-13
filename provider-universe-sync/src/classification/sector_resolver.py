from __future__ import annotations

from src.models import UniverseInstrument, normalize_sector_key


def resolve_sector_key(
    instrument: UniverseInstrument,
    override_map: dict[tuple[str, str], str],
) -> tuple[str, str]:
    """
    Hybrid sector strategy:
    1) Supabase override wins.
    2) Provider official sector.
    3) Fallback to unknown.
    """
    key = (instrument.exchange, instrument.symbol)
    if key in override_map:
        return normalize_sector_key(override_map[key]), "override"

    official = normalize_sector_key(instrument.official_sector_key)
    if official and official != "unknown":
        return official, "official"

    return "unknown", "official"
