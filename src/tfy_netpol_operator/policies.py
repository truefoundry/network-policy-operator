"""Builders for the three managed NetworkPolicy objects.

All objects are plain dicts (server-side friendly) so they can be applied via the
Kubernetes client and compared cheaply.
"""

from __future__ import annotations

MANAGED_BY = "tfy-netpol-operator"
MANAGED_LABELS = {"app.kubernetes.io/managed-by": MANAGED_BY}
MANAGED_BY_SELECTOR = f"app.kubernetes.io/managed-by={MANAGED_BY}"

NAME_DENY_INGRESS = "tfy-np-default-deny-ingress"
NAME_ALLOW_EGRESS = "tfy-np-allow-egress"
NAME_ALLOW_INGRESS = "tfy-np-allow-ingress"

MANAGED_NAMES = (NAME_DENY_INGRESS, NAME_ALLOW_EGRESS, NAME_ALLOW_INGRESS)

# Built-in immutable namespace name label (Kubernetes >= 1.21).
NS_NAME_LABEL = "kubernetes.io/metadata.name"


def _metadata(name: str, namespace: str) -> dict:
    return {"name": name, "namespace": namespace, "labels": dict(MANAGED_LABELS)}


def build_default_deny_ingress(namespace: str) -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": _metadata(NAME_DENY_INGRESS, namespace),
        "spec": {"podSelector": {}, "policyTypes": ["Ingress"]},
    }


def build_allow_all_egress(namespace: str) -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": _metadata(NAME_ALLOW_EGRESS, namespace),
        "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]},
    }


def build_allow_ingress(
    namespace: str,
    sources: list[str],
    node_cidrs: list[str] | None = None,
    allow_all_namespaces: bool = False,
) -> dict:
    froms: list[dict] = []
    if allow_all_namespaces:
        froms.append({"namespaceSelector": {}})
    else:
        # self first, then declared/baseline sources
        froms.append({"namespaceSelector": {"matchLabels": {NS_NAME_LABEL: namespace}}})
        for ns in sources:
            froms.append({"namespaceSelector": {"matchLabels": {NS_NAME_LABEL: ns}}})
    for cidr in node_cidrs or []:
        froms.append({"ipBlock": {"cidr": cidr}})
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": _metadata(NAME_ALLOW_INGRESS, namespace),
        "spec": {
            "podSelector": {},
            "policyTypes": ["Ingress"],
            "ingress": [{"from": froms}],
        },
    }


def build_policy_set(
    namespace: str,
    sources: list[str],
    node_cidrs: list[str] | None = None,
    allow_all_namespaces: bool = False,
) -> list[dict]:
    """Return the policy set in apply order: default-deny, egress, then allow-ingress.

    Deny-before-allow: the default-deny is applied first to lock down ingress
    before the allow rules are added.
    """
    return [
        build_default_deny_ingress(namespace),
        build_allow_all_egress(namespace),
        build_allow_ingress(namespace, sources, node_cidrs, allow_all_namespaces),
    ]
