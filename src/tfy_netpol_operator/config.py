"""Operator configuration, loaded from a mounted ConfigMap (YAML)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

# Namespaces the operator must never manage, regardless of annotation.
SYSTEM_PREFIXES = ("kube-", "openshift-")
SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}

DEFAULT_CONFIG_PATH = "/etc/tfy-netpol-operator/config.yaml"


@dataclass
class Config:
    baseline_allowed_namespaces: list[str] = field(default_factory=list)
    denylist: set[str] = field(default_factory=set)
    node_cidrs: list[str] = field(default_factory=list)
    dry_run: bool = True
    allow_wildcard: bool = False
    default_deny_ingress: bool = True
    resync_interval: float = 300.0

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = path or os.getenv("CONFIG_PATH", DEFAULT_CONFIG_PATH)
        data: dict = {}
        if os.path.exists(path):
            with open(path) as fh:
                data = yaml.safe_load(fh) or {}
        return cls(
            baseline_allowed_namespaces=list(data.get("baselineAllowedNamespaces", [])),
            denylist=set(data.get("denylist", [])),
            node_cidrs=list(data.get("nodeCIDRs", [])),
            dry_run=bool(data.get("dryRun", True)),
            allow_wildcard=bool(data.get("allowWildcard", False)),
            default_deny_ingress=bool(data.get("defaultDenyIngress", True)),
            resync_interval=float(data.get("resyncIntervalSeconds", 300)),
        )


def is_system_namespace(name: str, denylist: set[str]) -> bool:
    if name in SYSTEM_NAMESPACES or name in denylist:
        return True
    return name.startswith(SYSTEM_PREFIXES)
