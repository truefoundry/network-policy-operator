#!/usr/bin/env bash
#
# test-networkpolicies.sh — end-to-end test harness for tfy-netpol-operator.
#
# Validates the operator across three layers and prints a results table:
#
#   PHASE 0  Preflight        cluster reachable, operator healthy, dry-run + CNI state
#   PHASE 1  Unit logic       runs pytest (policy builders + reconcile decisions)
#   PHASE 2  Reconcile (live) operator creates/updates/deletes the right policies
#   PHASE 3  Enforcement      real ingress is allowed/blocked per policy (needs a
#                             NetworkPolicy-enforcing CNI, e.g. AWS VPC CNI w/ network
#                             policy, Calico, Cilium)
#
# The script is adaptive: if the operator is in dryRun=true it skips the live phases
# (no policies are created), and if the CNI does not enforce NetworkPolicies it skips
# the enforcement probes — each with a clear SKIP + reason rather than a false PASS.
#
# It only creates clearly-named, ephemeral namespaces (default prefix "netpol-e2e-")
# and deletes them on exit. It never mutates the operator, its config, or kube-system.
#
# Usage:
#   scripts/test-networkpolicies.sh [--keep] [--no-unit] [--no-enforcement]
#
# Environment overrides:
#   OPERATOR_NS        namespace of the operator        (default: tfy-system)
#   OPERATOR_DEPLOY    operator deployment name         (default: tfy-netpol-operator)
#   OPERATOR_CONFIGMAP operator config map name         (default: <deploy>-config)
#   NS_PREFIX          test namespace prefix            (default: netpol-e2e)
#   TARGET_IMAGE       HTTP server image for the target (default: nginx:1.27-alpine)
#   CLIENT_IMAGE       curl client image                (default: curlimages/curl:8.10.1)
#   RECONCILE_TIMEOUT  seconds to wait for the operator (default: 60)
#   PROBE_TIMEOUT      seconds for each curl probe      (default: 6)
#
set -uo pipefail

# ----------------------------------------------------------------------------- config
OPERATOR_NS="${OPERATOR_NS:-tfy-system}"
OPERATOR_DEPLOY="${OPERATOR_DEPLOY:-tfy-netpol-operator}"
OPERATOR_CONFIGMAP="${OPERATOR_CONFIGMAP:-${OPERATOR_DEPLOY}-config}"
NS_PREFIX="${NS_PREFIX:-netpol-e2e}"
TARGET_IMAGE="${TARGET_IMAGE:-nginx:1.27-alpine}"
CLIENT_IMAGE="${CLIENT_IMAGE:-curlimages/curl:8.10.1}"
RECONCILE_TIMEOUT="${RECONCILE_TIMEOUT:-60}"
PROBE_TIMEOUT="${PROBE_TIMEOUT:-6}"
ANNOTATION="truefoundry.com/allowed-ingress-namespaces"

# operator-managed policy names (must match src/tfy_netpol_operator/policies.py)
NP_EGRESS="tfy-np-allow-egress"
NP_INGRESS="tfy-np-allow-ingress"
NP_DENY="tfy-np-default-deny-ingress"
MANAGED_SELECTOR="app.kubernetes.io/managed-by=tfy-netpol-operator"

KEEP=false
RUN_UNIT=true
RUN_ENFORCEMENT=true

# unique suffix so parallel/re-runs don't collide
SUFFIX="$(date +%s | tail -c 6)$$"
NS_TARGET="${NS_PREFIX}-target-${SUFFIX}"
NS_ALLOWED="${NS_PREFIX}-allowed-${SUFFIX}"
NS_BLOCKED="${NS_PREFIX}-blocked-${SUFFIX}"
SVC="echo-target"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ----------------------------------------------------------------------------- colors
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
else
  C_RESET=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_DIM=""
fi

# ----------------------------------------------------------------------------- results
declare -a R_NAME R_STATUS R_DETAIL
PASS=0; FAIL=0; SKIP=0

