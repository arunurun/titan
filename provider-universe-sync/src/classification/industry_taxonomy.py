from __future__ import annotations

from src.models import normalize_sector_key


_RULES: tuple[tuple[str, str], ...] = (
    ("defence", "defence"),
    ("aerospace", "defence"),
    ("bank", "banks_private"),
    ("financial", "nbfc_financial_services"),
    ("nbfc", "nbfc_financial_services"),
    ("insurance", "insurance"),
    ("software", "it"),
    ("it services", "it"),
    ("technology", "it"),
    ("telecom", "telecom"),
    ("pharma", "pharma_healthcare"),
    ("health", "pharma_healthcare"),
    ("hospital", "pharma_healthcare"),
    ("fmcg", "fmcg_staples"),
    ("consumer goods", "fmcg_staples"),
    ("consumer durables", "consumer_discretionary"),
    ("automobile", "auto"),
    ("auto ancillary", "auto_ancillary"),
    ("tyres", "auto_ancillary"),
    ("metal", "metals_mining"),
    ("mining", "metals_mining"),
    ("oil", "oil_gas_energy"),
    ("gas", "oil_gas_energy"),
    ("power", "power_utilities"),
    ("utility", "power_utilities"),
    ("capital goods", "capital_goods_industrials"),
    ("industrial", "capital_goods_industrials"),
    ("construction", "infrastructure_construction"),
    ("infrastructure", "infrastructure_construction"),
    ("engineering", "capital_goods_industrials"),
    ("chemical", "chemicals"),
    ("cement", "cement_building_materials"),
    ("building material", "cement_building_materials"),
    ("realty", "realty_reits"),
    ("reit", "realty_reits"),
    ("media", "media"),
    ("entertainment", "media"),
    ("logistics", "logistics"),
    ("shipping", "logistics"),
    ("textile", "textiles"),
)


def map_industry_to_sector_key(industry: str) -> str:
    text = (industry or "").strip().lower()
    if not text:
        return "unknown"
    for needle, sector in _RULES:
        if needle in text:
            return normalize_sector_key(sector)
    return "unknown"
