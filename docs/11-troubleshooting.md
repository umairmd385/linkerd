# 11. Troubleshooting: symptom → cause → fix

Start with the generic tools, then find your symptom in the tables.

```bash
linkerd check                                      # control plane and certificates
linkerd check --proxy -n prod-app-ns          # data plane (the proxies in that namespace)
kubectl -n <ns> describe pod <pod>                 # events: why it is Pending / not Ready
kubectl -n <ns> logs <pod> -c linkerd-proxy        # the proxy's own log (warn level by default)
kubectl -n <ns> logs <pod> -c linkerd-init         # the firewall-setup init container
kubectl -n envoy-gateway-system logs <proxy-pod> -c envoy      # the edge access log and errors
kubectl get httproute -A; kubectl get gateway -A   # are the routes Accepted / Programmed?
```

## A. Pods and the proxy

| Symptom | Likely cause | Fix |
|---|---|---|
| New pod has **no** `linkerd-proxy` (shows `1/1`, not `2/2`) | pod created while the proxy-injector was down; or the annotation is missing/typo'd; or namespace/pod has `linkerd.io/inject: disabled` | `kubectl rollout restart` the deployment; check `kubectl get pod -o jsonpath='{.metadata.annotations}'`; keep 2 injector replicas. Alert `PodWithoutLinkerdProxy` |
| Pod stuck in `Init:0/2` / `Init:CrashLoopBackOff` | `linkerd-init` cannot set firewall rules: pod-security blocks `NET_ADMIN`, or no node capability | `kubectl logs <pod> -c linkerd-init`; the app namespace must allow the init container (we run the default `proxy-init`; the alternative is the Linkerd CNI plugin, see the commented option in `11-...yaml`) |
| Pod `Pending`: "Insufficient cpu/memory" | meshed pods reserve the proxy's requests too | check allocated resources per node ([10.8](10-operations.md)); free capacity or add a node |
| Pod starts, then the **app crash-loops on a connection error** to Redis/Kafka/DB | port not in `proxy.opaquePorts`, or the app connects before the proxy is ready | add the port to `opaquePorts` (values file → `helm upgrade` → roll the service); keep `nativeSidecar: true` |
| `linkerd check --proxy` says a proxy is "not the same version as the control plane" | normal after an upgrade; pods keep the old proxy until recreated | roll the deployments |
| Whole app crashes when **Redis** restarts | the applications exit on an unhandled Redis error (application behaviour, not Linkerd) | restart Redis only in a quiet period; app fix: handle Redis errors (`redis.on('error', ...)`), use the stable service name |

## B. Identity and certificates

| Symptom | Likely cause | Fix |
|---|---|---|
| `linkerd check` error "identity ... certificate"/"trust anchor" | the trust anchor in `identityTrustAnchorsPEM` does not match the issuer's signer; issuer Secret has the wrong type | issuer secret must be `kubernetes.io/tls` (cert-manager makes it); redo `--set-file identityTrustAnchorsPEM=` from `linkerd-trust-anchor` |
| New pods cannot start (`linkerd-proxy` waits for identity) | identity component down, or issuer expired | `kubectl -n linkerd get pods`; `kubectl -n linkerd get certificate`; if cert-manager stopped, fix it first (alert `LinkerdIdentityIssuerNotRenewing`) |
| certificate-expiry alerts never fire / `LinkerdCertMetricsMissing` fires | cert-manager metrics not scraped (the port is named `http-metrics`) | apply `53-vmservicescrape-cert-manager.yaml` |
| `x509: certificate signed by unknown authority` after a Linkerd upgrade/reinstall | a new trust anchor was introduced without the pods trusting it | do not change the anchor casually: this is the planned rotation procedure ([10.7](10-operations.md)) |

## C. The edge (Envoy Gateway)

| Symptom | Likely cause | Fix |
|---|---|---|
| Hostname returns **404** from the NLB | no `HTTPRoute` for that Host, or the route is not attached | `kubectl get httproute -A`; read `status.parents[].conditions` (`Accepted`) |
| Route `Accepted: False`, reason `RefNotPermitted` | the route's namespace is not in the `ReferenceGrant` of `31-...yaml` | add the namespace and re-apply |
| **503** `no healthy upstream` at the edge | backend Service name/port wrong, no ready endpoints | `kubectl -n <ns> get svc,endpoints <svc>`; fix `backendRefs` |
| **500 on every route** after applying the security-headers policy | Lua is disabled in the controller | `enableLua: true` in `12-...yaml`, `helm upgrade eg`; read the policy status |
| Admin tool open to everyone, or blocked for everyone | the PROXY-protocol policy is missing or `clientIPDetection` was added | apply `34-...yaml` exactly (no `clientIPDetection`); test with a forged `X-Forwarded-For` |
| Access log shows the NLB's address instead of the client | `ClientTrafficPolicy` not applied / not attached | `kubectl get clienttrafficpolicy -n envoy-gateway-system` |
| `server: envoy` header visible | `EnvoyPatchPolicy` not `Programmed` or `enableEnvoyPatchPolicy` off | read its status; `12-...yaml` |
| Users get **429** | the per-IP bucket (30/s) is exceeded, typically many users behind one address | raise `requests` in `38-rate-limit.yaml` after checking the real per-IP peak |
| WebSocket sessions drop after ~minutes | idle timeout | `idleTimeout` in `34-...yaml` (3600 s) and the NLB idle timeout |
| NLB not created / no address | AWS Load Balancer Controller missing or the annotations rejected | `kubectl -n envoy-gateway-system describe svc <envoy service>`; controller logs |

## D. Policy (step 8 only)

| Symptom | Likely cause | Fix |
|---|---|---|
| "connection refused/reset" from one service to another after enforcing | the caller is not in the callee's `MeshTLSAuthentication` | Grafana *Denials by workload and caller* names it; add it (`--extra-caller`) and re-apply; roll back with `accessPolicy: audit` |
| Everything denied for a service | its `Server` has no `AuthorizationPolicy` (generator prints `NOTE: ... no allowed callers`) | add the missing policy |
| Policy seems ignored | the `Server` selector matches no pods, or the pods have no proxy | `kubectl describe server`; `check_policy_coverage.py` |

## E. Monitoring

| Symptom | Likely cause | Fix |
|---|---|---|
| Dashboards say **No data** | proxies not scraped (`50-...yaml` not applied), or no meshed pods yet, or wrong data source | `kubectl -n monitoring-ns get vmpodscrape linkerd-proxy`; in Explore run `count(request_total)` |
| Only some pods appear | those pods are not meshed | the *Pods without a proxy* panel and alert |
| mTLS panels look low | mostly kubelet probes (no identity by design) | read the **count** and the breakdown by caller identity, not a percentage ([08](08-step-7-monitoring-and-logs.md)) |
| `log_type:edge_access` returns nothing | Vector's `process_logs` is not tagging yet | update Vector values; filter by `kubernetes.container_name:"envoy"` meanwhile |
| vmagent logs "skipping duplicate scrape target" | a scrape object matches a pod more than once | restrict it to one container/port with a `keep` relabel rule |

## F. If you must roll back fast

1. **One service:** remove its annotations, roll it.
2. **The edge:** point DNS back to the old load balancer ([07](07-step-6-dns-and-cutover.md), section 7.5) *if you still have one.*
3. **Linkerd itself:** the proxies sit in the traffic path of every meshed pod (including the edge proxies), so never uninstall the
   control plane while pods are still meshed; un-mesh services one at a time first ([10.10](10-operations.md)).