record() { # <name> <PASS|FAIL|SKIP> <detail>
  R_NAME+=("$1"); R_STATUS+=("$2"); R_DETAIL+=("$3")
  case "$2" in
    PASS) PASS=$((PASS+1)); printf "  %sv PASS%s  %-42s %s\n" "$C_GREEN" "$C_RESET" "$1" "${C_DIM}$3${C_RESET}" ;;
    FAIL) FAIL=$((FAIL+1)); printf "  %sx FAIL%s  %-42s %s\n" "$C_RED" "$C_RESET" "$1" "${C_RED}$3${C_RESET}" ;;
    SKIP) SKIP=$((SKIP+1)); printf "  %s- SKIP%s  %-42s %s\n" "$C_YELLOW" "$C_RESET" "$1" "${C_DIM}$3${C_RESET}" ;;
  esac
}

phase() { printf "\n%s%s== %s ==%s\n" "$C_BOLD" "$C_BLUE" "$1" "$C_RESET"; }
info()  { printf "%s%s%s\n" "$C_DIM" "$1" "$C_RESET"; }
die()   { printf "%s%sFATAL:%s %s\n" "$C_BOLD" "$C_RED" "$C_RESET" "$1" >&2; exit 2; }

# assert helper: PASS if condition (eval'd) is true, else FAIL
assert() { # <name> <detail-on-fail> <command...>
  local name="$1" detail="$2"; shift 2
  if "$@" >/dev/null 2>&1; then record "$name" PASS ""; else record "$name" FAIL "$detail"; fi
}

# ----------------------------------------------------------------------------- args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep) KEEP=true ;;
    --no-unit) RUN_UNIT=false ;;
    --no-enforcement) RUN_ENFORCEMENT=false ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

