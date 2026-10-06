# 12. File reference: every file in this directory

How to read the tables: **Step** is the guide step that uses the file; **Live?** says how it compares with what runs in
production (checked with `kubectl diff` / `helm get values` on 2026-10-06).

## 12.1 Top level

| File | What it is |
|---|---|
| [`README.md`](../README.md) | Start page: what is installed, the request path, the reading order, known limits |
| `docs/` | The guide (below); its own index is `docs/README.md` |
| `manifests/` | Every Kubernetes / Helm file, numbered in the order it is used (below); index and working rules in `manifests/README.md` |
| `examples/` | Small commented templates to copy; index in `examples/README.md` |
| `scripts/` | Helper scripts (below); index in `scripts/README.md` |

## 12.2 `docs/`

| File | Content |
|---|---|
| `README.md` | Index of the guide with the reading order and time estimates |
| `background-1-why-a-service-mesh.md` | What a service mesh is, how it works, what problems it solves and costs, whether you need one |
| `background-2-the-case-for-linkerd.md` | What Linkerd is, why we chose it over Istio, how it works, an honest scorecard and alternatives |
| `00-linkerd-from-zero.md` | Concepts and glossary for a complete beginner |
| `01-prerequisites.md` | Tools, access, cluster requirements, capacity, decisions |
| `02-step-1-cert-manager-and-identity.md` | Install cert-manager, create the `linkerd` namespace and certificates |
| `03-step-2-install-linkerd.md` | Helm install of Linkerd, every value explained |
| `04-step-3-install-envoy-gateway.md` | CRDs and controller of Envoy Gateway |
| `05-step-4-configure-the-edge.md` | Gateway, routes and edge policies; tests without DNS |
| `06-step-5-mesh-your-services.md` | Adding the proxy to applications, one at a time |
| `07-step-6-dns-and-cutover.md` | Route 53 switch and rollback |
| `08-step-7-monitoring-and-logs.md` | Scraping, dashboards, alerts, logs |
| `09-step-8-security-policy.md` | Server / AuthorizationPolicy, audit then deny |
| `10-operations.md` | Daily checks, adding services, upgrades, certificates, capacity |
| `11-troubleshooting.md` | Symptom → cause → fix |
| `12-file-reference.md` | This page |
| `13-lessons-learned.md` | Incidents and mistakes, and what they taught us |

## 12.3 `manifests/` (numbered by use)

The numbers leave gaps on purpose (10-12 install, 30s edge, 40s services/policy, 50s monitoring). `40` is the generated policy
file (not stored here: you generate it in step 8).

### Installation (steps 1-3)

| File | Kind | What it does | Step | Live? |
|---|---|---|---|---|
| `10-cert-manager-linkerd-identity.yaml` | 2 `Issuer`, 2 `Certificate` (cert-manager) | The self-renewing certificate chain for Linkerd: `linkerd-selfsigned` Issuer → `linkerd-trust-anchor` Certificate (10 y, ECDSA, `isCA`) → `linkerd-trust-anchor` Issuer → `linkerd-identity-issuer` Certificate (48 h, renew 25 h before). The namespace is *not* in the file (create it with its labels first). Edit: only the lifetimes, if you change policy. | 1 | identical |
| `11-linkerd-control-plane-values.yaml` | Helm values | Settings of the `linkerd-control-plane` chart: cert-manager issuer (`scheme: kubernetes.io/tls`), pod-cert lifetime 24 h, 2 replicas + anti-affinity + PDB, permissive default policy, native sidecar, proxy size (25m / 32Mi, limit 256Mi, no CPU limit), opaque ports, log level, small control-plane requests. The trust anchor is passed on the command line (`--set-file identityTrustAnchorsPEM=`). Edit to change proxy size/ports; then `helm upgrade` and roll services. | 2 | identical (values match `helm get values`) |
| `12-envoy-gateway-values.yaml` | Helm values | Settings of the `gateway-helm` chart: controller size, **`enableLua: true`**, **`enableEnvoyPatchPolicy: true`**, log level. | 3 | identical (live also has a temporary node preference, not kept here) |

### The public edge (step 4)

| File | Kind | What it does | Live? |
|---|---|---|---|
| `30-envoyproxy.yaml` | `EnvoyProxy prod-edge` | How Envoy Gateway builds the edge: 2-5 proxy pods (HPA at 80% CPU), pod annotations to join the mesh and skip inbound ports 10080/10443, the NLB `Service` annotations (internet-facing, PROXY protocol, TLS 1.3 + ACM cert), and the JSON access-log format. | identical except the live object still carries a temporary node-preference block that this copy leaves out |
| `31-gatewayclass-gateway.yaml` | `GatewayClass eg`, `Gateway prod-edge-gateway`, `ReferenceGrant allow-httproutes` | The class (which software), the Gateway (listeners 80 and 443), and the grant that lets routes from four namespaces attach. | identical |
| `32-envoy-extension-policy-security.yaml` | `EnvoyExtensionPolicy security-headers-and-cors` | Lua: security headers, strict CORS, removes `Server`/`X-Powered-By`. | identical |
| `33-envoy-patch-policy-server-header.yaml` | `EnvoyPatchPolicy server-header-pass-through` | Stops Envoy re-adding `server: envoy`. | identical |
| `34-client-traffic-policy.yaml` | `ClientTrafficPolicy nlb-proxy-protocol` | Accept the NLB's PROXY protocol (real client IP); 3600 s idle timeout. **No `clientIPDetection`.** | identical |
| `35-httproutes.yaml` | 11 × `HTTPRoute` | hostname/path → Service for ArgoCD, Jenkins, Grafana, phpMyAdmin, the API (3 routes), charting library, socket, admin and user front-ends. | identical |
| `36-backend-traffic-policy.yaml` | 2 × `BackendTrafficPolicy` | Retries on `/users` and `/notification` only. | identical |
| `37-security-policy-admin.yaml` | 4 × `SecurityPolicy` | Office/VPN IP allow-list for ArgoCD, Jenkins, Grafana, phpMyAdmin (default Deny). | identical |
| `38-rate-limit.yaml` | `BackendTrafficPolicy per-ip-rate-limit` | 30 requests/s per client IP at the Gateway; 429 above. Comments explain why it is a flood guard. | identical |

