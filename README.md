# tfy-netpol-operator

Annotation-driven Kubernetes NetworkPolicy operator for the TrueFoundry ecosystem.
Built on [Kopf](https://kopf.readthedocs.io/). See the design docs:
[HLD](../NETWORK-POLICY-OPERATOR-HLD.md) and [LLD](../NETWORK-POLICY-OPERATOR-LLD.md).

## What it does

For every namespace carrying the single annotation
`truefoundry.com/allowed-ingress-namespaces`, the operator reconciles three standard
`networking.k8s.io/v1` NetworkPolicies, applied in this order:

1. `tfy-np-allow-egress` — allow-all egress (egress stays open).
2. `tfy-np-allow-ingress` — allow ingress from the same namespace, the configured
   baseline namespaces, and the namespaces listed in the annotation.
3. `tfy-np-default-deny-ingress` — default-deny ingress backstop (applied last,
   after the allow rules are in place; can be disabled via `defaultDenyIngress`).

Annotation semantics:

| Annotation state | Result |
|------------------|--------|
| Absent | Namespace not managed; any managed policies are removed. |
| Present, empty (`""`) | Default-deny ingress + allow-all egress + allow `self` + baselines. |
| Present, with list (`"argocd,prometheus"`) | The above **plus** allow ingress from each listed namespace. |
| Present, with prefix wildcard (`"ihg-*"`) | The above **plus** allow ingress from every namespace whose name starts with `ihg-`. New matching namespaces are picked up immediately on creation; deletions are cleaned up on the next resync. Only a single trailing `*` is supported, and it can be mixed with plain names (`"argocd,ihg-*"`). |

Key safety properties: **allow-before-deny** ordering (the allow rules are applied
before the default-deny backstop so a namespace is never left deny-only
mid-reconcile), **baseline allows** so an empty annotation cannot black-hole a
namespace, **system-namespace exclusion**, and **dry-run** mode for first rollout.

## Layout

```
src/tfy_netpol_operator/
  config.py     # ConfigMap-backed settings + system-namespace exclusion
  parsing.py    # annotation parsing / validation
  policies.py   # NetworkPolicy builders
  k8s.py        # idempotent apply/delete + namespace reads
  operator.py   # Kopf handlers + reconcile loop
tests/          # unit tests (parsing, policy builders)
deploy/helm/tfy-netpol-operator/   # Helm chart
```

## Develop & test

```bash
cd network-policy-operator
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## Run locally (against your kubeconfig)

```bash
pip install -e .
CONFIG_PATH=./dev-config.yaml kopf run --standalone --all-namespaces -m tfy_netpol_operator.operator
```

`dev-config.yaml` example:

```yaml
dryRun: true
allowWildcard: false
baselineAllowedNamespaces: [ingress-nginx, prometheus, tfy-agent]
denylist: [tfy-system]
nodeCIDRs: []
```

## Build
`Note:` Can be skipped if using Truefoundry provided version of Operator

```bash
docker build -t <registry>/tfy-netpol-operator:<image-version> .
docker push <registry>/tfy-netpol-operator:<image-version>
```

## Deploy

TrueFoundry publishes the chart to `oci://tfy.jfrog.io/tfy-helm/tfy-netpol-operator`
and the operator image to `tfy.jfrog.io/tfy-images/tfy-netpol-operator` (both via CI
on merge to `main`; the chart's default image already points at the matching tag).
The registry allows anonymous pulls — no `helm registry login` needed to install:

```bash
helm upgrade --install tfy-netpol-operator oci://tfy.jfrog.io/tfy-helm/tfy-netpol-operator \
  --version 0.2.0 \
  -n tfy-system --create-namespace \
  --set 'config.baselineAllowedNamespaces={istio-system,prometheus,tfy-agent}' \
  --set config.dryRun=true
```

To inspect the chart before installing: `helm pull oci://tfy.jfrog.io/tfy-helm/tfy-netpol-operator --version 0.2.0 --untar`.

When developing, install from the local chart source instead:

```bash
helm upgrade --install tfy-netpol-operator deploy/helm/tfy-netpol-operator \
  -n tfy-system --create-namespace \
  --set image.repository=<registry>/tfy-netpol-operator \
  --set image.tag=<image-version> \
  --set 'config.baselineAllowedNamespaces={istio-system,prometheus,tfy-agent}' \
  --set config.dryRun=true
```

Roll out with `config.dryRun=true` first, review the logged intended policies, then set
`config.dryRun=false` to enforce.

> **Prerequisite:** the cluster's CNI must actually enforce NetworkPolicies, or the
> operator's policies are accepted by the API server but ignored. On EKS with the AWS
> VPC CNI, enable it on the addon (`enableNetworkPolicy: "true"`) and verify with
> `kubectl get policyendpoints -A` — see the
> [wildcard test report](docs/wildcard-annotation-test-report.md) for how this fails open.

## Enroll a namespace

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: my-addon
  annotations:
    truefoundry.com/allowed-ingress-namespaces: "argocd,prometheus"
```

Prefix wildcards are supported, alone or mixed with plain names:

```yaml
    truefoundry.com/allowed-ingress-namespaces: "argocd,ihg-*"
```

## Verification

The wildcard feature was verified end to end on a live EKS cluster (operator 0.6.0):
policy-spec expansion, immediate pickup of newly created matching namespaces (< 10 s),
pruning of deleted namespaces on resync, rejection/warning cases, and a real traffic
matrix with CNI enforcement enabled — 15/15 cases passed. Full details, evidence, and
observed latencies: [docs/wildcard-annotation-test-report.md](docs/wildcard-annotation-test-report.md).

## Disable / uninstall

Per namespace, remove the annotation and the operator deletes its policies there:

```bash
kubectl annotate ns <namespace> truefoundry.com/allowed-ingress-namespaces-
```

To remove everything, uninstall the release — a post-delete hook Job deletes every
operator-managed NetworkPolicy across all namespaces (disable with
`--set cleanupOnUninstall=false` to keep the policies):

```bash
helm uninstall tfy-netpol-operator -n tfy-system
```

For manual cleanup, scale the operator to zero **first** (it recreates its policies
on drift while running), then delete by label:

```bash
kubectl -n tfy-system scale deploy tfy-netpol-operator --replicas=0
kubectl delete netpol -A -l app.kubernetes.io/managed-by=tfy-netpol-operator
```

If policies created by an operator **older than 0.6.0** hang in `Terminating`
here, they carry a Kopf finalizer only the (now stopped) operator could remove;
strip it to let the deletion finish:

```bash
kubectl get netpol -A -o jsonpath='{range .items[*]}{.metadata.namespace} {.metadata.name}{"\n"}{end}' | \
  while read ns name; do kubectl patch netpol "$name" -n "$ns" --type=merge -p '{"metadata":{"finalizers":null}}'; done
```

Note that `config.dryRun: true` only stops new writes — it does not remove policies
that were already applied.

## Argo CD coexistence

Generated policies carry `app.kubernetes.io/managed-by: tfy-netpol-operator` and are not
in any Argo CD Application's Git source. Exclude them from Argo pruning (resource
exclusion or `ignoreDifferences`) so Argo does not delete operator-owned policies. See
LLD §8.

