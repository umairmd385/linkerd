# 0. Linkerd from zero

> New to service meshes? Read [background-1-why-a-service-mesh.md](background-1-why-a-service-mesh.md) and
> [background-2-the-case-for-linkerd.md](background-2-the-case-for-linkerd.md) first; this page is the vocabulary and glossary.

You do not need to know anything before reading this page. It explains the ideas once, in plain words, and defines every term
the rest of the guide uses.

## 0.1 The problem a service mesh solves

Our exchange is made of about 20 small programs ("services"): users, orders, wallets, matching engine, and so on. They run as
**pods** on Kubernetes and talk to each other over the network. Three things are hard to do well inside each program:

1. **Privacy and identity.** Anybody who can see the network could read or change traffic between services. And a service has
   no reliable way to know *who* is calling it.
2. **Visibility.** When something is slow, which service is it, and which call?
3. **Reliability.** Retrying failed calls, spreading load, noticing unhealthy copies.

A **service mesh** moves these jobs out of the programs and into a small helper that sits next to every program. The programs
are not changed.

## 0.2 What Linkerd is

**Linkerd** is a service mesh. It has two parts:

* **Data plane = the proxies.** A tiny program called `linkerd-proxy` is added to every application pod. All traffic going in
  or out of the application passes through it. The proxy encrypts the traffic, measures it, and applies rules.
* **Control plane = the brain.** A few ordinary pods in the namespace `linkerd` that configure the proxies and hand out
  certificates.

```
 Pod A                                   Pod B
 +-----------------------------+         +-----------------------------+
 | application  --->  proxy ===|=========|===> proxy  --->  application |
 +-----------------------------+  mTLS   +-----------------------------+
        ^  asks for its certificate / for where to send traffic  ^
        +------------------ Linkerd control plane ---------------+
```

The two proxies talk to each other over **mTLS** (explained below). The applications only see plain traffic on their own side.

## 0.3 Words you will meet

