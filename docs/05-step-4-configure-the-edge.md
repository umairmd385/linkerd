# 5. Step 4: configure the public edge (Gateway, routes, policies)

**Goal:** a new, working public entry point exists **in parallel** with whatever serves traffic today, and you have proven it
works **without changing DNS**. **Impact:** creates one new AWS load balancer (a few dollars a month); no user traffic yet.

## 5.1 The objects and how they fit

```
GatewayClass "eg"  ──points at──>  EnvoyProxy "prod-edge"           (30, 31)  how the proxies and the NLB are built
      │
Gateway "prod-edge-gateway"  (listeners :80 and :443)             (31)      the front door
      │  <── attached by parentRefs
HTTPRoute (one per hostname/path)  ──backendRefs──> Service           (35)      where each hostname/path goes
      │
Policies attached to the Gateway or to a route:
  ClientTrafficPolicy  nlb-proxy-protocol        (34)  understand the NLB's PROXY header → real client IP
  EnvoyExtensionPolicy security-headers-and-cors  (32)  add security headers, strict CORS (Lua)
  EnvoyPatchPolicy     server-header-pass-through (33)  hide the "server: envoy" header
  BackendTrafficPolicy per-ip-rate-limit          (38)  30 requests/s per client IP
  BackendTrafficPolicy ...-retry                  (36)  retries on two API routes
  SecurityPolicy       admin-ip-allowlist         (37)  admin tools only from office/VPN addresses
```

## 5.2 What each file does (apply order)

| # | File | Kind(s) | What it does and why |
|---|---|---|---|
| 1 | [`30-envoyproxy.yaml`](../manifests/30-envoyproxy.yaml) | `EnvoyProxy prod-edge` | **Builds the proxy pods and the NLB.** 2 replicas minimum, autoscaled to 5 at 80% CPU. Pod annotations `linkerd.io/inject: enabled` (the edge pods join the mesh, so edge → service is mTLS) and `config.linkerd.io/skip-inbound-ports: "10080,10443"` (the NLB's PROXY-protocol traffic cannot go through Linkerd's HTTP detection). The `Service` annotations make the AWS controller create an internet-facing NLB with PROXY protocol, TLS 1.3 and **your ACM certificate** (replace the ARN). Also defines the JSON access log written to stdout. |
| 2 | [`31-gatewayclass-gateway.yaml`](../manifests/31-gatewayclass-gateway.yaml) | `GatewayClass eg`, `Gateway prod-edge-gateway`, `ReferenceGrant allow-httproutes` | The **front door**: listener `http` on 80 and `https` on 443 (both plain HTTP inside, because TLS ends at the NLB; the 443 listener exists so the NLB gets a 443 port). The `ReferenceGrant` allows routes from other namespaces (`prod-app-ns`, `argocd`, `jenkins`, `monitoring-ns`) to attach to this Gateway; **add a namespace here when you add a route from a new one**, or the route silently fails with `RefNotPermitted`. |
| 3 | [`34-client-traffic-policy.yaml`](../manifests/34-client-traffic-policy.yaml) | `ClientTrafficPolicy` | `enableProxyProtocol: true`: without it every connection from the NLB is rejected and the real client IP is lost. 3600 s idle timeout for long-lived WebSockets. **Never add `clientIPDetection`** (see below). |
| 4 | [`32-envoy-extension-policy-security.yaml`](../manifests/32-envoy-extension-policy-security.yaml) | `EnvoyExtensionPolicy` | A small **Lua** script run on every request/response: adds HSTS, `X-Frame-Options: DENY`, CSP `frame-ancestors 'none'`, `nosniff`, referrer policy; removes `X-Powered-By` and `Server`; **strict CORS** (an `Origin` that is not one of the four trusted origins gets `access-control-allow-origin: null`). Edit the origin list for your own domains. Needs `enableLua` (step 3). |
| 5 | [`33-envoy-patch-policy-server-header.yaml`](../manifests/33-envoy-patch-policy-server-header.yaml) | `EnvoyPatchPolicy` | Envoy re-adds `server: envoy` after the Lua script removed it; this raw patch sets `server_header_transformation: PASS_THROUGH` so it does not. Needs `enableEnvoyPatchPolicy`. |
| 6 | [`38-rate-limit.yaml`](../manifests/38-rate-limit.yaml) | `BackendTrafficPolicy` | **Local rate limit:** each distinct client IP gets its own bucket of 30 requests per second; beyond that Envoy answers **429**. Treat it as a flood guard, not a precise quota: Envoy keeps a bucket per worker thread per proxy pod, so the real ceiling per IP is a multiple of 30/s (about 120/s per pod measured). Users behind one shared IP (office, mobile carrier NAT) share a bucket. A strict global quota needs Envoy Gateway's global rate limiter (needs Redis). |
| 7 | [`35-httproutes.yaml`](../manifests/35-httproutes.yaml) | `HTTPRoute` × 11 | **Hostname + path → Service.** Example: `exchange.example.com` → `prod-frontend-user-service:80`; `api.example.com` with prefixes `/users`, `/orders`... → each backend service. Routes live in the namespace of the target Service. |
| 8 | [`36-backend-traffic-policy.yaml`](../manifests/36-backend-traffic-policy.yaml) | `BackendTrafficPolicy` × 2 | Retries (3 tries, 5 s each, on gateway errors / connect failures / refused streams) for the `/users` and `/notification` routes only. **Never add retries to a route that creates money-moving records**: a retried request can be applied twice. |
| 9 | [`37-security-policy-admin.yaml`](../manifests/37-security-policy-admin.yaml) | `SecurityPolicy` × 4 | **Admin tools** (ArgoCD, Jenkins, Grafana, phpMyAdmin) answer only to the five listed office/VPN addresses (`clientCIDRs`, default action `Deny`). It relies on the PROXY protocol policy so the address it sees is the real client's. |

