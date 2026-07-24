"""Unit tests for the reconcile loop and desired-policy computation in operator.py.

These exercise the decision logic (which policies, in what order, when to clean up,
dry-run gating, system-namespace exclusion, wildcard handling) without a cluster by
monkeypatching the Kubernetes apply/delete helpers and the module-level Config.
"""

from __future__ import annotations

import pytest

from tfy_netpol_operator import operator
from tfy_netpol_operator.config import Config
from tfy_netpol_operator.parsing import ANNOTATION
from tfy_netpol_operator.policies import (
    NAME_ALLOW_EGRESS,
    NAME_ALLOW_INGRESS,
    NAME_DENY_INGRESS,
    NS_NAME_LABEL,
)


class DummyLogger:
    def __init__(self):
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, msg, *_, **__):
        self.infos.append(str(msg))

    def warning(self, msg, *_, **__):
        self.warnings.append(str(msg))


@pytest.fixture
def logger():
    return DummyLogger()


@pytest.fixture
def applied(monkeypatch):
    """Capture apply/delete/prune calls instead of hitting the API server."""
    calls = {"applied": [], "deleted": [], "pruned": []}

    def fake_apply(body, _logger):
        calls["applied"].append(body)

    def fake_delete(namespace, _logger):
        calls["deleted"].append(namespace)

    def fake_prune(namespace, keep, _logger):
        calls["pruned"].append((namespace, set(keep)))

    monkeypatch.setattr(operator, "apply_network_policy", fake_apply)
    monkeypatch.setattr(operator, "delete_managed_policies", fake_delete)
    monkeypatch.setattr(operator, "prune_managed_policies", fake_prune)
    return calls


def set_cfg(monkeypatch, **kw):
    cfg = Config(
        baseline_allowed_namespaces=kw.get("baseline", []),
        denylist=set(kw.get("denylist", [])),
        node_cidrs=kw.get("node_cidrs", []),
        dry_run=kw.get("dry_run", False),
        allow_wildcard=kw.get("allow_wildcard", False),
        default_deny_ingress=kw.get("default_deny_ingress", True),
    )
    monkeypatch.setattr(operator, "cfg", cfg)
    return cfg


def _names(bodies):
    return [b["metadata"]["name"] for b in bodies]


def _froms(bodies):
    allow = next(b for b in bodies if b["metadata"]["name"] == NAME_ALLOW_INGRESS)
    return allow["spec"]["ingress"][0]["from"]


# --- _desired_policies --------------------------------------------------------

def test_desired_policies_order_is_allow_before_deny(monkeypatch, logger):
    set_cfg(monkeypatch)
    pols = operator._desired_policies("app", {ANNOTATION: "argocd"}, logger)
    assert _names(pols) == [NAME_ALLOW_EGRESS, NAME_ALLOW_INGRESS, NAME_DENY_INGRESS]
    assert _names(pols)[-1] == NAME_DENY_INGRESS  # deny last (fail-closed)


def test_desired_policies_self_plus_listed(monkeypatch, logger):
    set_cfg(monkeypatch)
    pols = operator._desired_policies("app", {ANNOTATION: "argocd,prometheus"}, logger)
    froms = _froms(pols)
    selectors = [f["namespaceSelector"]["matchLabels"][NS_NAME_LABEL] for f in froms]
    assert selectors[0] == "app"  # self always first
    assert "argocd" in selectors and "prometheus" in selectors


def test_desired_policies_empty_annotation_uses_baselines_only(monkeypatch, logger):
    set_cfg(monkeypatch, baseline=["prometheus", "ingress-nginx"])
    pols = operator._desired_policies("app", {ANNOTATION: ""}, logger)
    selectors = [
        f["namespaceSelector"]["matchLabels"][NS_NAME_LABEL] for f in _froms(pols)
    ]
    assert selectors == ["app", "prometheus", "ingress-nginx"]


def test_desired_policies_invalid_names_are_skipped_and_warned(monkeypatch, logger):
    set_cfg(monkeypatch)
    pols = operator._desired_policies("app", {ANNOTATION: "argocd,Bad_Name"}, logger)
    selectors = [
        f["namespaceSelector"]["matchLabels"][NS_NAME_LABEL] for f in _froms(pols)
    ]
    # parse_sources lowercases, so the invalid token is reported as "bad_name".
    assert "bad_name" not in selectors
    assert any("bad_name" in w.lower() for w in logger.warnings)


def test_desired_policies_wildcard_rejected_when_not_allowed(monkeypatch, logger):
    set_cfg(monkeypatch, allow_wildcard=False, baseline=["prometheus"])
    pols = operator._desired_policies("app", {ANNOTATION: "*"}, logger)
    froms = _froms(pols)
    # No open "all namespaces" selector; falls back to self + baseline.
    assert {"namespaceSelector": {}} not in froms
    selectors = [f["namespaceSelector"]["matchLabels"][NS_NAME_LABEL] for f in froms]
    assert selectors == ["app", "prometheus"]
    assert any("wildcard" in w.lower() for w in logger.warnings)


