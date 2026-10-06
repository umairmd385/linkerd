# 13. Lessons learned (real incidents from the production rollout, October 2026)

Each entry: what happened, why, what we do now. Read this before changing anything important; every item cost real time.

## Certificates and the control plane

1. **The Linkerd CLI install gave one-year certificates and could not be upgraded to cert-manager.** `linkerd install` had been
   run by hand; both certificates expired in 12 months, a calendar-date outage. `linkerd upgrade` has no external-issuer option
   and refuses a new trust anchor ("x509: certificate signed by unknown authority"). *Now:* install with Helm and cert-manager
   from the start (steps 1-2). Replacing the control plane was only safe because **no application pod was meshed yet**; doing
   it later is a planned, multi-step rotation.
2. **A single proxy-injector let six pods start without a proxy.** During a `helm upgrade` the injector restarted; pods created in
   that minute were created unmeshed because the webhook's failure policy is `Ignore`. They ran without mTLS for minutes.
   *Now:* `controllerReplicas: 2`, an alert (`PodWithoutLinkerdProxy`), a script that finds and restarts such pods, and the rule to
   check `kubectl -n linkerd get pods` before any upgrade.
3. **The Gateway API CRDs were owned by whoever installed them first.** The Linkerd CLI had installed Gateway API v1.6.2 with a
   `safe-upgrades` admission policy, so Envoy Gateway had to install *only its own* CRDs. *Now:* one owner for the Gateway API
   CRDs (the Envoy Gateway CRD chart, standard channel); never overlay two bundles.

## The edge

4. **`clientIPDetection` made the admin IP allow-list bypassable.** Adding it makes Envoy trust the caller-controlled
   `X-Forwarded-For` header. Found by testing: from an allowed address, `X-Forwarded-For: 1.2.3.4` was *refused*, meaning the
   header decided access. *Now:* never set it; the test with a forged header is in step 4.
5. **A policy that was "applied" returned 500 on every route.** Envoy Gateway has Lua disabled by default; the security-headers
   policy was accepted but broken. *Now:* `enableLua: true`, and the habit of reading each object's `status` and sending a real
   request after `kubectl apply`.
6. **YAML nesting error.** In the first `EnvoyProxy` draft the `affinity` block sat at the wrong level. The server-side dry run
   rejected it. *Now:* `kubectl apply --dry-run=server` on every file; it rejects unknown fields.
7. **The per-IP "rate limit" was never per IP.** The old Istio filter was one shared bucket. We chose true per-IP limiting
   (30 req/s). Measured: Envoy's local limiter keeps a bucket per worker thread per pod, so the real ceiling is a multiple of 30/s
   (about 120/s per pod). Treat it as a flood guard. A strict quota needs the global rate limiter (Redis).

## Rolling services into the mesh

8. **Converting everything in one push stalled the platform for about ten minutes.** Waves (small first, money path last) with a
   verification after each are the safe way. A rolling update needs capacity for one extra pod per service.
9. **A Redis restart crash-looped the backends.** Redis was restarted to add its proxy. Our applications exit on any uncaught Redis
   error, some used per-pod DNS (`prod-redis-0...`) that exists only while the pod is Ready, and the node was flapping. About
   twelve minutes of crash loops. *Now:* use the stable service name; restart Redis only in a quiet period; pin image digests
   (`redis:latest` with `imagePullPolicy: Always` can silently upgrade on restart); handle Redis errors in the application.
10. **Autoscalers fight surge tests.** KEDA scaled a service back while we were trying to surge it, so the old pod had to be marked
    (`controller.kubernetes.io/pod-deletion-cost`) to be the one removed.
11. **Karpenter consolidated away the node holding Redis.** An empty-looking, cheaper node was removed during a quiet period.
    *Now:* `karpenter.sh/do-not-disrupt: "true"` on Redis.
12. **`kubectl rollout restart` does not upset ArgoCD.** The restart annotation is not in Git and is not treated as drift, so
    restarting deployments one by one is a safe way to pick up new proxy settings.

## Capacity and sizing

13. **Nodes were at 96-100% memory before the first proxy was added.** Free memory *first* (we removed unused Istio add-ons,
    about 1.3 GiB) or the mesh competes with the applications.
14. **The proxy request was 4× too large.** 100m CPU was copied from the old sidecar; 26 proxies *requested* 2.6 vCPU and *used*
    0.2 (peak 90m). The four fixed nodes were 80-96% reserved but 7-25% busy, so Karpenter added a fifth node. Lowering the request
    to 25m (no CPU limit) freed about 2 vCPU and the extra node left. *Rule:* size from measured usage (`kubectl top pod
    --containers`); keep no CPU limit on the proxy.

## Observability

15. **The mTLS percentage looked terrible (about 1%) and was misleading.** About 99% of the requests the proxies saw were kubelet
    probes, which cannot carry an identity. Real traffic *was* mTLS (the caller identities, including the edge gateway, were in
    the access log). *Now:* dashboards count authenticated requests and break them down by caller; the document explains it.
16. **Certificate alerts that could never fire.** A scrape for cert-manager used the wrong port name, matched nothing, and the
    `certmanager_*` metrics did not exist, so the expiry alerts were silent. *Now:* the correct scrape (`53-...`), and the alert
    `LinkerdCertMetricsMissing` that fires when the metrics are missing. *Rule:* after writing an alert, check its metric exists.
17. **A scrape that matched each pod several times** flooded vmagent with "duplicate scrape target" errors and tripped a
    log-volume alert. *Rule:* restrict to one container and one port with a `keep` relabel.
18. **Dashboards' "No data" is usually a scrape or label problem,** not Grafana. Check `count(request_total)` first.
19. **`${VAR}` inside a Vector VRL string is stored literally;** use `get_env_var("VAR")`. Every log had the node name
    `${VECTOR_SELF_NODE_NAME}` until fixed.

## Security

20. **Application logs can leak sensitive values that the mesh never sees.** The mesh's own logs contain no bodies or headers, but the
    applications' logs are only as clean as the code. Mask sensitive patterns in the log pipeline (Vector) before storage, and fix
    the application so it does not print them.
21. **Lua and patch policies are code execution at the edge.** Keep RBAC for `envoyextensionpolicies` and `envoypatchpolicies`
    to platform admins only.
22. **The mesh was mTLS but not restrictive.** Encryption and identity are in place; *authorisation* is still permissive.
    Treat step 8 as unfinished work, not an optional extra, if you need to prove "only these services may call this one".

## Habits that paid off

* `kubectl apply --dry-run=server` and `helm template` before every change.
* Reading object **status** (not just apply output) after every change.
* Testing a new edge with a `Host` header **before** touching DNS.
* One change per upgrade; one service per step; baseline numbers before and after.
* Keeping a rollback for every step in the same document as the step.