# ----------------------------------------------------------------------------- cleanup
cleanup() {
  if $KEEP; then
    info "--keep set; leaving namespaces ${NS_TARGET}, ${NS_ALLOWED}, ${NS_BLOCKED}"
    return
  fi
  info "Cleaning up test namespaces..."
  kubectl delete ns "$NS_TARGET" "$NS_ALLOWED" "$NS_BLOCKED" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ----------------------------------------------------------------------------- helpers
np_exists() { kubectl get networkpolicy "$1" -n "$2" >/dev/null 2>&1; }

managed_np_count() {
  kubectl get networkpolicy -n "$1" -l "$MANAGED_SELECTOR" \
    -o jsonpath='{.items[*].metadata.name}' 2>/dev/null | wc -w | tr -d ' '
}

# wait until the managed-policy count in $1 equals $2 (or timeout)
wait_for_np_count() { # <ns> <expected> 
  local ns="$1" want="$2" deadline=$(( SECONDS + RECONCILE_TIMEOUT ))
  while (( SECONDS < deadline )); do
    [[ "$(managed_np_count "$ns")" == "$want" ]] && return 0
    sleep 2
  done
  return 1
}

# does the allow-ingress policy permit the given namespace? (matchLabels selector)
ingress_allows_ns() { # <target-ns> <source-ns>
  kubectl get networkpolicy "$NP_INGRESS" -n "$1" -o json 2>/dev/null \
    | grep -q "\"kubernetes.io/metadata.name\": *\"$2\""
}

# run a curl probe from a pod in <ns> to the target service; echo the container exit code
probe() { # <from-ns>  ->  prints exit code (0 reachable; non-zero blocked/timeout)
  local from="$1" pod="probe-${SUFFIX}-$RANDOM"
  local url="http://${SVC}.${NS_TARGET}.svc.cluster.local"
  kubectl run "$pod" -n "$from" --image="$CLIENT_IMAGE" --restart=Never \
    --command -- sh -c "curl -sS -m ${PROBE_TIMEOUT} -o /dev/null ${url}" >/dev/null 2>&1
  # wait for the pod to terminate
  local deadline=$(( SECONDS + PROBE_TIMEOUT + 25 )) phase
  while (( SECONDS < deadline )); do
    phase="$(kubectl get pod "$pod" -n "$from" -o jsonpath='{.status.phase}' 2>/dev/null)"
    [[ "$phase" == "Succeeded" || "$phase" == "Failed" ]] && break
    sleep 1
  done
  local code
  code="$(kubectl get pod "$pod" -n "$from" \
    -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}' 2>/dev/null)"
  kubectl delete pod "$pod" -n "$from" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  echo "${code:-1}"
}

# ============================================================================= PHASE 0
phase "PHASE 0 — Preflight"
command -v kubectl >/dev/null 2>&1 || die "kubectl not found in PATH"
kubectl cluster-info >/dev/null 2>&1 || die "cannot reach the cluster (check kubeconfig/context)"
info "Context: $(kubectl config current-context 2>/dev/null)"

# operator deployment present + available
if kubectl get deploy "$OPERATOR_DEPLOY" -n "$OPERATOR_NS" >/dev/null 2>&1; then
  avail="$(kubectl get deploy "$OPERATOR_DEPLOY" -n "$OPERATOR_NS" \
    -o jsonpath='{.status.availableReplicas}' 2>/dev/null)"
  if [[ "${avail:-0}" -ge 1 ]]; then
    record "operator deployment available" PASS "${avail} replica(s) ready"
  else
    record "operator deployment available" FAIL "0 available replicas (ImagePull/crash?)"
  fi
else
  record "operator deployment available" FAIL "deploy/${OPERATOR_DEPLOY} not found in ${OPERATOR_NS}"
fi

# read dryRun from the operator's ConfigMap
DRY_RUN="unknown"
cfg="$(kubectl get configmap "$OPERATOR_CONFIGMAP" -n "$OPERATOR_NS" \
  -o jsonpath='{.data.config\.yaml}' 2>/dev/null)"
if [[ -n "$cfg" ]]; then
  if grep -qiE '^\s*dryRun:\s*false\s*$' <<<"$cfg"; then DRY_RUN="false"; fi
  if grep -qiE '^\s*dryRun:\s*true\s*$'  <<<"$cfg"; then DRY_RUN="true";  fi
fi
info "Operator dryRun = ${DRY_RUN}"

# detect a NetworkPolicy-enforcing CNI
CNI_ENFORCES=false; CNI_NOTE="no enforcing CNI detected"
if kubectl -n kube-system get ds aws-node \
     -o jsonpath='{.spec.template.spec.containers[*].name}' 2>/dev/null \
     | grep -q "aws-network-policy-agent"; then
  CNI_ENFORCES=true; CNI_NOTE="AWS VPC CNI network-policy-agent present"
elif kubectl get pods -n kube-system 2>/dev/null | grep -qiE 'calico|cilium'; then
  CNI_ENFORCES=true; CNI_NOTE="Calico/Cilium detected"
fi
info "CNI enforcement: ${CNI_ENFORCES} (${CNI_NOTE})"

# ============================================================================= PHASE 1
if $RUN_UNIT; then
  phase "PHASE 1 — Unit logic (pytest)"
  PYTEST_BIN=""
  if [[ -x "${REPO_ROOT}/.venv/bin/pytest" ]]; then PYTEST_BIN="${REPO_ROOT}/.venv/bin/pytest"
  elif command -v pytest >/dev/null 2>&1; then PYTEST_BIN="pytest"; fi
  if [[ -n "$PYTEST_BIN" ]]; then
    if ( cd "$REPO_ROOT" && "$PYTEST_BIN" -q >/tmp/np_pytest.log 2>&1 ); then
      record "pytest (parsing/policies/reconcile)" PASS "$(grep -Eo '[0-9]+ passed' /tmp/np_pytest.log | tail -1)"
    else
      record "pytest (parsing/policies/reconcile)" FAIL "see /tmp/np_pytest.log"
    fi
  else
    record "pytest (parsing/policies/reconcile)" SKIP "pytest not installed (create .venv)"
  fi
fi

# ============================================================================= PHASE 2
phase "PHASE 2 — Reconcile correctness (live cluster)"
if [[ "$DRY_RUN" == "true" ]]; then
  record "live reconcile tests" SKIP "operator is dryRun=true; set config.dryRun=false to run"
else
  info "Creating test namespaces..."
  for ns in "$NS_TARGET" "$NS_ALLOWED" "$NS_BLOCKED"; do
    kubectl create ns "$ns" >/dev/null 2>&1 || true
  done

  # deploy an HTTP target in the protected namespace
  kubectl -n "$NS_TARGET" create deployment "$SVC" --image="$TARGET_IMAGE" --port=80 >/dev/null 2>&1 || true
  kubectl -n "$NS_TARGET" expose deployment "$SVC" --port=80 --target-port=80 >/dev/null 2>&1 || true

  # TC: enroll target with the allowed namespace listed
  kubectl annotate ns "$NS_TARGET" "${ANNOTATION}=${NS_ALLOWED}" --overwrite >/dev/null 2>&1

  if wait_for_np_count "$NS_TARGET" 3; then
    record "3 managed policies created on enroll" PASS "egress + allow-ingress + deny"
  else
    record "3 managed policies created on enroll" FAIL \
      "expected 3, got $(managed_np_count "$NS_TARGET") within ${RECONCILE_TIMEOUT}s"
  fi

  assert "policy: ${NP_EGRESS} exists"  "missing allow-egress"  np_exists "$NP_EGRESS"  "$NS_TARGET"
  assert "policy: ${NP_INGRESS} exists" "missing allow-ingress" np_exists "$NP_INGRESS" "$NS_TARGET"
  assert "policy: ${NP_DENY} exists"    "missing default-deny"  np_exists "$NP_DENY"    "$NS_TARGET"

  # default-deny spec is empty podSelector + Ingress only
  deny_spec="$(kubectl get networkpolicy "$NP_DENY" -n "$NS_TARGET" -o jsonpath='{.spec.policyTypes[*]}' 2>/dev/null)"
  if [[ "$deny_spec" == "Ingress" ]]; then
    record "default-deny is Ingress-only" PASS ""
  else
    record "default-deny is Ingress-only" FAIL "policyTypes='${deny_spec}'"
  fi

  # allow-ingress includes self + the listed (allowed) namespace
  assert "allow-ingress permits self (${NS_TARGET})" "self selector missing" \
    ingress_allows_ns "$NS_TARGET" "$NS_TARGET"
  assert "allow-ingress permits listed (${NS_ALLOWED})" "allowed-ns selector missing" \
    ingress_allows_ns "$NS_TARGET" "$NS_ALLOWED"

  # blocked namespace must NOT be in the allow list
  if ingress_allows_ns "$NS_TARGET" "$NS_BLOCKED"; then
    record "allow-ingress excludes non-listed ns" FAIL "${NS_BLOCKED} unexpectedly allowed"
  else
    record "allow-ingress excludes non-listed ns" PASS ""
  fi

  # managed-by label present on all three
  labelled="$(kubectl get networkpolicy -n "$NS_TARGET" -l "$MANAGED_SELECTOR" \
    -o jsonpath='{.items[*].metadata.name}' 2>/dev/null | wc -w | tr -d ' ')"
  if [[ "$labelled" == "3" ]]; then
    record "managed-by label on all policies" PASS ""
  else
    record "managed-by label on all policies" FAIL "${labelled}/3 labelled"
  fi

  # TC: empty annotation -> still 3 policies, but allowed-ns no longer permitted
  kubectl annotate ns "$NS_TARGET" "${ANNOTATION}=" --overwrite >/dev/null 2>&1
  sleep 5
  if ingress_allows_ns "$NS_TARGET" "$NS_ALLOWED"; then
    record "empty annotation drops listed ns" FAIL "${NS_ALLOWED} still allowed"
  else
    record "empty annotation drops listed ns" PASS "self/baselines only"
  fi
  [[ "$(managed_np_count "$NS_TARGET")" == "3" ]] \
    && record "empty annotation keeps 3 policies" PASS "" \
    || record "empty annotation keeps 3 policies" FAIL "count=$(managed_np_count "$NS_TARGET")"

  # restore enrollment for the enforcement phase
  kubectl annotate ns "$NS_TARGET" "${ANNOTATION}=${NS_ALLOWED}" --overwrite >/dev/null 2>&1
  wait_for_np_count "$NS_TARGET" 3 >/dev/null 2>&1

  # TC: removing the annotation cleans everything up
  kubectl annotate ns "$NS_ALLOWED" "${ANNOTATION}=foo" --overwrite >/dev/null 2>&1
  wait_for_np_count "$NS_ALLOWED" 3 >/dev/null 2>&1
  kubectl annotate ns "$NS_ALLOWED" "${ANNOTATION}-" >/dev/null 2>&1
  if wait_for_np_count "$NS_ALLOWED" 0; then
    record "annotation removal deletes policies" PASS "cleanup verified on ${NS_ALLOWED}"
  else
    record "annotation removal deletes policies" FAIL \
      "still $(managed_np_count "$NS_ALLOWED") policies after ${RECONCILE_TIMEOUT}s"
  fi
fi

# ============================================================================= PHASE 3
phase "PHASE 3 — Enforcement (connectivity probes)"
if [[ "$DRY_RUN" == "true" ]]; then
  record "enforcement probes" SKIP "operator is dryRun=true; nothing is enforced"
elif ! $RUN_ENFORCEMENT; then
  record "enforcement probes" SKIP "--no-enforcement set"
elif ! $CNI_ENFORCES; then
  record "enforcement probes" SKIP "CNI does not enforce NetworkPolicies (${CNI_NOTE})"
else
  info "Waiting for target service to be ready..."
  kubectl -n "$NS_TARGET" rollout status deploy/"$SVC" --timeout=90s >/dev/null 2>&1 || true

  # ensure target is enrolled allowing $NS_ALLOWED
  kubectl annotate ns "$NS_TARGET" "${ANNOTATION}=${NS_ALLOWED}" --overwrite >/dev/null 2>&1
  wait_for_np_count "$NS_TARGET" 3 >/dev/null 2>&1
  sleep 5  # give the CNI agent time to program the policy

  code_self="$(probe "$NS_TARGET")"
  [[ "$code_self" == "0" ]] \
    && record "ingress from self → ALLOWED" PASS "" \
    || record "ingress from self → ALLOWED" FAIL "curl exit ${code_self} (expected reachable)"

  code_allow="$(probe "$NS_ALLOWED")"
  [[ "$code_allow" == "0" ]] \
    && record "ingress from listed ns → ALLOWED" PASS "from ${NS_ALLOWED}" \
    || record "ingress from listed ns → ALLOWED" FAIL "curl exit ${code_allow} (expected reachable)"

  code_block="$(probe "$NS_BLOCKED")"
  if [[ "$code_block" != "0" ]]; then
    record "ingress from non-listed ns → BLOCKED" PASS "from ${NS_BLOCKED} (curl exit ${code_block})"
  else
    record "ingress from non-listed ns → BLOCKED" FAIL "reachable — policy not enforced"
  fi
fi

# ============================================================================= SUMMARY
phase "SUMMARY"
TOTAL=$((PASS+FAIL+SKIP))
printf "%s%-46s %s%s\n" "$C_BOLD" "Test" "Result" "$C_RESET"
printf "%s\n" "------------------------------------------------------------"
for i in "${!R_NAME[@]}"; do
  case "${R_STATUS[$i]}" in
    PASS) col="$C_GREEN" ;; FAIL) col="$C_RED" ;; *) col="$C_YELLOW" ;;
  esac
  printf "%-46s %s%-5s%s %s\n" "${R_NAME[$i]}" "$col" "${R_STATUS[$i]}" "$C_RESET" "${C_DIM}${R_DETAIL[$i]}${C_RESET}"
done
printf "%s\n" "------------------------------------------------------------"
printf "%sTotal: %d   %s%d passed%s   %s%d failed%s   %s%d skipped%s\n" \
  "$C_BOLD" "$TOTAL" \
  "$C_GREEN" "$PASS" "$C_RESET" \
  "$C_RED" "$FAIL" "$C_RESET" \
  "$C_YELLOW" "$SKIP" "$C_RESET"

[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
