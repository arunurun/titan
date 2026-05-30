"""DEPRECATED: news secrets live in public.titan_secrets.

Use scripts/apply_titan_secrets_migration.py instead.
Historical SQL: sql/alter_session_config_add_kv.sql (do not apply).
Revert KV merge: scripts/restore_session_config_from_kv.py
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "DEPRECATED: use scripts/apply_titan_secrets_migration.py "
        "(public.titan_secrets). To revert session_config KV merge, run "
        "scripts/restore_session_config_from_kv.py.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
