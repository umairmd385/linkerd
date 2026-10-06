# 10. Operations: running Linkerd and the edge day to day

## 10.1 The five-minute health check (daily, or when something feels wrong)

```bash
linkerd check                                             # control plane + certificates + webhooks: all green
kubectl -n linkerd get pods                               # 6 Running (2 identity, 2 destination, 2 proxy-injector)
kubectl -n linkerd get certificate                        # both READY True
kubectl -n envoy-gateway-system get pods,gateway          # 1 controller + 2-5 proxies Ready; Gateway Programmed True
kubectl -n prod-app-ns get pods | grep -v "2/2"      # nothing unexpected (every app pod shows 2/2 = app + proxy)
```

Then Grafana **Linkerd / Overview** (success rate ~100%, no policy denials) and **Linkerd / Security & control plane**
(issuer countdown bouncing between ~25 and 48 h, "Pods without a proxy" = 0, control-plane pods ready = 6).

## 10.2 Add a new service

1. **ServiceAccount**: copy a block in `manifests/41-serviceaccounts.yaml` (name = the deployment name) and apply it first.
2. **Mesh it**: add `serviceAccountName`, `linkerd.io/inject: enabled` and `config.linkerd.io/access-log: json`
   (`examples/mesh-a-service.yaml`). Roll out; verify as in [06](06-step-5-mesh-your-services.md).
