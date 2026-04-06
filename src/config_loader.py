"""Load secrets and configuration from environment (optionally via .env)."""

from __future__ import annotations

import os
from collections.abc import Mapping
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
    gemini_api_keys: tuple[str, ...]
    supabase_url: str
    supabase_key: str

    @property
    def gemini_api_key(self) -> str:
        """First Gemini key (backward compatible)."""
        return self.gemini_api_keys[0]


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


def parse_gemini_api_keys_from_env(
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """
    Collect Gemini API keys for rotation on quota (429).

    Precedence:
    - GEMINI_API_KEYS: comma-separated list (if non-empty, used as the base list)
    - Else GEMINI_API_KEY: one key, or comma-separated multiple keys
    - Then append GEMINI_API_KEY_2 .. GEMINI_API_KEY_5 if set (e.g. second GitHub secret)

    Order is preserved; duplicates are dropped.
    Pass ``env`` to read from a merged mapping (e.g. os.environ overlaid with .env file).
    """
    def get(name: str) -> str:
        if env is None:
            return os.environ.get(name, "").strip()
        raw = env.get(name, "")
        return str(raw).strip() if raw is not None else ""

    keys: list[str] = []
    bulk = get("GEMINI_API_KEYS")
    if bulk:
        keys.extend(x.strip() for x in bulk.split(",") if x.strip())
    else:
        primary = get("GEMINI_API_KEY")
        if primary:
            if "," in primary:
                keys.extend(x.strip() for x in primary.split(",") if x.strip())
            else:
                keys.append(primary)
    for i in range(2, 6):
        extra = get(f"GEMINI_API_KEY_{i}")
        if extra:
            keys.append(extra)
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    if not out:
        raise ValueError(
            "Missing Gemini API key: set GEMINI_API_KEY or GEMINI_API_KEYS with at least one key"
        )
    return tuple(out)


def try_parse_gemini_api_keys_from_env(
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Like parse_gemini_api_keys_from_env, but returns () when nothing is configured."""
    try:
        return parse_gemini_api_keys_from_env(env)
    except ValueError:
        return ()


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
        gemini_api_keys=parse_gemini_api_keys_from_env(),
        supabase_url=req("SUPABASE_URL"),
        supabase_key=req("SUPABASE_KEY"),
    )