Replace before applying on a different environment: the ACM ARN (30), hostnames (35, 37), allowed origins (32), allowed IPs (37),
`ReferenceGrant` namespaces (31).

## 5.3 Apply

```bash
# 1 + 2: proxies, NLB, Gateway
kubectl apply -f manifests/30-envoyproxy.yaml
kubectl apply -f manifests/31-gatewayclass-gateway.yaml

# 3-6: policies that attach to the Gateway
kubectl apply -f manifests/34-client-traffic-policy.yaml
kubectl apply -f manifests/32-envoy-extension-policy-security.yaml
kubectl apply -f manifests/33-envoy-patch-policy-server-header.yaml
kubectl apply -f manifests/38-rate-limit.yaml

# 7-9: routes and route-level policies (the target namespaces must exist)
kubectl apply -f manifests/35-httproutes.yaml
kubectl apply -f manifests/36-backend-traffic-policy.yaml
kubectl apply -f manifests/37-security-policy-admin.yaml
```

Wait for the NLB (2-3 minutes) and read its address:

```bash
kubectl -n envoy-gateway-system get gateway prod-edge-gateway
NLB=$(kubectl -n envoy-gateway-system get gateway prod-edge-gateway -o jsonpath='{.status.addresses[0].value}')
echo $NLB          # k8s-envoygat-envoyenv-....elb.us-east-1.amazonaws.com
```

## 5.4 Read the status of every object (do not trust `apply` output)

`kubectl apply` only says the object was stored. **Whether Envoy Gateway accepted it is in `status`.** Two real bugs in this
rollout were invisible without reading status (see below).

```bash
kubectl get gatewayclass,gateway -A                                    # ACCEPTED True, PROGRAMMED True
kubectl get httproute -A                                               # all hostnames listed
kubectl get httproute -A -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.parents[0].conditions[?(@.type=="Accepted")].status}{"\n"}{end}'
kubectl get clienttrafficpolicy,backendtrafficpolicy,securitypolicy,envoyextensionpolicy,envoypatchpolicy -A
kubectl -n envoy-gateway-system get pods                               # 2 proxy pods, each 3/3 (linkerd-proxy, envoy, shutdown-manager)
```

Every route and policy must show `Accepted: True`; the `EnvoyPatchPolicy` must show `Programmed: True`.

## 5.5 Test the new edge **without touching DNS**

Send requests straight to the NLB with the hostname in a `Host` header:

```bash
for h in exchange admin chart socket api; do
  printf "%-10s " $h
  curl -s -o /dev/null -m 10 -w '%{http_code}\n' -H "Host: $h.example.com" http://$NLB/
done
```

(Use each service's own health path rather than `/` if `/` returns 404/403 by design, for example the chart service answers
403 on `/`.)

More checks worth doing:

| Test | Command / expectation |
|---|---|
| Security headers | `curl -sI -H 'Host: exchange.example.com' http://$NLB/` shows `strict-transport-security`, `x-frame-options: DENY`, **no** `server` header |
| Strict CORS | send `Origin: https://evil.example`; the response has `access-control-allow-origin: null`. A trusted origin is echoed back |
| WebSocket | open `socket.example.com` Socket.IO handshake through the NLB; expect `101 Switching Protocols` |
| Admin allow-list | from a non-listed address `prod-grafana...` returns **403**; from an allowed one it answers |
| Forged client IP | from an allowed address send `X-Forwarded-For: 1.2.3.4`; the request must still be **allowed** (the header must be ignored) |
| Rate limit | a flood (hundreds of requests/second from one IP) gets some `429`s and recovers after a pause |
| Real client IP in logs | `kubectl -n envoy-gateway-system logs <proxy-pod> -c envoy | tail` shows your real address in `downstream_remote_address` |

## 5.6 Two bugs we hit here (so you can avoid them)

1. **`clientIPDetection` made the allow-list bypassable.** Adding `clientIPDetection` (even `numTrustedHops: 0`) to the
   ClientTrafficPolicy makes Envoy take the client address from the request's `X-Forwarded-For` header, which the caller controls.
   From an allowed address `X-Forwarded-For: 1.2.3.4` was *refused* — meaning the header, not the real address, decided access.
   Fixed by removing the block; Envoy then uses the address from the PROXY header.
2. **Lua was silently off.** The security-headers policy was accepted but every route returned **500** until `enableLua: true`
   was set on the controller (step 3). Read `status` and test a real request after every policy.

## 5.7 Roll back

The edge is additive: `kubectl delete` the files above in reverse order (routes/policies, then gateway, then envoyproxy). Deleting
the `Gateway` deletes the NLB. No DNS has changed so users are unaffected.

Next: [06-step-5-mesh-your-services.md](06-step-5-mesh-your-services.md).
