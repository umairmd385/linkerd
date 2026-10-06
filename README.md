# Linkerd + Envoy Gateway: setup guide and reference (production reference)

This directory is a complete, self-contained guide to the service mesh that runs in production. It is written for someone who
has **never used Linkerd** (or a service mesh at all). Read the documents in order and run the commands as written and you get
the same setup as production: a mutual-TLS service mesh (Linkerd), a public entry point (Envoy Gateway), monitoring, and an
optional security-policy layer.

> Everything here was proven on the production cluster (`prod-example-eks`, AWS us-east-1, Kubernetes 1.36) in
> October 2026. Each document says plainly which parts were run exactly as written and which parts are derived from chart
> defaults for a fresh cluster. **Always run `helm ... --dry-run` / `kubectl apply --dry-run=server` first.**

## What you get

| Piece | What it does | Version in production |
|---|---|---|
| **cert-manager** | Issues and renews the certificates Linkerd uses for identity | v1.21.2 |
| **Linkerd** (control plane + one small proxy next to each application container) | Encrypts and authenticates every call between services (mTLS), measures requests, enforces who may call whom | edge-26.9.3 (Helm chart `2026.9.3`) |
| **Gateway API** | The Kubernetes standard for describing public routes (`Gateway`, `HTTPRoute`) | CRDs v1.6.x, standard channel |
| **Envoy Gateway** | The public entry point (replaces Istio's ingress gateway): TLS-terminated traffic from the AWS load balancer is routed to the right service | 1.9.2 |
| **Monitoring** | Metrics, four Grafana dashboards, alert rules and request logs for the mesh and the edge | VictoriaMetrics / Grafana / VictoriaLogs / Vector |

## How a request travels (one picture)

```
 Internet
    |  HTTPS (TLS 1.3, ACM certificate)
    v
 AWS Network Load Balancer  (adds a "PROXY protocol" header so the real client IP is kept)
    |  plain TCP
    v
 Envoy Gateway proxy pods  (namespace envoy-gateway-system)      <- checks client IP allow-list, rate limit,
    |                                                                adds security headers, picks the route
    |  mTLS (Linkerd proxy of the gateway -> Linkerd proxy of the service)
    v
 Application pod:  [ linkerd-proxy ]  <->  [ your application container ]
    |  mTLS to the next service when it calls another service
    v
 Other application pods ...
```

## Read in this order

**New to service meshes? Start with the two background pages.** They explain the problem, the idea, and why we picked Linkerd
(including an honest list of what we gave up).

| # | Document | You will learn / do |
|---|---|---|
| A | [background-1-why-a-service-mesh.md](docs/background-1-why-a-service-mesh.md) | What a service mesh is, how it works, which problems it solves, what it costs, whether you need one |
| B | [background-2-the-case-for-linkerd.md](docs/background-2-the-case-for-linkerd.md) | What Linkerd is, why we chose it, how it works, scorecard (gains, trade-offs, what is unproven), alternatives |
| 0 | [00-linkerd-from-zero.md](docs/00-linkerd-from-zero.md) | What a service mesh and Linkerd are, in plain language; every term used later |
| 1 | [01-prerequisites.md](docs/01-prerequisites.md) | Tools, access, cluster requirements, capacity |
| 2 | [02-step-1-cert-manager-and-identity.md](docs/02-step-1-cert-manager-and-identity.md) | Install cert-manager and create Linkerd's certificates |
| 3 | [03-step-2-install-linkerd.md](docs/03-step-2-install-linkerd.md) | Install the Linkerd control plane, check it is healthy |
| 4 | [04-step-3-install-envoy-gateway.md](docs/04-step-3-install-envoy-gateway.md) | Install Envoy Gateway and its CRDs |
| 5 | [05-step-4-configure-the-edge.md](docs/05-step-4-configure-the-edge.md) | Gateway, routes, IP allow-list, rate limit, security headers; test without touching DNS |
| 6 | [06-step-5-mesh-your-services.md](docs/06-step-5-mesh-your-services.md) | Put the Linkerd proxy next to your applications, safely, one at a time |
| 7 | [07-step-6-dns-and-cutover.md](docs/07-step-6-dns-and-cutover.md) | Point Route 53 at the new load balancer, and how to roll back |
| 8 | [08-step-7-monitoring-and-logs.md](docs/08-step-7-monitoring-and-logs.md) | Metrics scraping, dashboards, alerts, access logs |
| 9 | [09-step-8-security-policy.md](docs/09-step-8-security-policy.md) | Optional: allow only known callers (audit first, then enforce) |
| 10 | [10-operations.md](docs/10-operations.md) | Daily checks, adding a service, upgrades, certificates, scaling |
| 11 | [11-troubleshooting.md](docs/11-troubleshooting.md) | Symptom → cause → fix |
| 12 | [12-file-reference.md](docs/12-file-reference.md) | **Every file in this directory explained** |
| 13 | [13-lessons-learned.md](docs/13-lessons-learned.md) | Real incidents and mistakes from the rollout, so you do not repeat them |

## Directory layout

```
linkerd/
├── README.md                  this page
├── docs/                      the guide: background A-B, steps 0-9, operations, troubleshooting, file reference, lessons
│   └── README.md              index of the guide with reading order and time estimates
├── manifests/                 every Kubernetes/Helm file, numbered in the order they are used
│   └── README.md              what the number ranges mean and the rules for working with the files
├── examples/                  small, commented templates to copy (new route, mesh a service, policy)
│   └── README.md
└── scripts/                   helper scripts (policy generator, dashboard generator, checks)
    └── README.md
```

## The five-minute version (for someone who only needs the shape of it)

1. `cert-manager` makes a root certificate (the *trust anchor*) and an *issuer* certificate that renews itself every ~23 hours.
2. Linkerd's control plane uses the issuer to give **every pod its own short-lived certificate** (24 h) as soon as it starts.
3. A `linkerd-proxy` container runs in each application pod. Applications are not changed: the proxy catches their traffic and
   upgrades it to mTLS automatically.
4. Envoy Gateway receives public traffic and sends it into the mesh over mTLS.
5. Grafana dashboards and alerts show request rate, success rate, latency and whether traffic is encrypted.

## Important limits to know up front

* **Linkerd authorisation policy is still permissive** in production (any meshed pod may call any service). The mesh encrypts
  and authenticates traffic but does not yet *restrict* it. Step 8 is how to restrict it.
* Kubelet health probes are plain HTTP by design (they come from the node and cannot carry a mesh identity), so a naive
  "percentage of traffic that is mTLS" looks low. The dashboards count authenticated requests instead. See
  [13-lessons-learned.md](docs/13-lessons-learned.md).
* Istio was the previous mesh; mentions of it in file comments are history. It is gone from the cluster.
