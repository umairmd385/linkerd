# 3. Step 2: install the Linkerd control plane

**Goal:** Linkerd runs in the `linkerd` namespace and `linkerd check` is green. **Impact:** none on applications (nothing is
meshed yet).

## 3.1 What gets installed

Two Helm charts from the **edge** channel (`https://helm.linkerd.io/edge`), both at version `2026.9.3`
(= Linkerd `edge-26.9.3`):

| Chart | Installs |
|---|---|
| `linkerd-crds` | Linkerd's CRDs: `Server`, `AuthorizationPolicy`, `MeshTLSAuthentication`, `HTTPRoute` (Linkerd's policy flavour), `ServiceProfile`, ... |
| `linkerd-control-plane` | the control-plane pods (below) and the injector webhook |

Control-plane pods (2 copies of each, spread over different nodes):

| Pod | Job |
|---|---|
| `linkerd-identity` | signs each pod's certificate (using the issuer from step 1) |
| `linkerd-destination` | tells proxies where services are and which policies apply (also runs the **policy** controller and the service-profile validator) |
| `linkerd-proxy-injector` | a **webhook**: when a pod is created with `linkerd.io/inject: enabled`, it adds the `linkerd-init` and `linkerd-proxy` containers |

> **Why not the Linkerd CLI to install?** `linkerd install` creates a one-year certificate by hand and cannot use cert-manager's
> issuer; changing that later needs a dangerous certificate swap. We learned this the hard way; see
> [13-lessons-learned.md](13-lessons-learned.md). Use Helm.

## 3.2 The values file

[`manifests/11-linkerd-control-plane-values.yaml`](../manifests/11-linkerd-control-plane-values.yaml). The important keys:

| Key | Value | Meaning |
|---|---|---|
| `identity.issuer.scheme` | `kubernetes.io/tls` | take the issuer from a normal TLS secret, i.e. from cert-manager |
| `identity.issuer.issuanceLifetime` | `24h0m0s` | lifetime of each **pod** certificate |
| `controllerReplicas` | `2` | two copies of every control-plane component (see the box below) |
| `enablePodAntiAffinity`, `enablePodDisruptionBudget` | `true` | copies on different nodes; a node drain cannot take both |
| `defaultInboundPolicy` | `all-unauthenticated` | permissive default: pods accept any caller until you add policy (step 8) |
| `nativeSidecar` | `true` | proxy runs as a Kubernetes native sidecar |
| `proxy.resources` | CPU request `25m`, memory request `32Mi`, limit `256Mi`, **no CPU limit** | measured usage is ~5-10 MiB and ~8 millicores; a CPU limit would throttle the proxy under bursts |
| `proxy.opaquePorts` | `25,587,3306,4444,5432,6379,9092,9093,9300,11211,26379` | ports treated as raw TCP (mail, MySQL, Redis, Kafka, Redis Sentinel...) so Linkerd does not try to guess HTTP on them |
| `proxy.cores` | `2` | cap on the proxy's worker threads |
| `proxy.logLevel` | `warn,linkerd=info` | quiet logs |
| `proxyInit.runAsRoot` | `false` | the init container runs without full root |
| `*Resources` | small requests | sizes of the control-plane containers |

> **Why two replicas?** With one proxy-injector, restarting it (for example during an upgrade) lets pods that are created in that
> moment start **without a proxy**, because the webhook's failure policy is `Ignore`. On 2026-10-06 six pods started
> unmeshed that way. Two replicas close the gap. The same applies to `identity` (certificates for new pods) and `destination`.

> **The proxy-resource numbers matter for autoscaling.** KEDA/HPA CPU *utilisation* is usage divided by the **sum of the pod's
> container requests**, so the proxy's request is part of the denominator. Changing it slightly changes when scaling triggers.

## 3.3 Install

```bash
helm repo add linkerd-edge https://helm.linkerd.io/edge
helm repo update

# 1) CRDs
helm install linkerd-crds linkerd-edge/linkerd-crds --version 2026.9.3 -n linkerd

# 2) control plane (reads the trust anchor file you exported in step 1)
helm install linkerd-control-plane linkerd-edge/linkerd-control-plane --version 2026.9.3 -n linkerd \
  -f manifests/11-linkerd-control-plane-values.yaml \
  --set-file identityTrustAnchorsPEM=/tmp/linkerd-ca.crt \
  --wait --timeout 6m
```

Preview first without changing anything: add `--dry-run` (or run `helm template ... > /dev/null` to prove the chart renders).

> **What was run in production vs. what is derived.** Production's control plane was installed with one command like the second
> one above (after removing an earlier CLI install); its CRDs were installed by the CLI. The two-chart sequence above for a
> brand-new cluster is derived from the chart defaults (`linkerd-crds` installs only Linkerd's CRDs by default,
> `installGatewayAPI: false`; Gateway API CRDs come with Envoy Gateway in step 3). Dry-run it.

## 3.4 Check

```bash
kubectl -n linkerd get pods                 # 6 pods, each Running (2 identity, 2 destination, 2 proxy-injector)
kubectl -n linkerd get certificate          # both READY True
linkerd check --wait 120s                   # every line should be a green check
```

`linkerd check` is the single most useful command in this whole setup: it verifies certificates, webhooks, the control plane and
(later) the data-plane proxies. A *warning* is worth reading; an *error* means stop.

* [ ] `linkerd check` passes
* [ ] 6 control-plane pods Running on different nodes: `kubectl -n linkerd get pods -o wide`
* [ ] The identity certificates are still `READY True`

## 3.5 Look at what you installed

```bash
kubectl -n linkerd get deploy
kubectl get crd | grep linkerd
kubectl get mutatingwebhookconfiguration | grep linkerd      # the injector
```

## 3.6 Roll back

Safe while nothing is meshed:

```bash
helm uninstall linkerd-control-plane -n linkerd
helm uninstall linkerd-crds -n linkerd      # also removes Linkerd's CRDs and every policy object. Be sure.
```

Next: [04-step-3-install-envoy-gateway.md](04-step-3-install-envoy-gateway.md).
