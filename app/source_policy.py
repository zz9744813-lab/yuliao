"""Single source for human evidence source types admitted to K2/K3/K5.

An unknown registry type is never proof that a text is a usable human source.
The allowlist is intentionally independent of identity and license purposes.
"""
from __future__ import annotations

HUMAN_SOURCE_TYPES = frozenset({"human_fiction"})
PRODUCTION_NONBENCHMARK_PREFIX = "production_nonbenchmark_"


def compliant_human_source(source_type: object) -> bool:
    if not isinstance(source_type, str):
        return False
    value = source_type.strip()
    return (value in HUMAN_SOURCE_TYPES or
            value.startswith(PRODUCTION_NONBENCHMARK_PREFIX))
