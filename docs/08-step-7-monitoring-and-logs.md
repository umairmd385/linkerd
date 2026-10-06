# 8. Step 7: monitoring, alerts and logs

**Goal:** you can see request rate, success rate, latency and mTLS coverage for every meshed service, get alerted when the mesh
is unhealthy, and search request logs. **Impact:** none on traffic.

Linkerd has an optional add-on called `linkerd-viz` (its own dashboard and Prometheus). **We do not use it**: it duplicates our
VictoriaMetrics, and its `tap` feature can stream live request metadata from any pod (an extra privileged surface). The same
numbers come from the proxies' own metrics, shown in four Grafana dashboards we generate.

## 8.1 How the data flows

```
 linkerd-proxy (every pod, port 4191 /metrics) ──scraped every 30 s──> VictoriaMetrics ──> Grafana dashboards
 Envoy Gateway proxy pods, linkerd-proxy access logs (stdout JSON) ──> Vector ──> VictoriaLogs ──> Grafana "Explore"
 cert-manager metrics (port 9402) ──> VictoriaMetrics ──> certificate-expiry alerts ──> vmalert ──> Alertmanager ──> Google Chat
```

## 8.2 Files (apply in this order)

| File | Kind | What it does |
|---|---|---|
| [`50-vmpodscrape-linkerd.yaml`](../manifests/50-vmpodscrape-linkerd.yaml) | `VMPodScrape` | Scrapes **every** Linkerd proxy (port `linkerd-admin`, 4191) in all namespaces. Relabelling copies pod labels onto the metrics so queries can say `namespace`, `deployment`, `pod`. With native sidecars the proxy is an init container; the `keep` rule matches it by container name. |
| [`53-vmservicescrape-cert-manager.yaml`](../manifests/53-vmservicescrape-cert-manager.yaml) | `VMServiceScrape` | Scrapes cert-manager's metrics (port `http-metrics`, 9402). **Required** by the certificate-expiry alerts: without it those alerts can never fire. |
| [`51-grafana-linkerd-dashboards.yaml`](../manifests/51-grafana-linkerd-dashboards.yaml) | 4 × `ConfigMap` (label `grafana_dashboard: "1"`) | The four dashboards; Grafana's sidecar loads any ConfigMap with that label. **Generated** by `scripts/gen_linkerd_dashboards.py`: change the script, not the YAML. |
| [`52-vmrule-linkerd-security.yaml`](../manifests/52-vmrule-linkerd-security.yaml) | `VMRule` | 11 alert rules (table below). |

```bash
kubectl apply -f manifests/50-vmpodscrape-linkerd.yaml
kubectl apply -f manifests/53-vmservicescrape-cert-manager.yaml
python3 scripts/gen_linkerd_dashboards.py --check      # runs every dashboard query against VictoriaMetrics (needs kubectl access)
kubectl apply -f manifests/51-grafana-linkerd-dashboards.yaml
kubectl apply -f manifests/52-vmrule-linkerd-security.yaml
```

Check that metrics arrive (a few minutes after meshing a pod): in Grafana Explore (VictoriaMetrics data source) run
`count(request_total{direction="inbound"})` → a number greater than 0.

These objects are VictoriaMetrics-operator kinds. If you use the Prometheus Operator, convert `VMPodScrape`/`VMServiceScrape` to
`PodMonitor`/`ServiceMonitor` and `VMRule` to `PrometheusRule`; queries and metric names are unchanged.

## 8.3 The four dashboards (Grafana folder "Linkerd")

| Dashboard (uid) | Use it to answer |
|---|---|
| **Linkerd / Overview** (`linkerd-overview`) | Is everything healthy? Top stats: meshed pods, success rate, requests/s, p99 latency, **mTLS requests**, policy denials. A per-workload table (RPS, success, p50/p95/p99, mTLS req/s) and trend graphs. |
| **Linkerd / Workload** (`linkerd-workload`) | Pick one deployment: its callers, what it calls, status codes, latency, proxy CPU/RAM, and a live access-log panel. |
| **Linkerd / Service graph** (`linkerd-graph`) | Who calls whom, with success rate and latency per edge, built from the callee's metrics and the caller's mTLS identity. |
| **Linkerd / Security & control plane** (`linkerd-security`) | Certificate countdowns (identity issuer, trust anchor), control-plane pods ready, pods **without** a proxy, mTLS requests/s and *by caller identity*, requests with no mesh identity, policy allows/denies, control-plane CPU/memory/restarts, certificate refreshes. |

### Reading the mTLS panels correctly

