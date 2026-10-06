# Background 1: why a service mesh? What it is, how it works, and what problems it solves

*Read this first if you have never worked with a service mesh. It needs no Kubernetes expertise beyond knowing that programs run
in "pods" and talk to each other over the network. Words in **bold** are defined in [00-linkerd-from-zero.md](00-linkerd-from-zero.md#03-words-you-will-meet).*

## 1. The situation: many small programs talking to each other

A modern exchange is not one program. Ours is about twenty: users and login, orders, the matching engine, wallets, staking,
referrals, e-mail, a price/chart feed, a websocket service, two web front-ends, and more. They run as separate **pods** on
Kubernetes, so that each can be deployed, scaled and fixed on its own.

The price of that design is that a request from a user now crosses **several network hops**:

```
 browser → edge → users-service → wallet-service → (database / Kafka / Redis / an external custody API)
                           └──→ orders-service  → matching-engine
```

Every arrow is a network call. Inside one program a function call is instant, private and cannot be intercepted. A network call
is none of those. Each arrow brings the same five questions, and every one of the twenty programs needs an answer to each:

| # | Question | If nobody answers it... |
|---|---|---|
| 1 | **Is this call private?** (Can someone on the network read or change it?) | internal traffic travels in clear text |
| 2 | **Who is calling me?** (Can I trust the caller's claim?) | any pod that can reach me can pretend to be anyone |
| 3 | **What is happening?** (How many requests, how many fail, how slow, between which services?) | an incident takes hours to localise |
| 4 | **What if the other side is slow or down?** (Retry? Timeout? Pick another copy?) | one slow service freezes the callers, then their callers |
| 5 | **Is the traffic balanced?** (Which of the 3 copies gets this request?) | one copy is overloaded while others are idle |

## 2. How teams answered these questions before service meshes

| Approach | How | Problems |
|---|---|---|
| **Put the logic in every application** (libraries for TLS, retries, metrics) | each team adds a client library | different languages need different libraries; each service implements it slightly differently; a security fix means *rebuilding and redeploying all services*; developers must remember to do it |
| **Ignore it** ("the cluster network is trusted") | plain HTTP/gRPC inside the cluster | fails any serious security review: one compromised pod can read everything that passes by; regulators ask for encryption in transit *inside* the platform too |
| **Network-only controls** (security groups, Kubernetes NetworkPolicy) | firewall rules by IP address | they know *addresses*, not *identities*; pod IPs change constantly; no encryption; no visibility into requests |
| **One shared gateway** for everything | all internal calls go out and back in through a central proxy | extra latency and a bottleneck for traffic that should stay local |

## 3. The idea of a service mesh

A **service mesh** moves the five jobs out of the applications into infrastructure, in a uniform way:

> Next to every application container, run a small **proxy**. All traffic to and from the application passes through that proxy.
> A central **control plane** configures the proxies and gives each one a cryptographic identity. The applications are not changed.

```
 Without a mesh:                          With a mesh:
 [app A] ─────plain network────▶ [app B]   [app A]→[proxy]═══encrypted, identified═══[proxy]→[app B]
                                                       ▲                       ▲
                                                       └──── control plane ────┘
```

The proxy that sits next to a program is called a **sidecar** (like the sidecar of a motorbike: attached, travelling with it).
The set of all proxies is the **data plane**; the programs that manage them are the **control plane**.

## 4. How it works, step by step

1. **Injection.** When a pod is created, a Kubernetes **admission webhook** (a hook that can modify new pods) adds two containers:
   an *init container* that edits the pod's firewall rules so every packet to or from the application is redirected to the proxy,
   and the proxy container itself. The application is unaware.
2. **Identity.** At startup the proxy asks the control plane's *identity* service for a certificate. The service checks the pod's
   Kubernetes identity (its ServiceAccount) and signs a short-lived certificate (24 hours in our setup). That certificate is the
   pod's proof of who it is.
3. **Discovery.** The proxy asks the control plane where a destination service currently runs (pod addresses change all the time).
4. **A call.** App A calls `http://wallet-service`. The call leaves A and is caught by A's proxy. The proxy picks a healthy copy of
   `wallet-service`, opens a connection to **B's proxy** and both sides present certificates: this is **mutual TLS (mTLS)**. B's
   proxy hands the *plain* request to B. The reply returns the same way.
5. **Telemetry.** Each proxy counts requests, errors and latency (and can log each request), so you see the whole system without
   changing any code.
6. **Rules.** The control plane can tell the proxy "this service accepts calls only from these identities", and the proxy enforces it.

## 5. The problems it solves (mapped to our five questions)

| Question | What the mesh does | In our platform |
|---|---|---|
| 1. Private? | mTLS between all meshed pods, automatically, with certificates that rotate every 24 h | traffic between users, wallets, orders... is encrypted without touching application code. Helps answer a regulator's "is data encrypted in transit inside your platform?" |
| 2. Who is calling? | every connection carries the caller's **identity** | the proxy log records `client.id` (for example `envoy-...gateway` or `prod-backend-users-service`), so you can prove *who* called *what*; policy can allow only known callers |
| 3. What is happening? | "golden metrics" for every service without instrumenting it: request rate, success rate, latency (p50/p95/p99) | four Grafana dashboards, the *service graph* (who calls whom), alerts when a service's success rate drops |
| 4. Slow or down? | timeouts, retries and failover handled in one place (we use them lightly) | the edge retries two idempotent API routes; applications are otherwise unchanged |
| 5. Balanced? | per-**request** load balancing, not per connection | important for gRPC and WebSockets: a normal Kubernetes Service balances per *connection*, and gRPC keeps one connection open for hours, so all its traffic sticks to one copy |

## 6. What a mesh does **not** do

* It does not fix bugs, or make an insecure application secure: it protects the *traffic between* programs.
* mTLS alone does not stop an authorised caller from misusing a service. To restrict *who may call whom* you must add
  authorisation policy (step 8 in this guide; not yet enforced in production).
* It does not replace a firewall, a WAF or good application logging (a mesh cannot stop an application from writing sensitive values into its own logs).
* It does not encrypt traffic that leaves the cluster (to the database, Kafka brokers outside the mesh, external APIs): those
  connections need their own TLS.

## 7. What it costs (be honest about this)

| Cost | Detail |
|---|---|
| **Resources** | one extra container per pod (ours: 25m CPU / 32 MiB requested, ~8m CPU / 6-10 MiB used), plus the control plane |
| **Latency** | one extra hop on each side of every call (typically a fraction of a millisecond per hop for a lightweight proxy; measure it for your own traffic) |
| **Complexity** | a new layer to learn, upgrade and debug; new failure modes (for example a pod starting without its proxy, expired mesh certificates) |
| **Operations** | the mesh sits in the path of *every* call: its health becomes platform health. That is why we run two replicas, alert on certificate expiry, and keep this documentation |

## 8. Do you actually need one? (a checklist)

A mesh is worth it when **several** of these are true:

* [ ] many services (roughly 10 or more) call each other over the network
* [ ] you must show encryption in transit *inside* the cluster, or prove which service called which (compliance)
* [ ] you want uniform metrics/logs per call without editing every service
* [ ] services are written in several languages, so libraries do not scale
* [ ] you want to restrict service-to-service access by identity, not by IP

It is **not** worth it for a handful of services, or when a single gateway and TLS to the database are all you need.
Our exchange meets all five, which is why we adopted one; the next document explains why **Linkerd**.

## 9. Words you will meet next

*Sidecar, proxy, control plane, data plane, mTLS, identity, certificate, trust anchor, gateway, ingress, east-west and
north-south traffic* are explained in [00-linkerd-from-zero.md](00-linkerd-from-zero.md). *North-south* = traffic entering the
cluster from the internet (handled by the edge, Envoy Gateway). *East-west* = traffic between services inside the cluster
(handled by the mesh).

Next: [background-2-the-case-for-linkerd.md](background-2-the-case-for-linkerd.md).
