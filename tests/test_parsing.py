from tfy_netpol_operator.parsing import (
    is_valid_namespace_name,
    merge_sources,
    parse_sources,
    split_valid,
)


def test_parse_sources_basic():
    assert parse_sources("argocd,prometheus") == ["argocd", "prometheus"]


def test_parse_sources_trims_and_lowercases():
    assert parse_sources("  ArgoCD , Prometheus ") == ["argocd", "prometheus"]


def test_parse_sources_dedupes_preserving_order():
    assert parse_sources("a,b,a,c,b") == ["a", "b", "c"]


def test_parse_sources_drops_self_and_blanks():
    assert parse_sources("self, ,argocd", self_name="truefoundry") == ["argocd"]
    assert parse_sources("truefoundry,argocd", self_name="truefoundry") == ["argocd"]


def test_parse_sources_empty():
    assert parse_sources("") == []
    assert parse_sources(None) == []


def test_namespace_name_validation():
    assert is_valid_namespace_name("argocd")
    assert is_valid_namespace_name("tfy-agent")
    assert not is_valid_namespace_name("Bad_Name")
    assert not is_valid_namespace_name("-leading")
    assert not is_valid_namespace_name("trailing-")
    assert not is_valid_namespace_name("a" * 64)


def test_split_valid():
    valid, invalid = split_valid(["argocd", "Bad_Name", "prometheus"])
    assert valid == ["argocd", "prometheus"]
    assert invalid == ["Bad_Name"]


def test_merge_sources_injects_baselines_and_dedupes():
    merged = merge_sources(
        user=["argocd"],
        baseline=["prometheus", "argocd", "tfy-agent"],
        self_name="truefoundry",
    )
    assert merged == ["argocd", "prometheus", "tfy-agent"]


def test_merge_sources_drops_self():
    merged = merge_sources(user=["truefoundry", "argocd"], baseline=[], self_name="truefoundry")
    assert merged == ["argocd"]
