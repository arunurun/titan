"""Load secrets and configuration from environment (optionally via .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class BreezeConfig:
    """ICICI Breeze credentials only (for testing data fetch without Supabase/Gemini)."""

    breeze_api_key: str
    breeze_secret: str
    breeze_session_token: str


@dataclass(frozen=True)
class TitanConfig:
    breeze_api_key: str
    breeze_secret: str
    breeze_session_token: str
    gemini_api_key: str
    supabase_url: str
    supabase_key: str


def _find_dotenv() -> Path | None:
    here = Path(__file__).resolve().parent
    for base in (here.parent, *here.parents):
        candidate = base / ".env"
        if candidate.is_file():
            return candidate
    return None


def _load_dotenv_file(env_path: str | Path | None, *, override: bool) -> None:
    if env_path is not None:
        load_dotenv(env_path, override=override)
    else:
        dotenv = _find_dotenv()
        if dotenv is not None:
            load_dotenv(dotenv, override=override)


def load_breeze_config(env_path: str | Path | None = None) -> BreezeConfig:
    """Read only Breeze variables (useful for `fetch_nifty_data` tests without Supabase)."""
    _load_dotenv_file(env_path, override=True)

    def req(name: str) -> str:
        v = os.environ.get(name, "").strip()
        if not v:
            raise ValueError(f"Missing or empty required environment variable: {name}")
        return v

    return BreezeConfig(
        breeze_api_key=req("BREEZE_API_KEY"),
        breeze_secret=req("BREEZE_SECRET"),
        breeze_session_token=req("BREEZE_SESSION_TOKEN"),
    )


def load_config(env_path: str | Path | None = None) -> TitanConfig:
    """Read required variables from the environment."""
    _load_dotenv_file(env_path, override=False)

    def req(name: str) -> str:
        v = os.environ.get(name, "").strip()
        if not v:
            raise ValueError(f"Missing or empty required environment variable: {name}")
        return v

    return TitanConfig(
        breeze_api_key=req("BREEZE_API_KEY"),
        breeze_secret=req("BREEZE_SECRET"),
        breeze_session_token=req("BREEZE_SESSION_TOKEN"),
        gemini_api_key=req("GEMINI_API_KEY"),
        supabase_url=req("SUPABASE_URL"),
        supabase_key=req("SUPABASE_KEY"),
    )