3. **Non-HTTP ports**: if the service speaks raw TCP on a port not in `proxy.opaquePorts`, add that port to
   `11-linkerd-control-plane-values.yaml` **and** `helm upgrade` ([10.5](#105-change-a-setting-or-upgrade-linkerd)).
4. **Expose it publicly (only if needed)**: add an `HTTPRoute` (`examples/httproute.yaml`) in the service's namespace; if the
   namespace is new add it to the `ReferenceGrant` in `31-...yaml`. Check `Accepted: True` and test with the `Host` header.
5. **Admin tool?** Add a `SecurityPolicy` with the allowed IPs (copy a block of `37-...yaml`).
6. **Policy** (if you enforce it): regenerate and review ([09](09-step-8-security-policy.md)); until then the default is permissive.
7. **Alerts and dashboards** pick the service up automatically (they select by label/namespace).

## 10.3 Add or change a public hostname

Edit `35-httproutes.yaml` (or add `examples/httproute.yaml`), then `kubectl apply`. Update, if relevant: the allowed origins in
`32-envoy-extension-policy-security.yaml`, the IP allow-list in `37-...yaml`, DNS (Route 53 alias to the NLB), and the ACM
certificate (must cover the new name).

## 10.4 Change the IP allow-list or the rate limit

* IPs: edit the `clientCIDRs` in `37-security-policy-admin.yaml` (all four objects) and apply. Takes effect in seconds.
* Rate limit: edit `requests` in `38-rate-limit.yaml` and apply. Check the real per-IP peak first in the edge log; users behind
  one shared address share a bucket.

## 10.5 Change a setting or upgrade Linkerd

Linkerd (edge channel) releases frequently; each chart version maps to one `edge-YY.M.N` release. **Upgrade deliberately, not
automatically.**

```bash
helm repo update
helm show values linkerd-edge/linkerd-control-plane --version <new> > /tmp/new-values.yaml    # diff against ours for renamed keys
helm upgrade linkerd-crds linkerd-edge/linkerd-crds --version <new> -n linkerd
helm upgrade linkerd-control-plane linkerd-edge/linkerd-control-plane --version <new> -n linkerd \
  -f manifests/11-linkerd-control-plane-values.yaml --set-file identityTrustAnchorsPEM=/tmp/linkerd-ca.crt --wait --timeout 6m
linkerd check
linkerd check --proxy          # lists meshed pods still running an OLDER proxy
```

Rules that came from real incidents:

* **Change one thing per upgrade.** Mixing a replica change with a version bump makes failures hard to trace.
* **Only changing `controllerReplicas`** (or other values that do not change the pod template) restarts nothing.
* **Values that change the injector's defaults (proxy size, opaque ports, log level)** apply **only to pods created after the
  upgrade**. Existing pods keep the old values until they are recreated. To roll the services one at a time:
  `python3 scripts/resize_proxy_requests.py` (dry run) then `--go`. It restarts deployments whose proxy CPU request is still above
  the target, waits for each rollout and for every pod in the namespace to be Ready, and stops at the first problem. It
  skips the matching engine on purpose (it may hold the order book in memory): restart that and **Redis** yourself in a quiet
  period. `kubectl rollout restart` does not make ArgoCD revert anything (the restart annotation is not in Git).
* **Two replicas of every control-plane component** mean an upgrade does not leave a window where new pods start without a proxy.
  Check first: `kubectl -n linkerd get pods` shows 2/2 ready of each.
* **Pod-template changes need a roll of the services** to pick up a new proxy version; schedule it like a deployment.

## 10.6 Upgrade Envoy Gateway

```bash
helm template eg-crds oci://docker.io/envoyproxy/gateway-crds-helm --version <new> \
  --set crds.gatewayAPI.enabled=true --set crds.gatewayAPI.channel=standard --set crds.envoyGateway.enabled=true \
  | kubectl apply --server-side -f -
helm upgrade eg oci://docker.io/envoyproxy/gateway-helm --version <new> -n envoy-gateway-system --set crds.enabled=false \
  -f manifests/12-envoy-gateway-values.yaml --wait
kubectl -n envoy-gateway-system get pods        # proxies are replaced by a rolling update
```

Read the release notes for removed fields in the policy CRDs. After the upgrade re-read the **status** of every policy and test
a real request (section 5.4/5.5).

## 10.7 Certificates

| Certificate | Lifetime | Renewal | What to watch |
|---|---|---|---|
| Pod certificates | 24 h | Linkerd, automatic, in the proxy | nothing; if every pod loses mTLS check identity pods |
| **Identity issuer** `linkerd-identity-issuer` | 48 h | cert-manager, ~25 h before expiry | dashboard countdown (25-48 h); alert `LinkerdIdentityIssuerNotRenewing` |
| **Trust anchor** `linkerd-trust-anchor` | 10 years | see below | alert `LinkerdTrustAnchorExpiring` (90 days) |

**The trust anchor in 2035-2036.** cert-manager is set to renew the root one year before it expires. Because the key is kept,
the chain keeps working at that moment, but Linkerd's *configured copy* of the root (`identityTrustAnchorsPEM`, copied into every
proxy at pod creation) is still the old certificate with the old end date. Before that date: export the renewed root
(`kubectl -n linkerd get secret linkerd-trust-anchor -o jsonpath='{.data.ca\.crt}' | base64 -d`), `helm upgrade` with
`--set-file identityTrustAnchorsPEM=...`, then roll every meshed workload. Rehearse this in a test cluster well before 2035 and
follow the Linkerd documentation for your version; this procedure is the plan, not something yet run. Put the date in the team
calendar (see step 1).

## 10.8 Scaling and capacity

* **Edge proxies**: autoscale 2-5 pods at 80% CPU (`envoyHpa` in `30-envoyproxy.yaml`). Keep at least 2 (alert `EdgeGatewayDegraded`).
* **Control plane**: 2 replicas each; raise `controllerReplicas` to 3 for very large clusters.
* **Per-pod proxy**: ~25m CPU / 32Mi request. If `kubectl top` shows a proxy regularly above its request, raise it for the whole
  namespace via the values file, or for one workload with the pod annotations `config.linkerd.io/proxy-cpu-request` and
  `config.linkerd.io/proxy-memory-request`.
* **Node memory/CPU**: every meshed pod costs the proxy's requests; check `kubectl top nodes` and the *requests* per node
  (`kubectl describe node | grep -A8 "Allocated resources"`) before adding more services.

## 10.9 Protect critical pods from node consolidation

Karpenter removes under-used nodes. Pods that must not be moved without warning (Redis) carry
`karpenter.sh/do-not-disrupt: "true"`. Add the same to any other stateful pod that cannot tolerate a restart.

## 10.10 Remove a service from the mesh / remove everything

* One service: remove its annotations and roll it.
* All of Linkerd: first un-mesh every workload (so no pod depends on the proxy), then
  ```bash
  helm uninstall linkerd-control-plane -n linkerd
  helm uninstall linkerd-crds -n linkerd            # also deletes every Server/AuthorizationPolicy
  kubectl delete -f manifests/10-cert-manager-linkerd-identity.yaml
  kubectl delete namespace linkerd
  ```
* The edge: delete the routes, policies, gateway and `EnvoyProxy`, then `helm uninstall eg eg-crds -n envoy-gateway-system`
  (the NLB disappears with the Gateway).

Next: [11-troubleshooting.md](11-troubleshooting.md).
