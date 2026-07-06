# tfy-netpol-operator

Annotation-driven Kubernetes NetworkPolicy operator for the TrueFoundry ecosystem.
Built on [Kopf](https://kopf.readthedocs.io/). See the design docs:
[HLD](../NETWORK-POLICY-OPERATOR-HLD.md) and [LLD](../NETWORK-POLICY-OPERATOR-LLD.md).

## What it does

For every namespace carrying the single annotation
`truefoundry.com/allowed-ingress-namespaces`, the operator reconciles three standard
`networking.k8s.io/v1` NetworkPolicies, applied in this order:

1. `tfy-np-default-deny-ingress` — default-deny ingress (applied first).
2. `tfy-np-allow-egress` — allow-all egress (egress stays open).
3. `tfy-np-allow-ingress` — allow ingress from the same namespace, the configured
   baseline namespaces, and the namespaces listed in the annotation.

Annotation semantics:

| Annotation state | Result |
|------------------|--------|
| Absent | Namespace not managed; any managed policies are removed. |
| Present, empty (`""`) | Default-deny ingress + allow-all egress + allow `self` + baselines. |
| Present, with list (`"argocd,prometheus"`) | The above **plus** allow ingress from each listed namespace. |

Key safety properties: **deny-before-allow** ordering (the default-deny is applied
first so ingress is locked down before the allow rules are added; a failed
mid-reconcile therefore fails closed), **baseline allows** so an empty annotation
cannot black-hole a namespace, **system-namespace exclusion**, and **dry-run** mode
for first rollout.

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
TrueFoundry Provided Operator image: `tfy.jfrog.io/tfy-images/tfy-netpol-operator:0.5.0`

```bash
Build:
docker build -t <registry>/tfy-netpol-operator:0.1.0 .
docker push <registry>/tfy-netpol-operator:0.1.0

Deploy:
This image can be used: tfy.jfrog.io/tfy-images/tfy-netpol-operator:0.2.0
helm upgrade --install tfy-netpol-operator deploy/helm/tfy-netpol-operator \
  -n tfy-system --create-namespace \
  --set image.repository=tfy.jfrog.io/tfy-images/tfy-netpol-operator \
  --set image.tag=0.5.0 \
  --set 'config.baselineAllowedNamespaces={ingress-nginx,prometheus,tfy-agent}' \
  --set config.dryRun=true
```

Roll out with `config.dryRun=true` first, review the logged intended policies, then set
`config.dryRun=false` to enforce.

## Enroll a namespace

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: my-addon
  annotations:
    truefoundry.com/allowed-ingress-namespaces: "argocd,prometheus"
```

## Argo CD coexistence

Generated policies carry `app.kubernetes.io/managed-by: tfy-netpol-operator` and are not
in any Argo CD Application's Git source. Exclude them from Argo pruning (resource
exclusion or `ignoreDifferences`) so Argo does not delete operator-owned policies. See
LLD §8.

