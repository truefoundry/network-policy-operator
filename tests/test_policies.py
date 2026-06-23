from tfy_netpol_operator.policies import (
    MANAGED_LABELS,
    NAME_ALLOW_EGRESS,
    NAME_ALLOW_INGRESS,
    NAME_DENY_INGRESS,
    NS_NAME_LABEL,
    build_allow_all_egress,
    build_allow_ingress,
    build_default_deny_ingress,
    build_policy_set,
)


def test_default_deny_ingress():
    np = build_default_deny_ingress("truefoundry")
    assert np["spec"] == {"podSelector": {}, "policyTypes": ["Ingress"]}
    assert np["metadata"]["name"] == NAME_DENY_INGRESS
    assert np["metadata"]["labels"] == MANAGED_LABELS


def test_allow_all_egress():
    np = build_allow_all_egress("truefoundry")
    assert np["spec"] == {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]}
    assert np["metadata"]["name"] == NAME_ALLOW_EGRESS


def test_allow_ingress_self_first_then_sources():
    np = build_allow_ingress("truefoundry", ["argocd", "prometheus"])
    froms = np["spec"]["ingress"][0]["from"]
    assert froms[0] == {"namespaceSelector": {"matchLabels": {NS_NAME_LABEL: "truefoundry"}}}
    assert froms[1] == {"namespaceSelector": {"matchLabels": {NS_NAME_LABEL: "argocd"}}}
    assert froms[2] == {"namespaceSelector": {"matchLabels": {NS_NAME_LABEL: "prometheus"}}}
    assert np["metadata"]["name"] == NAME_ALLOW_INGRESS


def test_allow_ingress_with_node_cidrs():
    np = build_allow_ingress("ns", ["argocd"], node_cidrs=["10.0.0.0/16"])
    froms = np["spec"]["ingress"][0]["from"]
    assert froms[-1] == {"ipBlock": {"cidr": "10.0.0.0/16"}}


def test_allow_ingress_wildcard_all_namespaces():
    np = build_allow_ingress("ns", [], allow_all_namespaces=True)
    assert np["spec"]["ingress"][0]["from"] == [{"namespaceSelector": {}}]


def test_policy_set_order_is_allow_before_deny():
    policies = build_policy_set("truefoundry", ["argocd"])
    names = [p["metadata"]["name"] for p in policies]
    # egress, allow-ingress, then deny last
    assert names == [NAME_ALLOW_EGRESS, NAME_ALLOW_INGRESS, NAME_DENY_INGRESS]
    assert names[-1] == NAME_DENY_INGRESS
