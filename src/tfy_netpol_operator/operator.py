"""Kopf handlers and reconcile loop for the annotation-driven NetworkPolicy operator.

Single annotation contract (see HLD):
  truefoundry.com/allowed-ingress-namespaces
    - absent           -> namespace not managed (clean up any managed policies)
    - present, empty   -> default-deny ingress + allow-all egress + allow self + baselines
    - present, list    -> the above + allow ingress from each listed namespace
"""

from __future__ import annotations

import kopf

from .config import Config, is_system_namespace
from .k8s import (
    apply_network_policy,
    delete_managed_policies,
    load_kube_config,
    read_namespace_annotations,
)
from .parsing import ANNOTATION, WILDCARD, merge_sources, parse_sources, split_valid
from .policies import (
    MANAGED_BY,
    build_allow_all_egress,
    build_allow_ingress,
    build_default_deny_ingress,
)

cfg = Config()

_MANAGED_LABEL_FILTER = {"app.kubernetes.io/managed-by": MANAGED_BY}


@kopf.on.startup()
def startup(settings: kopf.OperatorSettings, logger, **_):
    global cfg
    load_kube_config()
    cfg = Config.load()
    # Run standalone (single replica). For HA, enable peering and run >1 replica.
    settings.posting.level = 20  # post events at INFO and above
    # This operator only handles built-in resources (namespaces, networkpolicies),
    # so disable Kopf's cluster-wide CRD discovery scan. Avoids needing cluster-scope
    # list/watch on customresourcedefinitions (403s) for a capability we don't use.
    settings.scanning.disabled = True
    logger.info(
        "tfy-netpol-operator started "
        f"(dry_run={cfg.dry_run}, baselines={cfg.baseline_allowed_namespaces}, "
        f"node_cidrs={cfg.node_cidrs}, allow_wildcard={cfg.allow_wildcard})"
    )


def _desired_policies(namespace: str, annotations: dict, logger) -> list[dict]:
    """Compute the ordered policy set [egress, allow-ingress, deny-ingress]."""
    raw = (annotations.get(ANNOTATION) or "").strip()

    if raw == WILDCARD:
        if cfg.allow_wildcard:
            return [
                build_allow_all_egress(namespace),
                build_allow_ingress(namespace, [], cfg.node_cidrs, allow_all_namespaces=True),
                build_default_deny_ingress(namespace),
            ]
        logger.warning(
            f"namespace {namespace}: wildcard '*' is rejected by policy; "
            "applying baseline + self only"
        )
        raw = ""

    user_sources = parse_sources(raw, self_name=namespace)
    combined = merge_sources(user_sources, cfg.baseline_allowed_namespaces, self_name=namespace)
    valid, invalid = split_valid(combined)
    if invalid:
        logger.warning(f"namespace {namespace}: skipping invalid namespace names {invalid}")

    return [
        build_allow_all_egress(namespace),
        build_allow_ingress(namespace, valid, cfg.node_cidrs),
        build_default_deny_ingress(namespace),
    ]


def reconcile(name: str, meta: dict | None, logger, allow_cleanup: bool = True) -> None:
    annotations = (meta or {}).get("annotations") or {}

    if is_system_namespace(name, cfg.denylist):
        return

    if ANNOTATION not in annotations:
        if allow_cleanup:
            delete_managed_policies(name, logger)
        return

    policies = _desired_policies(name, annotations, logger)

    if cfg.dry_run:
        for pol in policies:
            logger.info(f"[dry-run] would apply {pol['metadata']['name']} in {name}")
        return

    # Allow-before-deny, fail-closed: egress + allow first, deny last.
    # If any apply raises, the default-deny (last element) is never reached.
    for pol in policies:
        apply_network_policy(pol, logger)


# --- Namespace events ---------------------------------------------------------

@kopf.on.create("", "v1", "namespaces")
@kopf.on.resume("", "v1", "namespaces")
@kopf.on.update("", "v1", "namespaces")
def on_namespace_event(name, meta, logger, **_):
    reconcile(name, meta, logger, allow_cleanup=True)


@kopf.timer("", "v1", "namespaces", interval=300.0)
def resync(name, meta, logger, **_):
    # Periodic drift correction for enrolled namespaces; skip cleanup listing here.
    reconcile(name, meta, logger, allow_cleanup=False)


# --- Drift on managed policies ------------------------------------------------

@kopf.on.delete("networking.k8s.io", "v1", "networkpolicies", labels=_MANAGED_LABEL_FILTER)
@kopf.on.update("networking.k8s.io", "v1", "networkpolicies", labels=_MANAGED_LABEL_FILTER)
def on_managed_policy_changed(namespace, logger, **_):
    annotations = read_namespace_annotations(namespace)
    if annotations is None:
        return  # namespace is being deleted; nothing to do
    reconcile(namespace, {"annotations": annotations}, logger, allow_cleanup=False)
