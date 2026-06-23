"""Annotation parsing and validation for the NetworkPolicy operator."""

from __future__ import annotations

import re

ANNOTATION = "truefoundry.com/allowed-ingress-namespaces"
WILDCARD = "*"

# DNS-1123 label: lowercase alphanumeric and '-', must start/end alphanumeric, <= 63 chars.
_DNS1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def parse_sources(value: str | None, self_name: str | None = None) -> list[str]:
    """Split a comma-separated annotation value into a clean, ordered, de-duplicated list.

    Drops blanks, the literal "self", and the namespace's own name (self is implied).
    """
    if not value:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in value.split(","):
        item = raw.strip().lower()
        if not item or item == "self":
            continue
        if self_name and item == self_name:
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def is_valid_namespace_name(name: str) -> bool:
    return bool(name) and len(name) <= 63 and bool(_DNS1123_LABEL.match(name))


def split_valid(names: list[str]) -> tuple[list[str], list[str]]:
    """Partition names into (valid, invalid) preserving order."""
    valid: list[str] = []
    invalid: list[str] = []
    for n in names:
        (valid if is_valid_namespace_name(n) else invalid).append(n)
    return valid, invalid


def merge_sources(user: list[str], baseline: list[str], self_name: str) -> list[str]:
    """Combine user sources with baseline sources, drop self, de-duplicate preserving order."""
    combined: list[str] = []
    seen: set[str] = set()
    for s in list(user) + list(baseline):
        if not s or s == self_name or s in seen:
            continue
        seen.add(s)
        combined.append(s)
    return combined