### Identities and optional network layer (steps 5, 8)

| File | Kind | What it does | Live? |
|---|---|---|---|
| `41-serviceaccounts.yaml` | 21 × `ServiceAccount` | One identity per chart-built service (`automountServiceAccountToken: false`). The label `managed-by: migrations-istio-to-linkerd` is historical; harmless. | identical |
| `43-serviceaccounts-redis-phpmyadmin.yaml` | 3 × `ServiceAccount` | Identities for Redis, RedisInsight, phpMyAdmin (not built from the shared chart). | identical |
| `42-networkpolicy-ingress-baseline.yaml` | 2 × `NetworkPolicy` | **Optional, not applied in production.** Default-deny ingress for the application namespace except same namespace, the edge, monitoring and Linkerd. | not applied |

### Monitoring (step 7)

| File | Kind | What it does | Live? |
|---|---|---|---|
| `50-vmpodscrape-linkerd.yaml` | `VMPodScrape linkerd-proxy` | Scrape every proxy's `/metrics` (port 4191) and copy pod labels onto the series. | identical |
| `51-grafana-linkerd-dashboards.yaml` | 4 × `ConfigMap` (label `grafana_dashboard: "1"`) | The four Linkerd dashboards. **Generated** by `scripts/gen_linkerd_dashboards.py`. | identical |
| `52-vmrule-linkerd-security.yaml` | `VMRule linkerd-security-alerts` | 11 alert rules (groups `linkerd.security`, `linkerd.reliability`). | identical |
| `53-vmservicescrape-cert-manager.yaml` | `VMServiceScrape cert-manager-metrics` | Scrape cert-manager (port `http-metrics`) so the certificate-expiry alerts can work. | identical |

## 12.4 `scripts/`

All scripts are plain Python 3 (`pyyaml`), read the live cluster through `kubectl`, and only **print** or **write a file**; none
changes the cluster by itself except where stated.

| Script | What it does | When to use |
|---|---|---|
| `gen_linkerd_dashboards.py` | Builds the four Grafana dashboards and writes `manifests/51-...yaml`. `--check` runs every query against VictoriaMetrics through a `kubectl port-forward`. | after editing a panel in the script |
| `gen_callgraph.py` | Reads the proxies' metrics over a window (`--window 30d`) and prints, as JSON, which caller identities reached each service (`client_id` on inbound requests and TCP connections). | step 8, before generating policy |
| `gen_linkerd_policy.py` | Prints the Server / AuthorizationPolicy / MeshTLSAuthentication objects for every Service, in stage A (baseline) or B/C (`--callgraph`, `--access-policy audit|deny`), with `--extra-caller` for callers the graph missed. Reads the routes in `manifests/35-httproutes.yaml` to know which services are public. | step 8 |
| `check_policy_coverage.py` | Reports pods without a proxy, ports without a Server, and Servers still permissive/in audit. Exit code 1 if anything needs attention. | after applying policy; in reviews |
| `resize_proxy_requests.py` | Dry-run by default. With `--go`, restarts, **one deployment at a time with checks**, every deployment whose proxy CPU request is above the target (25m) *or that has no proxy*; skips the matching engine and Redis. | after changing proxy defaults ([10.5](10-operations.md)) |

## 12.5 `examples/`

| File | Shows | Validated |
|---|---|---|
| `mesh-a-service.yaml` | ServiceAccount + the pod-template annotations that put a service in the mesh | server-side dry run |
| `httproute.yaml` | A public route (single host, and several services behind one host), plus an admin-IP `SecurityPolicy` (commented) | server-side dry run |
| `policy-example.yaml` | `MeshTLSAuthentication` ×2, `Server`, `AuthorizationPolicy` ×2 and how to read them | server-side dry run |

## 12.6 Files that live elsewhere but belong to this setup

| File | Where | Role |
|---|---|---|
| `vector-values.yaml` (the `process_logs` transform) | `monitoring-and-alerts/` | Tags request logs `edge_access` / `mesh_access` ([08](08-step-7-monitoring-and-logs.md)) |
| `services/<service>/values.yaml` (`deployment.serviceAccountName`, `podAnnotations`) | the GitOps repo, branch `production` | Where each service gets its Linkerd annotations in production |
| `redis/deployment.yaml`, `redis-insight.yaml`, `phpymadmin/deployment.yaml` | `gitops-repo/` | The three non-chart workloads, with Linkerd annotations and pinned images |
| `infra/migrations/istio-to-linkerd/` | repository | The record of the Istio → Linkerd migration (history, scripts that refer to Istio). Not needed to install Linkerd |

## 12.7 A note on "Istio" in comments

Several manifests mention Istio in their comments ("port of the Istio EnvoyFilter ..."). Istio was the previous mesh and has been
removed from the cluster; those comments explain *why a setting exists* (it reproduces an old behaviour) and can be ignored by
a new reader.