Linkerd labels each request `tls="true"` (mTLS), or `tls="no_identity"` (the caller did not present a mesh identity). **Most
`no_identity` requests are Kubernetes health probes**, which come from the node's kubelet and can never carry a mesh identity;
in production they were about 99% of what the proxies saw, because real user traffic is small compared to probes. That is why
the dashboards show the **count** of mTLS requests and a **breakdown by caller identity**, not a percentage. A table lists
requests with no identity: normal rows are workloads with health probes (about 0.2-0.6 req/s per pod); investigate a workload
whose rate is far higher, that is an unmeshed client.

## 8.4 Alert rules (`52-vmrule-linkerd-security.yaml`)

Alerts reach Google Chat through the existing path (vmalert → Alertmanager → calert). Group **linkerd.security**:

| Alert | Fires when | Severity |
|---|---|---|
| `PodWithoutLinkerdProxy` | a Running pod in `prod-app-ns` has no `linkerd-proxy` (no mTLS, no policy) for 10 min | critical |
| `LinkerdPolicyDenials` | more than 0.2 denied HTTP requests/s to a deployment for 10 min | warning |
| `LinkerdTcpPolicyDenials` | more than 0.2 denied TCP connections/s (Redis, opaque ports) for 10 min | warning |
| `LinkerdIdentityIssuerNotRenewing` | the issuer certificate has under 12 h left for 5 min (cert-manager is failing to renew) | critical |
| `LinkerdTrustAnchorExpiring` | the root has under 90 days left | warning |
| `LinkerdControlPlaneDown` | a control-plane deployment has 0 available replicas for 3 min | critical |
| `LinkerdCertMetricsMissing` | cert-manager's metrics for the issuer are missing for 30 min (the two alerts above would be blind) | warning |

Group **linkerd.reliability**:

| Alert | Fires when | Severity |
|---|---|---|
| `EdgeHigh5xxRate` | over 5% of requests through the edge fail for 5 min (and traffic is above 0.5 req/s) | critical |
| `EdgeGatewayDegraded` | fewer than 2 edge proxy pods are available for 5 min | critical |
| `ServiceSuccessRateLow` | a service's success rate is under 95% for 10 min (at least 0.1 req/s) | warning |
| `ServiceLatencyHigh` | a service's p95 latency is over 2 s for 15 min (the socket service is excluded: WebSocket sessions are long-lived) | warning |

The two policy-denial alerts stay silent until policy is enforced (step 8): the denial counters do not exist before the first
denial. The thresholds (5%, 95%, 2 s) were chosen at the start; tune them against your own traffic.

## 8.5 Logs

Two kinds of request logs exist, both JSON, one line per request:

| Log | Where it comes from | Vector tag | Notes |
|---|---|---|---|
| **Edge access log** | Envoy Gateway proxies, container `envoy` in `envoy-gateway-system` (format defined in `30-envoyproxy.yaml`) | `log_type: edge_access` | One line per public request. Authorization headers, cookies and bodies are **not** logged. Fields: `start_time, method, path, authority, response_code, response_flags, duration, bytes_*, x_forwarded_for, downstream_remote_address, user_agent, request_id, upstream_host, route_name` |
| **Mesh access log** | every `linkerd-proxy`, switched on by the pod annotation `config.linkerd.io/access-log: json` | `log_type: mesh_access` | Fields: `client.addr, client.id` (the caller's **mesh identity**, `-` if none), `host, method, uri, status, total_ns, user_agent` |

The tags are set in Vector's `process_logs` transform (production file `monitoring-and-alerts/vector-values.yaml`):

```
container = string(.kubernetes.container_name) ?? ""
if container == "envoy" && starts_with(string(.kubernetes.pod_namespace) ?? "", "envoy-gateway") {
    .log_type = "edge_access"
} else if container == "linkerd-proxy" {
    .log_type = "mesh_access"
} else {
    .log_type = "app"
}
```

Useful VictoriaLogs queries (Grafana → Explore → VictoriaLogs; or dashboard *VictoriaLogs Explorer* with `namespace=envoy-gateway-system`,
`container=envoy`):

```
log_type:edge_access _time:15m                                   # all public requests
log_type:edge_access response_code:5*                            # server errors at the edge
log_type:edge_access authority:"api.example.com" response_code:429    # rate-limited requests to the API
log_type:mesh_access kubernetes.pod_namespace:"prod-app-ns" -user_agent:"kube-probe/1.36"     # real traffic, no probes
log_type:mesh_access client.id:!"-"                              # requests with a mesh identity (who called whom)
```

## 8.6 Check

* [ ] `count(request_total)` returns a number greater than 0
* [ ] The four dashboards appear in Grafana, and **Linkerd / Overview** shows your meshed services
* [ ] `kubectl -n monitoring-ns get vmrule linkerd-security-alerts` exists; in vmalert (`/api/v1/rules`) all 11 rules are `health: ok`
* [ ] `log_type:edge_access` returns rows (after Vector is updated)

Next: [09-step-8-security-policy.md](09-step-8-security-policy.md).
