# Wildcard annotation test report — `tfy-netpol-operator` 0.6.0

Live verification of prefix-wildcard support (`ihs-*`) in the
`truefoundry.com/allowed-ingress-namespaces` annotation.

| | |
|---|---|
| Cluster | EKS `ujjwal-test-gpu-attach` (us-east-1) |
| Operator image | `tfy-netpol-operator:0.6.0` (ECR), `dryRun: false` |
| Baselines | `istio-system`, `tfy-agent` |
| CNI | AWS VPC CNI, network policy enforcement enabled mid-test (see finding) |
| Date | 24 Jul 2026, 11:40–12:45 IST |
| Result | **15 / 15 passed** · 0 operator bugs · 1 cluster finding (resolved) |

## Results

| ID | Scenario | Action / annotation | Expected | Observed | Result |
|----|----------|---------------------|----------|----------|--------|
| T1 | Pattern expansion | Annotate `ns3` with `ihs-*` | 3 policies; allow-ingress = self + `ihs-1..10` + baselines | Exactly as expected, self listed first | PASS |
| T2 | New matching namespace | Create `ihs-11` | Added to every pattern-declaring namespace immediately | In `ns3` and `tfy-agent` policies within 10 s (on-create handler, not resync) | PASS |
| T3 | Non-matching namespace | Create `nomatch-test` | Not added to any policy | Absent; `ihs-` source count unchanged | PASS |
| T4 | Matching namespace deleted | Delete `ihs-11` | Pruned on next resync (≤ 300 s) | Deleted 11:43:14, gone by 11:47:47 | PASS |
| T5 | Mixed plain + pattern | `argocd,ihs-*` | Plain name and all pattern matches included | self, `argocd`, `ihs-1..11`, baselines | PASS |
| T6 | Invalid pattern | `*ihs,argocd` | `*ihs` skipped with warning; valid tokens applied | Warning "skipping invalid namespace patterns ['*ihs']"; `argocd` kept | PASS |
| T7 | Bare `*` (`allowWildcard: false`) | `*` | Rejected; self + baselines | Warning "wildcard '*' is rejected by policy"; no open selector | PASS |
| T8 | Pattern matching nothing | `zzz-*` | Self + baselines only | Self + baselines only | PASS |
| T9 | Self-exclusion | Annotate `ihs-1` with `ihs-*` | Own namespace listed once | `ihs-1` first (self rule), then `ihs-2..10`; no duplicate | PASS |
| T10 | Empty annotation | `""` | Deny + egress + allow self + baselines | Self + `istio-system` + `tfy-agent` only | PASS |
| T11 | Annotation removed | Remove annotation | All managed policies deleted | 0 policies remain | PASS |
| T12 | Traffic: allowed source | curl `ns3` nginx from `ihs-2` pod | HTTP 200 | HTTP 200 | PASS |
| T13 | Traffic: blocked source | curl `ns3` nginx from `nomatch-test` pod | Blocked (timeout) | First run failed open (CNI enforcement disabled); re-run after enablement: timeout after 8 s — blocked | PASS |
| T14 | Traffic: same namespace | curl `ns3` nginx from another `ns3` pod | HTTP 200 (self rule) | HTTP 200 | PASS |
| T15 | Traffic: fresh matching namespace | Create `ihs-12`, curl ~12 s later | HTTP 200 | HTTP 200 — end-to-end wildcard pickup verified at traffic level | PASS |

## Cluster finding (resolved)

The first traffic run was not blocked from a non-allowed namespace: the AWS VPC
CNI node agent was running with `--enable-network-policy=false`
(`enable-network-policy-controller: "false"` in the `amazon-vpc-cni`
ConfigMap), so NetworkPolicies were accepted but not enforced and no
`PolicyEndpoints` existed. After enabling network policy on the `vpc-cni`
addon (`enableNetworkPolicy: "true"`), `PolicyEndpoints` were generated for
every operator-managed policy and the full traffic matrix passed. This was
cluster configuration, not an operator defect — but it is a deployment
prerequisite worth checking on every cluster (see README).

## Timing characteristics observed

| Event | Mechanism | Observed latency |
|-------|-----------|------------------|
| Annotation added/changed | Namespace update event | < 8 s |
| New namespace matching a pattern | `on_namespace_created_refresh_patterns` handler | < 10 s |
| Matched namespace deleted | Periodic resync timer (300 s) | ~4.5 min (stale selector harmless meanwhile) |

## Key evidence

Expanded allow-ingress sources in `ns3` (T1):

```
ns3            <- self, always first
ihs-1 … ihs-10 <- expanded from ihs-*
istio-system   <- baseline
tfy-agent      <- baseline
```

Operator warnings (T6/T7):

```
[WARNING] [wctest] namespace wctest: skipping invalid namespace patterns ['*ihs']
[WARNING] [wctest] namespace wctest: wildcard '*' is rejected by policy; applying baseline + self only
[INFO]    [ihs-11] Handler 'on_namespace_created_refresh_patterns' succeeded.
```

Traffic matrix after enabling CNI enforcement (T12–T15), against the nginx pod
in `ns3` (annotated `ihs-*`):

```
t-allowed  (ihs-2)         HTTP:200                            allowed via ihs-*
t-self     (ns3)           HTTP:200                            allowed via self rule
t-blocked  (nomatch-test)  curl: (28) timed out after 8002 ms  BLOCKED
t-new      (ihs-12, created ~12 s earlier)  HTTP:200           picked up on create
```

All test resources (pods, `ihs-11`, `ihs-12`, `nomatch-test`, `wctest`, test
annotations) were cleaned up after the run.
