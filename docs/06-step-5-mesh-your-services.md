# 6. Step 5: put your services into the mesh

**Goal:** each application pod gets a `linkerd-proxy` and its own identity, one service at a time, with no downtime.
**Impact:** pods are replaced by a normal rolling update. **Do this in small waves**, not all at once.

## 6.1 What "meshing a service" means

Linkerd adds its proxy to a pod only if the pod carries the annotation `linkerd.io/inject: enabled` **when the pod is created**.
So meshing a service = change the pod template (add the annotations + a ServiceAccount) → Kubernetes does a rolling update →
the new pods are created *with* the proxy. Existing pods are never changed in place.

What the injector adds to each pod:

| Container | Type | Job |
|---|---|---|
| `linkerd-init` | init container, runs once | sets firewall (iptables) rules so all traffic goes through the proxy |
| `linkerd-proxy` | **native sidecar** (an init container that keeps running) | the proxy itself, ports 4143 (inbound), 4140 (outbound), 4191 (admin and metrics) |

`kubectl get pods` then shows `2/2` for a pod with one application container: application + proxy.

## 6.2 What you add to each service

```yaml
# pod template (for a Helm chart: the values file's podAnnotations / serviceAccountName)
serviceAccountName: prod-backend-orders-service      # its OWN identity (step 6.3)
podAnnotations:
  linkerd.io/inject: enabled                         # add the proxy
  config.linkerd.io/access-log: json                 # proxy writes one JSON line per request (becomes log_type:mesh_access)
```

A full commented template: [`examples/mesh-a-service.yaml`](../examples/mesh-a-service.yaml). For a chart-based service in
production this is the `deployment:` block of `services/<service>/values.yaml` in the GitOps repository.

You do **not** need to change the application, its ports, its Services, or its health checks. The HTTP and gRPC health probes
keep working: Linkerd lets kubelet probes through.

## 6.3 One ServiceAccount per service (do this first)

All pods use the namespace's `default` ServiceAccount unless told otherwise, which would give **every** service the **same**
mesh identity (`default.prod-app-ns...`). Linkerd authorises callers by identity, so "orders may call wallets but
reports may not" can only be expressed if each service has its own.

[`manifests/41-serviceaccounts.yaml`](../manifests/41-serviceaccounts.yaml) creates one ServiceAccount per service
(`prod-backend-<name>-service`, ...). [`43-serviceaccounts-redis-phpmyadmin.yaml`](../manifests/43-serviceaccounts-redis-phpmyadmin.yaml)
does the same for the three workloads that are not built from the shared chart (Redis, RedisInsight, phpMyAdmin).
They set `automountServiceAccountToken: false` because the applications do not call the Kubernetes API (verify for your own
services: nothing should read `/var/run/secrets/kubernetes.io/serviceaccount`). Linkerd uses its own projected token, so the mesh
is unaffected.

```bash
kubectl apply -f manifests/41-serviceaccounts.yaml        # BEFORE changing any deployment: a missing SA blocks pod creation
kubectl apply -f manifests/43-serviceaccounts-redis-phpmyadmin.yaml
```

To create one for a new service: copy a block from `41-...yaml` (name = the deployment name).

## 6.4 Order of conversion

Convert low-risk services first and the money path last. Production order:

1. a canary with no dependants (`charting-library`)
2. small services (`referral`, `launchpad`, `p2p`; then `vip`, `staking`, `graph`, `email`, `liquidity`, ...)
3. user-facing services (`frontends`, `socket`, `users`)
4. critical last (`orders`, `matching-engine`, `wallets`)
5. stateful components (`redis`) in a quiet period, one at a time

For each service:

```bash
# 0. baseline: pod is healthy, note its restart count
kubectl -n prod-app-ns get pods -l app=<label>
# 1. apply the change (commit to the GitOps repo, or kubectl apply for a plain manifest)
# 2. watch the rolling update
kubectl -n prod-app-ns rollout status deploy/<name>
# 3. verify (below)
```

Do not start the next service until the previous one is verified. Only convert services whose Deployment uses a rolling update
with `maxUnavailable: 0` if you need zero downtime (production services use `maxSurge: 1, maxUnavailable: 0`).

## 6.5 Verify each service

```bash
NS=prod-app-ns
# 1. the proxy is there and the pod is Ready (2/2)
kubectl -n $NS get pod -l app=<label>
kubectl -n $NS get pod <pod> -o jsonpath='{.spec.initContainers[*].name}{"\n"}'        # linkerd-init linkerd-proxy
# 2. its own identity
kubectl -n $NS get pod <pod> -o jsonpath='{.spec.serviceAccountName}{"\n"}'
# 3. Linkerd's own check of the data plane
linkerd check --proxy -n $NS
# 4. no new errors in the application, no new restarts
kubectl -n $NS logs deploy/<name> -c <app-container> --since=5m | tail
```

And in Grafana, dashboard **Linkerd / Overview** (see [08](08-step-7-monitoring-and-logs.md)): the service appears in the
*Workloads* table with a success rate near 100%. Calls from another meshed service show up under **mTLS requests**, with the
caller's identity in **Linkerd / Security & control plane → mTLS requests/s by caller identity**.

Whole-namespace check, after a wave:

```bash
# every running pod in the namespace must have a proxy
kubectl -n $NS get pods -o json | python3 -c "
import json,sys
for p in json.load(sys.stdin)['items']:
    names=[c['name'] for c in p['spec'].get('initContainers',[])+p['spec']['containers']]
    if p['status']['phase']=='Running' and 'linkerd-proxy' not in names: print('NO PROXY:', p['metadata']['name'])"
```

The alert `PodWithoutLinkerdProxy` does the same continuously.

## 6.6 Things that go wrong (and are easy to prevent)

* **A pod started without a proxy.** This happens if the pod is created while the proxy-injector is down. With 2 injector
  replicas it should not; if it does, restart the Deployment (`kubectl rollout restart`). Check with the script above.
* **Stateful or non-HTTP services (Redis, Kafka, MySQL).** Their ports must be in the *opaque ports* list in the values file
  (`proxy.opaquePorts`), or Linkerd may try to read them as HTTP. Redis also gets the pod annotation
  `config.linkerd.io/opaque-ports: "6379"` as a belt-and-braces measure. **Restarting Redis drops every application's
  connection**: our apps exit on an unhandled Redis error, so do it in a quiet period.
* **Proxy resources and autoscaling.** The proxy's CPU request is part of the pod's autoscaling denominator ([03](03-step-2-install-linkerd.md)).
  After meshing, watch the HPA/KEDA targets for a day.
* **Database/Kafka clients that connect before the proxy is ready.** The native sidecar starts before the application, which
  avoids this. Do not switch `nativeSidecar` off.
* **Jobs and CronJobs.** A Job pod never "finishes" while the proxy runs. On native sidecars Kubernetes stops the proxy when the
  job container ends, so this works; do not use the old `linkerd-await` workaround.

## 6.7 Roll back one service

Remove the two annotations (and optionally the ServiceAccount line) and roll the Deployment again: the new pods start without
the proxy. Nothing else is affected: other services reach it over plain connections when it is not meshed (while policy is
permissive).

Next: [07-step-6-dns-and-cutover.md](07-step-6-dns-and-cutover.md).
