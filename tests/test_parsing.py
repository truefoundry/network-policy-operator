from tfy_netpol_operator.parsing import (
    expand_patterns,
    is_pattern,
    is_valid_namespace_name,
    is_valid_pattern,
    merge_sources,
    parse_sources,
    partition_patterns,
    pattern_matches,
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


# --- prefix patterns (e.g. "ihg-*") --------------------------------------------

def test_is_pattern_distinguishes_patterns_from_names_and_bare_wildcard():
    assert is_pattern("ihg-*")
    assert is_pattern("a*")
    assert is_pattern("*ihg")  # malformed, but routed to pattern validation
    assert not is_pattern("*")  # bare wildcard handled separately
    assert not is_pattern("argocd")


def test_is_valid_pattern():
    assert is_valid_pattern("ihg-*")
    assert is_valid_pattern("ihg*")
    assert not is_valid_pattern("*ihg")  # star must be trailing
    assert not is_valid_pattern("-ihg*")  # must start alphanumeric
    assert not is_valid_pattern("ihg-*-x*")  # single trailing star only
    assert not is_valid_pattern("a" * 63 + "*")  # too long


def test_partition_patterns_preserves_order():
    plain, patterns = partition_patterns(["argocd", "ihg-*", "prometheus", "team-*"])
    assert plain == ["argocd", "prometheus"]
    assert patterns == ["ihg-*", "team-*"]


def test_pattern_matches_prefix():
    assert pattern_matches("ihg-*", "ihg-app")
    assert pattern_matches("ihg-*", "ihg-")
    assert not pattern_matches("ihg-*", "ihg")
    assert not pattern_matches("ihg-*", "other")


def test_expand_patterns_matches_and_dedupes():
    namespaces = ["ihg-a", "ihg-b", "other", "team-x"]
    assert expand_patterns(["ihg-*"], namespaces) == ["ihg-a", "ihg-b"]
    # union across patterns, de-duplicated, pattern order first
    assert expand_patterns(["team-*", "ihg-*", "ihg-*"], namespaces) == [
        "team-x",
        "ihg-a",
        "ihg-b",
    ]


def test_expand_patterns_no_match_is_empty():
    assert expand_patterns(["ihg-*"], ["other", "argocd"]) == []