| Term | Meaning |
|---|---|
| **Pod** | One running copy of a program (one or more containers) in Kubernetes. |
| **Namespace** | A named folder inside the cluster. We use `prod-app-ns` for the application, `linkerd` for the mesh, `envoy-gateway-system` for the public entry, `cert-manager` for certificates, `monitoring-ns` for monitoring. |
| **Sidecar** | A helper container that runs inside the *same pod* as the application. `linkerd-proxy` is a sidecar. |
| **Native sidecar** | A Kubernetes feature (1.29+) where the sidecar is declared as an *init container that keeps running*. Kubernetes starts it **before** the application and stops it **after**, which avoids lost connections at start and stop. Our proxy runs this way (`nativeSidecar: true`). In `kubectl get pods`, `2/2` means "application + proxy are ready". |
| **TLS** | The encryption that makes `https://` safe: one side proves who it is with a certificate. |
| **mTLS** (mutual TLS) | TLS where **both sides** prove who they are. So a service knows exactly which service is calling it, and nobody in between can read the traffic. Linkerd turns this on automatically. |
| **Identity** | The name a pod proves in mTLS. In Linkerd it is built from the pod's **ServiceAccount**: `prod-backend-orders-service.prod-app-ns.serviceaccount.identity.linkerd.cluster.local`. Giving each service its own ServiceAccount gives each service its own identity (`manifests/41-serviceaccounts.yaml`). |
| **ServiceAccount** | A Kubernetes object that represents "who a pod is". |
| **Trust anchor** | The root certificate. Everything in the mesh trusts certificates signed (directly or indirectly) by it. We use a 10-year one. |
| **Identity issuer** | A certificate, signed by the trust anchor, that the Linkerd *identity* component uses to sign every pod's own short-lived certificate. We use a 48-hour one that cert-manager renews automatically. |
| **cert-manager** | A Kubernetes add-on that creates and renews certificates from `Certificate` objects. We use it so nobody has to remember to renew Linkerd's certificates by hand. |
| **CRD** (Custom Resource Definition) | A way to teach Kubernetes new kinds of objects (for example `HTTPRoute`). Installed once per cluster. |
| **Helm / chart / release** | Helm installs software from a packaged recipe (a *chart*); an installed copy is a *release*. Settings are given in a *values file*. |
| **Gateway API** | The Kubernetes standard way to describe public entry points: a `GatewayClass` (which software), a `Gateway` (listeners such as port 80/443), and `HTTPRoute` objects (hostname + path → service). |
| **Envoy / Envoy Gateway** | Envoy is a high-performance proxy. **Envoy Gateway** runs Envoy as our public entry point and is configured through the Gateway API plus its own policy objects. |
| **NLB** | AWS Network Load Balancer. The internet-facing front door. Created automatically from a Kubernetes `Service` of type `LoadBalancer`. |
| **ACM** | AWS Certificate Manager: holds the public `https` certificate that the NLB uses. (cert-manager above is a *different* thing: it is for the mesh's *internal* certificates.) |
| **PROXY protocol** | A small header the NLB adds so the application can still see the real client IP. |
| **Opaque port** | A port Linkerd must treat as raw TCP (no HTTP detection), for example Redis 6379, MySQL 3306, Kafka 9092. |
| **Server / AuthorizationPolicy / MeshTLSAuthentication** | Linkerd's rule objects: *Server* = "this port of these pods", *MeshTLSAuthentication* = "these identities", *AuthorizationPolicy* = "this Server accepts only those identities". See step 8. |
| **Probe** | A health check that the kubelet (the agent on each node) sends to a pod. It comes from the node, so it has no mesh identity. |
| **KEDA / HPA** | Autoscalers that add or remove pod copies. |

## 0.4 What mTLS gives us, concretely

* **Encryption in the cluster.** Traffic between our own pods cannot be read from the network.
* **Authenticated callers.** The receiving proxy knows the caller's identity. Metrics and logs record it (`client_id`), which
  also tells you *who calls whom* (`scripts/gen_callgraph.py`).
* **Automatic, short-lived keys.** Each pod's certificate lasts 24 hours and is replaced automatically; a stolen one is useless
  after a day.

What mTLS does **not** give by itself: it does not stop an allowed caller from doing something wrong, and it does not block
unknown callers unless you also turn on authorisation policy (step 8). Today production encrypts and identifies but does not
yet restrict.

## 0.5 Why Linkerd (and not something else)

We moved from Istio to Linkerd because Linkerd's proxy is much smaller (memory and CPU per pod), the control plane is simpler,
and its default behaviour is safe. The trade-off: Linkerd does less than Istio (for example, no built-in ingress gateway and no
per-route fault injection), so we use **Envoy Gateway** for the public edge.

## 0.6 The two places traffic is handled

1. **The edge** (north-south: from the internet into the cluster): AWS NLB → **Envoy Gateway**. Here we check the client IP
   allow-list for admin tools, limit requests per IP, add security headers, and choose the route by hostname and path.
2. **The mesh** (east-west: service to service): **Linkerd** proxies. Here we encrypt, identify, measure, and (later) restrict.

The Envoy Gateway pods are themselves *meshed*, so the hop from the edge to a service is also mTLS.

## 0.7 The five objects you will touch most

| You want to... | You change... |
|---|---|
| Expose a new hostname / path | an `HTTPRoute` (example: `examples/httproute.yaml`) |
| Put a service into the mesh | two pod annotations and a ServiceAccount (example: `examples/mesh-a-service.yaml`) |
| Restrict an admin tool by IP | a `SecurityPolicy` (`manifests/37-security-policy-admin.yaml`) |
| Allow only certain callers into a service | `Server` + `AuthorizationPolicy` (example: `examples/policy-example.yaml`) |
| Change proxy size or ports | `manifests/11-linkerd-control-plane-values.yaml` then `helm upgrade` |

Next: [01-prerequisites.md](01-prerequisites.md).
