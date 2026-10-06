# Background 2: Linkerd, what it is, why we chose it, how it works, and the honest case for and against

*Read [background-1-why-a-service-mesh.md](background-1-why-a-service-mesh.md) first. This page is a decision record: anyone
should be able to see **why** the platform uses Linkerd and judge whether the reasons still hold.*

> **How to read the evidence.** Numbers marked **measured** come from our production cluster in October 2026. Statements marked
> **general** are widely published properties of the products, not benchmarks we ran: check them again before relying on them for a
> new decision. Where we did **not** measure something, the text says so.

## 1. What Linkerd is

**Linkerd** is an open-source service mesh. It is a **CNCF graduated project** (the Cloud Native Computing Foundation's highest
maturity level) and is the project that popularised the term "service mesh". It has two parts:

* **The data plane**: a purpose-built proxy, `linkerd2-proxy`, written in **Rust** (general). It is not a general-purpose proxy
  like Envoy; it does only what a mesh needs, which keeps it small and fast.
* **The control plane**: a few small services (identity, destination, policy, proxy-injector) written in Go that configure the
  proxies and sign their certificates.

What Linkerd gives you, in the order we use them:

| Capability | Plain words |
|---|---|
| **Automatic mTLS** | every connection between meshed pods is encrypted and both sides are identified, with no configuration beyond adding the proxy |
| **Golden metrics** | request rate, success rate and latency for every service, with no code changes |
| **Latency-aware load balancing** | each *request* goes to the copy that is currently answering fastest (an "exponentially weighted moving average"), instead of each *connection* being pinned to one copy |
| **Protocol awareness** | understands HTTP/1, HTTP/2 and gRPC, and treats anything else as raw TCP (Redis, MySQL, Kafka) |
| **Authorisation policy** | "this port accepts only these identities" |
| **Retries / timeouts** | available per route (we use them only at the edge, on two routes) |

## 2. Why we adopted a service mesh at all

Our reasons, in priority order (see background 1 for the general case):

1. **Encryption in transit inside the platform** and **proof of who called whom**, needed for regulatory and compliance documentation.
2. **Uniform visibility** of about twenty services without instrumenting each one: success rate and latency per service, and a
   service graph during incidents.
3. **A foundation for restricting access by identity** (policy), which IP-based network rules cannot express in a cluster where
   pod addresses change constantly.

## 3. Why Linkerd rather than what we had (Istio)

We ran Istio (1.29) with sidecars before. The decision to move was driven by **resource use and operational weight**, plus a belief
(not measured by us) that Linkerd adds less latency.

| Factor | Istio as we ran it | Linkerd as we run it | Evidence |
|---|---|---|---|
| **Per-pod proxy size** | sidecar requested **100m CPU** | requests **25m CPU / 32 MiB**; uses ~8m CPU, 6-10 MiB | **measured** (Linkerd); Istio's request is from our old config; **we did not record the Istio sidecars' real memory use, so no before/after memory figure is claimed** |
| **Cluster headroom** | nodes were at **96-100% memory** before the work | after the migration and clean-up, nodes sat at **about 53-84% memory** and the fifth (Karpenter) node was no longer needed | **measured**; part of this is from removing Istio's own add-ons (below) and other tuning, so it is not purely "Linkerd vs Istio sidecar" |
| **Extra components** | istiod, 2 ingress-gateway pods, Prometheus (**943 MiB**), Zipkin (**266 MiB**), Kiali (64 MiB), Jaeger | 6 small control-plane pods (measured: 19m CPU and 252 MiB of memory in use in total, all containers), Envoy Gateway for the edge; metrics go to our existing VictoriaMetrics | **measured** |
| **Moving parts to operate** | many CRDs and several config styles (VirtualService, DestinationRule, EnvoyFilter, ...); a mis-set DestinationRule `ISTIO_MUTUAL` caused 503s during the move | a small set of objects (annotations, ServiceAccounts, optional policy) | experience |
| **Defaults** | strong but many options | secure by default (mTLS automatic), few knobs | general |
| **Latency** | unmeasured | unmeasured | **We never captured a before/after latency baseline.** Linkerd's maintainers and some third-party benchmarks report lower proxy latency and resource use than sidecar Istio (**general**); we have not verified that on our traffic. After the migration the edge answered every route with the same status codes as before. |

So: the *resource* case is supported by our own measurements; the *latency* case is **not** proven for our workload.

## 4. How Linkerd works (a slightly deeper look)

### 4.1 Adding the proxy

You add the annotation `linkerd.io/inject: enabled` to a pod template. When Kubernetes creates the pod, Linkerd's
**proxy-injector** webhook adds:

* `linkerd-init` (init container, runs once): writes iptables rules so that the pod's inbound traffic goes to the proxy's port
  **4143** and outbound traffic to **4140**.
* `linkerd-proxy`: the proxy. In our cluster it is a **native sidecar** (declared as an init container that keeps running,
  Kubernetes 1.29+), so it starts before the application and stops after it.

### 4.2 Identity and certificates

```
 trust anchor (root, 10 years)  ──signs──▶  identity issuer (48 h, renewed by cert-manager)
                                                │ signs
                                                ▼
                                     each pod's certificate (24 h)   ← made by the Linkerd *identity* service
```

