# docs/: the guide

Read in this order. Documents 01-09 are a **step-by-step install** (each ends with a checklist and a rollback); 10-13 are for
running and understanding the platform afterwards.

| Order | File | Purpose | Time |
|---|---|---|---|
| A | [background-1-why-a-service-mesh.md](background-1-why-a-service-mesh.md) | What a service mesh is, how it works, which problems it solves and what it costs | 15 min |
| B | [background-2-the-case-for-linkerd.md](background-2-the-case-for-linkerd.md) | What Linkerd is, why we chose it over Istio, how it works, and an honest scorecard | 15 min |
| 0 | [00-linkerd-from-zero.md](00-linkerd-from-zero.md) | Plain-language concepts and a **glossary** to keep open while reading the rest | 10 min |
| 1 | [01-prerequisites.md](01-prerequisites.md) | Tools, access, cluster requirements, capacity | 10 min |
| 2 | [02-step-1-cert-manager-and-identity.md](02-step-1-cert-manager-and-identity.md) | cert-manager, the `linkerd` namespace, the certificate chain | 10 min |
| 3 | [03-step-2-install-linkerd.md](03-step-2-install-linkerd.md) | Linkerd control plane via Helm | 15 min |
| 4 | [04-step-3-install-envoy-gateway.md](04-step-3-install-envoy-gateway.md) | Envoy Gateway CRDs and controller | 10 min |
| 5 | [05-step-4-configure-the-edge.md](05-step-4-configure-the-edge.md) | Gateway, routes and edge policies; tests without DNS | 30-60 min |
| 6 | [06-step-5-mesh-your-services.md](06-step-5-mesh-your-services.md) | Add the proxy to applications, one at a time | hours (in waves) |
| 7 | [07-step-6-dns-and-cutover.md](07-step-6-dns-and-cutover.md) | Move Route 53 to the new load balancer | 30 min + watch |
| 8 | [08-step-7-monitoring-and-logs.md](08-step-7-monitoring-and-logs.md) | Scraping, dashboards, alerts, logs | 30 min |
| 9 | [09-step-8-security-policy.md](09-step-8-security-policy.md) | Optional: restrict who may call whom | weeks (audit period) |
| 10 | [10-operations.md](10-operations.md) | Daily checks, upgrades, certificates, capacity | reference |
| 11 | [11-troubleshooting.md](11-troubleshooting.md) | Symptom → cause → fix | reference |
| 12 | [12-file-reference.md](12-file-reference.md) | Every file in this directory explained | reference |
| 13 | [13-lessons-learned.md](13-lessons-learned.md) | Real incidents and what they taught us | 15 min |

**Conventions in these documents**

* Commands run from the `linkerd/` directory (the parent of this folder) unless stated, so `manifests/...` means `linkerd/manifests/...`.
* "Production" means the cluster `prod-example-eks` in `us-east-1`.
* A box starting **Production note** says how production differs from a fresh install; a **Lesson** points to a real incident.
* Every step ends with a *Check* list; do not go on until every box is ticked.