def test_desired_policies_wildcard_allowed_opens_all_namespaces(monkeypatch, logger):
    set_cfg(monkeypatch, allow_wildcard=True)
    pols = operator._desired_policies("app", {ANNOTATION: "*"}, logger)
    assert {"namespaceSelector": {}} in _froms(pols)


def test_desired_policies_includes_node_cidrs(monkeypatch, logger):
    set_cfg(monkeypatch, node_cidrs=["10.0.0.0/16"])
    pols = operator._desired_policies("app", {ANNOTATION: "argocd"}, logger)
    assert {"ipBlock": {"cidr": "10.0.0.0/16"}} in _froms(pols)


def test_desired_policies_omits_deny_when_disabled(monkeypatch, logger):
    set_cfg(monkeypatch, default_deny_ingress=False)
    pols = operator._desired_policies("app", {ANNOTATION: "argocd"}, logger)
    assert _names(pols) == [NAME_ALLOW_EGRESS, NAME_ALLOW_INGRESS]
    assert NAME_DENY_INGRESS not in _names(pols)


def test_desired_policies_omits_deny_when_disabled_wildcard(monkeypatch, logger):
    set_cfg(monkeypatch, default_deny_ingress=False, allow_wildcard=True)
    pols = operator._desired_policies("app", {ANNOTATION: "*"}, logger)
    assert NAME_DENY_INGRESS not in _names(pols)
    assert {"namespaceSelector": {}} in _froms(pols)


# --- prefix patterns (e.g. "ihg-*") --------------------------------------------

def set_cluster_namespaces(monkeypatch, namespaces: dict[str, dict]):
    """Fake the cluster namespace listing: {name: annotations}."""
    monkeypatch.setattr(
        operator,
        "list_namespaces_with_annotations",
        lambda: list(namespaces.items()),
    )


def test_desired_policies_expands_prefix_pattern(monkeypatch, logger):
    set_cfg(monkeypatch)
    set_cluster_namespaces(
        monkeypatch, {"ihg-a": {}, "ihg-b": {}, "other": {}, "app": {}}
    )
    pols = operator._desired_policies("app", {ANNOTATION: "ihg-*"}, logger)
    selectors = [
        f["namespaceSelector"]["matchLabels"][NS_NAME_LABEL] for f in _froms(pols)
    ]
    assert selectors == ["app", "ihg-a", "ihg-b"]  # self first, then matches
    assert "other" not in selectors


def test_desired_policies_pattern_combines_with_plain_names_and_baseline(monkeypatch, logger):
    set_cfg(monkeypatch, baseline=["prometheus"])
    set_cluster_namespaces(monkeypatch, {"ihg-a": {}, "app": {}})
    pols = operator._desired_policies("app", {ANNOTATION: "argocd,ihg-*"}, logger)
    selectors = [
        f["namespaceSelector"]["matchLabels"][NS_NAME_LABEL] for f in _froms(pols)
    ]
    assert selectors == ["app", "argocd", "ihg-a", "prometheus"]


def test_desired_policies_pattern_never_selects_self(monkeypatch, logger):
    set_cfg(monkeypatch)
    set_cluster_namespaces(monkeypatch, {"ihg-a": {}, "ihg-app": {}})
    pols = operator._desired_policies("ihg-app", {ANNOTATION: "ihg-*"}, logger)
    selectors = [
        f["namespaceSelector"]["matchLabels"][NS_NAME_LABEL] for f in _froms(pols)
    ]
    # self appears once (as the leading self rule), not duplicated by the pattern
    assert selectors == ["ihg-app", "ihg-a"]


def test_desired_policies_invalid_pattern_is_skipped_and_warned(monkeypatch, logger):
    set_cfg(monkeypatch)
    set_cluster_namespaces(monkeypatch, {"ihg-a": {}})
    pols = operator._desired_policies("app", {ANNOTATION: "*ihg,argocd"}, logger)
    selectors = [
        f["namespaceSelector"]["matchLabels"][NS_NAME_LABEL] for f in _froms(pols)
    ]
    assert selectors == ["app", "argocd"]
    assert any("pattern" in w.lower() for w in logger.warnings)


