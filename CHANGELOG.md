# Changelog

All notable changes to the operator and its Helm chart are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Two versions are tracked per release: the **operator image** (`appVersion`,
also the Python package version) and the **Helm chart** version.

## Chart 0.2.1 - 2026-08-04

### Fixed

- Uninstall cleanup hook failed with a 403 on `patch networkpolicies`: its
  ClusterRole granted only `list` and `delete`, but the hook patches away
  leftover Kopf finalizers before deleting. Added the `patch` verb.

## Operator 0.6.0 / Chart 0.2.0 - 2026-07-23

### Verified

- Wildcard support validated end to end on a live EKS cluster (15/15 test
  cases): policy expansion, on-create pickup (< 10 s), resync pruning,
  rejection/warning paths, and a traffic matrix with AWS VPC CNI network
  policy enforcement enabled. See
  [docs/wildcard-annotation-test-report.md](docs/wildcard-annotation-test-report.md).
  Note: clusters must have CNI NetworkPolicy enforcement enabled or the
  generated policies fail open (found and fixed on the test cluster).

### Added

- Prefix-wildcard support in the `truefoundry.com/allowed-ingress-namespaces`
  annotation: a token like `ihg-*` allows ingress from every namespace whose
  name starts with `ihg-`. Patterns can be mixed with plain names
  (`"argocd,ihg-*"`); only a single trailing `*` is supported. Since
  NetworkPolicies cannot express wildcards, the operator expands patterns into
  exact per-namespace selectors and keeps them in sync.
- Immediate reconciliation when a namespace matching a declared prefix pattern
  is created (no longer waits up to `resyncIntervalSeconds`); removals of
  matched namespaces are cleaned up on the next resync.
- GitHub Actions workflow (`artifactory-helm-release.yaml`) that publishes the
  Helm chart to `oci://tfy.jfrog.io/tfy-helm` on push to `main`, skipping
  already-published versions.
- GitHub Actions workflow (`artifactory-image-release.yaml`) that builds and
  publishes the operator image to `tfy.jfrog.io/tfy-images` on push to `main`
  via the shared `truefoundry/github-workflows-public` build workflow, tagged
  with the chart's `appVersion` and skipping already-published tags.
- Chart: `helm uninstall` now removes every operator-managed NetworkPolicy via
  a post-delete hook Job (`cleanupOnUninstall`, default `true`). Runs after the
  operator is gone so the drift watch cannot recreate the policies mid-cleanup.

### Fixed

- Managed NetworkPolicies no longer carry a Kopf finalizer
  (`kopf.zalando.org/KopfFinalizerMarker`). Previously, deleting a managed
  policy while the operator was stopped (scaled down or uninstalled) hung in
  `Terminating` forever because only the operator could remove the finalizer.
  The drift-watch delete handler is now registered with `optional=True`
  (best-effort deletion events; the resync timer covers any missed drift), and
  the uninstall cleanup hook strips leftover finalizers from policies created
  by older operator versions.

- Policy apply order: leftover dead code from the apply-order change (#4) made
  the operator still apply `tfy-np-default-deny-ingress` first and ignore the
  `defaultDenyIngress` config toggle. Allow policies are now applied before the
  default-deny backstop, and the backstop is omitted when
  `defaultDenyIngress: false`.
- README described the outdated deny-first ordering.

## Operator 0.5.0 and earlier

Predates this changelog. Covers the initial annotation-driven operator
(default-deny/allow-egress/allow-ingress policy set, baseline allows,
system-namespace exclusion, dry-run mode) and its runtime fixes (#2–#4).
