# 9. Step 8 (optional, recommended): restrict who may call whom

**Goal:** each service accepts calls only from the services (and the edge) that really call it. **Status in production:** *not
applied yet.* Production runs the permissive default (`defaultInboundPolicy: all-unauthenticated`), so mTLS is active but nothing
is blocked. This document is the safe procedure and the tools to do it; the policy generator was run against the live cluster but
its output has not been enforced.

Do this only after steps 1-7 are healthy and every service has its own ServiceAccount (step 5).

## 9.1 The model in plain words

Linkerd authorisation has three kinds of objects. Read them as a sentence: *"The **Server** `orders-3008` accepts connections only
from callers matching the **MeshTLSAuthentication** `callers-orders`, as stated by the **AuthorizationPolicy** `orders-3008-callers`."*

| Object | Means | Example |
|---|---|---|
| `Server` | "this port of the pods matching these labels", plus the default for anyone not otherwise allowed (`accessPolicy`) and the protocol | `orders-3008` = port 3008 of the pods labelled `app: orders` |
| `MeshTLSAuthentication` | a list of **identities** (ServiceAccount-based names) | `prod-backend-users-service.prod-app-ns.serviceaccount.identity.linkerd.cluster.local` |
| `AuthorizationPolicy` | connects a `Server` to the authentications that are **allowed** | `orders-3008-callers` |

`accessPolicy` on a Server decides what happens to a caller *no policy allows*:

| `accessPolicy` | Behaviour |
|---|---|
| `all-unauthenticated` | allow everyone (what production has) |
| `audit` | allow everyone **but record** what would have been denied (metrics `inbound_*_authz_*`, "Denials" panels) |
| `deny` | **block** it |

Kubelet health probes are always allowed by Linkerd, whatever the policy.

A template with all three objects and comments: [`examples/policy-example.yaml`](../examples/policy-example.yaml).

## 9.2 The three stages

| Stage | What | Risk |
|---|---|---|
| **A. Baseline** | every Server allows any identity in the namespace (`*.prod-app-ns...`) plus the gateway | very low; first proof that policies are read at all |
| **B. Least privilege, audit mode** | each Server allows only the callers that **actually call it** (learned from metrics), `accessPolicy: audit` | none: nothing is blocked, you only watch |
| **C. Enforce** | same as B with `accessPolicy: deny` | real: a missed caller gets connection errors |

Go A → B → C. Stay in B for **at least 30 days** so monthly jobs show up in the call graph.

## 9.3 Stage B in commands

```bash
# 1. Who calls whom? Reads the callers' identities from the proxies' metrics over 30 days.
python3 scripts/gen_callgraph.py --window 30d > callgraph.json
#    stderr prints one line per service: "orders <- users, wallet-main, ..."; warns if some callers still use the 'default' SA.

# 2. The identity of the edge gateway (its ServiceAccount name is generated)
GW_SA=$(kubectl -n envoy-gateway-system get pod -l gateway.envoyproxy.io/owning-gateway-name=prod-edge-gateway \
        -o jsonpath='{.items[0].spec.serviceAccountName}')

# 3. Generate the policy (audit mode). Add callers the graph cannot see (rare jobs) with --extra-caller deployment=serviceaccount
python3 scripts/gen_linkerd_policy.py --gateway-sa "$GW_SA" --callgraph callgraph.json --access-policy audit \
  > manifests/40-linkerd-policy.yaml

# 4. Review it (every "NOTE: ... has no allowed callers" line must be understood), then apply
kubectl apply -f manifests/40-linkerd-policy.yaml
```

What the generator builds (from the live Services):

* a `Server` for every Service port (Redis is `opaque`; the rest `unknown` = detect);
* the HTTP port of publicly routed services also accepts the **gateway** identity; gRPC and other ports never do;
* Redis accepts only the services that use it; phpMyAdmin accepts only the gateway;
* RedisInsight has no Server on purpose: it is reached by `kubectl port-forward`, which arrives from the node and cannot
  present an identity.

## 9.4 Watch, then enforce

```bash
python3 scripts/check_policy_coverage.py       # pods without a proxy, ports without a Server, Servers still in audit/permissive mode
```

In Grafana **Linkerd / Security & control plane**: *Denials by workload and caller* and *Allowed vs denied* show what audit
mode **would** have blocked. Every row there is either a legitimate caller you must add (`--extra-caller`, regenerate,
re-apply) or an unexpected one to investigate. Alerts `LinkerdPolicyDenials` / `LinkerdTcpPolicyDenials` fire on sustained denials.

When audit shows nothing unexpected for a full period (a month):

```bash
python3 scripts/gen_linkerd_policy.py --gateway-sa "$GW_SA" --callgraph callgraph.json --access-policy deny \
  > manifests/40-linkerd-policy.yaml
kubectl apply -f manifests/40-linkerd-policy.yaml
```

Do it **one service at a time** by applying only that service's objects first (split the file), starting with a low-risk
service, and keep the previous file for instant rollback.

## 9.5 After applying, read the status

```bash
kubectl -n prod-app-ns get server,authorizationpolicy,meshtlsauthentication
kubectl -n prod-app-ns describe server <name>      # events and status show whether the Server selected any pods
```

Lesson from this rollout: after applying policies always read their `status` and send a real request; "applied" only means
"stored".

## 9.6 Optional extra layer: Kubernetes NetworkPolicy

[`manifests/42-networkpolicy-ingress-baseline.yaml`](../manifests/42-networkpolicy-ingress-baseline.yaml) is **not Linkerd**: it is a
plain Kubernetes NetworkPolicy that drops ingress traffic to the application namespace unless it comes from the same namespace,
the edge, monitoring or the Linkerd control plane. Why both: a pod that is **not meshed** (or whose proxy was bypassed)
enforces no Linkerd policy at all, but the network layer still drops traffic that is not allowed. It restricts **ingress only**
(applications call databases, Kafka and external APIs by name, so egress is not restricted). Requires the VPC CNI
network-policy agent. Apply it in a window and watch for 5xx; roll back with `kubectl delete -f` of the same file.

## 9.7 Pitfalls

* A service still on the `default` ServiceAccount shares an identity with other such services: you cannot separate them.
* The call graph only knows callers that were *seen*. Rare jobs (month-end reports) must be added by hand until seen.
* Redis, Kafka, MySQL clients are TCP, not HTTP: the policy applies to the TCP connection (identity-based), not per request.
* Removing a caller from policy breaks it immediately in `deny` mode: the symptom is "connection refused/reset" in the *caller's*
  log and a denial row in Grafana.

Next: [10-operations.md](10-operations.md).
