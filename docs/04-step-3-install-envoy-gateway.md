# 4. Step 3: install Envoy Gateway

**Goal:** the Envoy Gateway controller runs and the Gateway API / Envoy Gateway CRDs exist. **Impact:** none yet (no gateway
exists until step 4).

## 4.1 What Envoy Gateway is, and what you are installing

Envoy Gateway is a **controller**. You describe the public entry point with Gateway API objects (`Gateway`, `HTTPRoute`) and
Envoy Gateway-specific policy objects. The controller then **creates and configures the real proxy pods** (Envoy) and the
Kubernetes `Service` of type `LoadBalancer` that becomes the AWS NLB.

So there are two kinds of pods in `envoy-gateway-system`:

| Pods | Created by | Job |
|---|---|---|
| `envoy-gateway-...` (1) | the Helm chart (this step) | the controller: reads your objects, configures the proxies |
| `envoy-envoy-gateway-system-prod-edge-gateway-...` (2-5) | the controller, from `30-envoyproxy.yaml` (step 4) | the proxies that carry the traffic; autoscale on CPU |

## 4.2 CRDs first, in a separate chart

CRDs are installed separately from the controller (cleaner upgrades, and Linkerd's policy objects must not collide with the
Gateway API ones):

```bash
helm install eg-crds oci://docker.io/envoyproxy/gateway-crds-helm --version 1.9.2 \
  -n envoy-gateway-system --create-namespace \
  --set crds.gatewayAPI.enabled=true --set crds.gatewayAPI.channel=standard \
  --set crds.envoyGateway.enabled=true
```

* `crds.gatewayAPI` = the Kubernetes Gateway API CRDs (`Gateway`, `HTTPRoute`, `ReferenceGrant`, ...). Use the **standard**
  channel (stable features only).
* `crds.envoyGateway` = Envoy Gateway's own CRDs: `EnvoyProxy`, `ClientTrafficPolicy`, `BackendTrafficPolicy`,
  `SecurityPolicy`, `EnvoyExtensionPolicy`, `EnvoyPatchPolicy`.

> **Production note.** In production the Gateway API CRDs (v1.6.2, standard channel) already existed, installed earlier by the
> Linkerd CLI, so we applied only Envoy Gateway's CRDs with `--set crds.gatewayAPI.enabled=false` via
> `helm template ... | kubectl apply --server-side -f -`. On a clean cluster use the command above (it installs Gateway API
> v1.6.1, standard channel, and works with Linkerd). Never install two different Gateway API bundles over each other.

## 4.3 The controller

```bash
helm install eg oci://docker.io/envoyproxy/gateway-helm --version 1.9.2 \
  -n envoy-gateway-system \
  --set crds.enabled=false \
  -f manifests/12-envoy-gateway-values.yaml \
  --wait
```

[`manifests/12-envoy-gateway-values.yaml`](../manifests/12-envoy-gateway-values.yaml):

| Key | Value | Why |
|---|---|---|
| `deployment.replicas` | `1` | the controller only translates configuration; losing it for a minute does not stop traffic |
| `deployment.envoyGateway.resources` | 100m / 128Mi request, 512Mi limit | small and bounded |
| `config.envoyGateway.extensionApis.enableLua` | `true` | needed by `32-envoy-extension-policy-security.yaml` (security headers/CORS written in Lua). **Lua is off by default** |
| `config.envoyGateway.extensionApis.enableEnvoyPatchPolicy` | `true` | needed by `33-envoy-patch-policy-server-header.yaml` |

> **Security consequence of those two switches.** Anyone who can create an `EnvoyExtensionPolicy` can run Lua code inside the
> edge proxy, and anyone who can create an `EnvoyPatchPolicy` can rewrite its configuration (Envoy Gateway advisory
> GHSA-xrwg-mqj6-6m22). Keep the RBAC verbs for `envoyextensionpolicies` and `envoypatchpolicies` limited to platform admins.

## 4.4 Check

```bash
kubectl -n envoy-gateway-system get pods                         # envoy-gateway-... Running
kubectl get crd | grep -E "gateway.networking.k8s.io|gateway.envoyproxy.io"
kubectl get gatewayclass                                         # empty until step 4: that is normal
```

* [ ] Controller pod `Running` and `Ready`
* [ ] Gateway API CRDs (`gateways`, `httproutes`, `referencegrants`, ...) exist
* [ ] Envoy Gateway CRDs (`envoyproxies`, `securitypolicies`, ...) exist

## 4.5 Roll back

```bash
helm uninstall eg -n envoy-gateway-system
helm uninstall eg-crds -n envoy-gateway-system     # deletes all Gateway/HTTPRoute objects as well
```

Next: [05-step-4-configure-the-edge.md](05-step-4-configure-the-edge.md).