def test_new_namespace_triggers_reconcile_of_pattern_declarers(monkeypatch, applied, logger):
    set_cfg(monkeypatch, dry_run=False)
    set_cluster_namespaces(
        monkeypatch,
        {
            "ihg-new": {},                     # the namespace that was just created
            "app": {ANNOTATION: "ihg-*"},      # declares the pattern -> must refresh
            "app2": {ANNOTATION: "argocd"},    # no pattern -> untouched
            "plain": {},                       # unmanaged -> untouched
        },
    )
    operator.on_namespace_created_refresh_patterns("ihg-new", logger)
    applied_namespaces = {b["metadata"]["namespace"] for b in applied["applied"]}
    assert applied_namespaces == {"app"}
    # the refreshed policy now allows the new namespace
    allow = next(
        b for b in applied["applied"] if b["metadata"]["name"] == NAME_ALLOW_INGRESS
    )
    froms = allow["spec"]["ingress"][0]["from"]
    selectors = [f["namespaceSelector"]["matchLabels"][NS_NAME_LABEL] for f in froms]
    assert "ihg-new" in selectors


def test_new_namespace_without_pattern_match_triggers_nothing(monkeypatch, applied, logger):
    set_cfg(monkeypatch, dry_run=False)
    set_cluster_namespaces(monkeypatch, {"app": {ANNOTATION: "ihg-*"}})
    operator.on_namespace_created_refresh_patterns("unrelated", logger)
    assert applied["applied"] == []


# --- reconcile ----------------------------------------------------------------

def test_reconcile_applies_three_policies_in_order(monkeypatch, applied, logger):
    set_cfg(monkeypatch, dry_run=False)
    operator.reconcile("app", {"annotations": {ANNOTATION: "argocd"}}, logger)
    assert _names(applied["applied"]) == [
        NAME_ALLOW_EGRESS,
        NAME_ALLOW_INGRESS,
        NAME_DENY_INGRESS,
    ]
    assert applied["deleted"] == []


def test_reconcile_prunes_to_desired_set(monkeypatch, applied, logger):
    set_cfg(monkeypatch, dry_run=False)
    operator.reconcile("app", {"annotations": {ANNOTATION: "argocd"}}, logger)
    # prune is called once with exactly the three desired names to keep.
    assert applied["pruned"] == [
        ("app", {NAME_ALLOW_EGRESS, NAME_ALLOW_INGRESS, NAME_DENY_INGRESS})
    ]


def test_reconcile_with_deny_disabled_applies_two_and_prunes_deny(monkeypatch, applied, logger):
    set_cfg(monkeypatch, dry_run=False, default_deny_ingress=False)
    operator.reconcile("app", {"annotations": {ANNOTATION: "argocd"}}, logger)
    assert _names(applied["applied"]) == [NAME_ALLOW_EGRESS, NAME_ALLOW_INGRESS]
    # the keep-set excludes default-deny, so any existing deny policy gets pruned.
    ns, keep = applied["pruned"][0]
    assert keep == {NAME_ALLOW_EGRESS, NAME_ALLOW_INGRESS}
    assert NAME_DENY_INGRESS not in keep


def test_reconcile_dry_run_does_not_apply(monkeypatch, applied, logger):
    set_cfg(monkeypatch, dry_run=True)
    operator.reconcile("app", {"annotations": {ANNOTATION: "argocd"}}, logger)
    assert applied["applied"] == []
    assert any("would apply" in m for m in logger.infos)


def test_reconcile_no_annotation_cleans_up(monkeypatch, applied, logger):
    set_cfg(monkeypatch, dry_run=False)
    operator.reconcile("app", {"annotations": {}}, logger, allow_cleanup=True)
    assert applied["applied"] == []
    assert applied["deleted"] == ["app"]


def test_reconcile_no_annotation_skips_cleanup_when_disabled(monkeypatch, applied, logger):
    set_cfg(monkeypatch, dry_run=False)
    operator.reconcile("app", {"annotations": {}}, logger, allow_cleanup=False)
    assert applied["deleted"] == []


def test_reconcile_system_namespace_is_never_touched(monkeypatch, applied, logger):
    set_cfg(monkeypatch, dry_run=False)
    operator.reconcile("kube-system", {"annotations": {ANNOTATION: "argocd"}}, logger)
    assert applied["applied"] == []
    assert applied["deleted"] == []


def test_reconcile_denylisted_namespace_is_never_touched(monkeypatch, applied, logger):
    set_cfg(monkeypatch, dry_run=False, denylist=["tfy-system"])
    operator.reconcile("tfy-system", {"annotations": {ANNOTATION: "argocd"}}, logger)
    assert applied["applied"] == []
    assert applied["deleted"] == []


def test_reconcile_handles_missing_meta(monkeypatch, applied, logger):
    set_cfg(monkeypatch, dry_run=False)
    # No annotation present -> cleanup path, must not raise on None meta.
    operator.reconcile("app", None, logger, allow_cleanup=True)
    assert applied["deleted"] == ["app"]