At startup the proxy generates its own private key (it never leaves the pod), sends a signing request, and receives a certificate
whose name is built from the pod's ServiceAccount: `prod-backend-orders-service.prod-app-ns.serviceaccount.identity.linkerd.cluster.local`.
That name is the pod's **mesh identity**.

### 4.3 A call from A to B

1. A's application opens a connection to `wallet-service:3009` as usual.
2. The iptables rules send it to A's proxy (port 4140).
3. The proxy asks the **destination** service where `wallet-service` runs; it keeps an up-to-date list of healthy endpoints.
4. For each HTTP/gRPC *request* it picks the endpoint with the lowest recent latency; for raw TCP it picks one per connection.
5. It opens a connection to B's proxy (port 4143) and both sides complete **mutual TLS** using their certificates.
6. B's proxy checks the policy for this port and the caller's identity, then forwards the plain request to B's application.
7. Both proxies record metrics (and, if `config.linkerd.io/access-log: json` is set, a log line including the caller's identity).

### 4.4 Where traffic is **not** mTLS

Kubernetes health probes (from the node's kubelet), traffic to or from pods without a proxy, and connections to destinations
outside the mesh (databases, external APIs) are not mesh-encrypted. Probes are allowed by Linkerd and carry no identity by design.
In production probes are about 99% of the requests the proxies see; real requests (edge → service, service → service) are mTLS.

## 5. How we use it (the shape of our deployment)

| Aspect | Our choice | Where documented |
|---|---|---|
| Install method | Helm charts from the **edge** channel, version `2026.9.3` (= `edge-26.9.3`) | [03](03-step-2-install-linkerd.md) |
| Certificates | cert-manager: 10-year root, 48 h issuer, no manual renewal | [02](02-step-1-cert-manager-and-identity.md) |
| Public edge | **Envoy Gateway** (Linkerd has no ingress of its own); the edge pods are meshed, so edge → service is mTLS | [04](04-step-3-install-envoy-gateway.md), [05](05-step-4-configure-the-edge.md) |
| Identities | one ServiceAccount per service | [06](06-step-5-mesh-your-services.md) |
| Monitoring | proxies scraped into VictoriaMetrics; four Grafana dashboards; 11 alert rules; **linkerd-viz not used** | [08](08-step-7-monitoring-and-logs.md) |
| Policy | designed (audit → deny), **not yet enforced** | [09](09-step-8-security-policy.md) |

## 6. The honest scorecard

### What we gained
* Automatic encryption and identity between our services, with no application changes.
* Real numbers for every service (success rate, latency, who calls whom) and alerts on them.
* Lower per-pod overhead: 25m CPU request versus the old 100m; measured use is a fraction of even that.
* Fewer components to run than Istio plus its add-ons; about 1.3 GiB of memory freed by removing Prometheus/Zipkin/Kiali alone.
* A clean base for identity-based access control.

### What we gave up or accepted
* **Fewer features than Istio.** In particular: no mesh-level extension filters (Lua/Wasm), no rich fault-injection, and no
  built-in ingress. That is why the public edge is a separate product (Envoy Gateway), which is also where our Lua security-header
  filter lives.
* **A different release model.** The open-source project publishes **edge** releases; per the maintainers' 2024 announcement,
  stable release artifacts are produced by Buoyant (the company behind Linkerd) under its own terms (**general**; verify the
  current licensing and support terms yourself). We run edge releases, so we **pin exact versions and upgrade on purpose**
  ([10-operations.md](10-operations.md)).
* **A smaller ecosystem** (fewer ready-made integrations and fewer engineers who know it) than Istio.
* **New failure modes we now own**: pods that start without a proxy if the injector is down; expiry of the mesh certificates;
  a sidecar that must be sized, scraped and upgraded. We addressed these (2 replicas, alerts, runbooks) but they exist.

### What is still incomplete
* **Authorisation policy is permissive**: mesh traffic is encrypted and identified but not restricted.
* **No before/after latency measurement.**
* The mTLS **share** looks low on a naive dashboard because of probes (we changed the dashboards to count authenticated
  requests).

## 7. Alternatives we considered or could consider

| Option | Idea | Why not chosen / when to look again (general) |
|---|---|---|
| **No mesh** | rely on network rules and TLS to databases | cannot show identity-based, encrypted service-to-service traffic; no uniform metrics |
| **Istio, sidecar** (what we had) | richest feature set | heavier proxies and control plane; more objects to manage; the reason we left |
| **Istio ambient** | no per-pod sidecars (node-level proxy plus optional waypoint) | newer model, different operating trade-offs; revisit if sidecar overhead or sidecar lifecycle issues become the main pain |
| **Cilium service mesh / eBPF** | mesh features in the kernel/CNI | needs Cilium as the cluster network; we use the AWS VPC CNI |
| **Application-level TLS and libraries** | each team adds mTLS and retries | does not scale across services and languages; hard to prove coverage |

## 8. When to revisit this decision

Look again if: the licensing/support model of Linkerd changes in a way that affects us; we need mesh-level extension filters or
traffic management that Linkerd lacks; sidecar overhead becomes a problem again (for example hundreds more pods); the project's
release cadence makes upgrades a burden; or a measured latency problem is traced to the mesh.

## 9. Where to go next

* Learn the vocabulary: [00-linkerd-from-zero.md](00-linkerd-from-zero.md)
* Build it: start at [01-prerequisites.md](01-prerequisites.md)
* See every incident that shaped the setup: [13-lessons-learned.md](13-lessons-learned.md)
