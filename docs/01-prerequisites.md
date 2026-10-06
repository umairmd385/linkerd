# 1. Prerequisites

Do not start installing until every box below is ticked.

## 1.1 Tools on your computer

| Tool | Why | Check |
|---|---|---|
| `kubectl` (matching your cluster, 1.36 in production) | talk to the cluster | `kubectl version --client` |
| `helm` 3.12 or newer (with OCI support) | install charts | `helm version` |
| `linkerd` CLI, **edge** channel (used only for health checks) | `linkerd check` | `linkerd version --client` (`edge-26.9.3`) |
| `openssl` | read certificate dates | `openssl version` |
| `python3` with `pyyaml` | the helper scripts in `scripts/` | `python3 -c "import yaml"` |
| `curl`, `dig` | testing the edge and DNS | |
| `aws` CLI | Route 53 / load balancer lookups (step 6) | `aws sts get-caller-identity` |

Install the Linkerd CLI (edge channel) and make sure it is on your `PATH`:

```bash
curl -sL https://run.linkerd.io/install-edge | sh
export PATH=$HOME/.linkerd2/bin:$PATH
linkerd version --client
```

## 1.2 Access

* `kubectl` points at the right cluster. **Always check before you apply anything:**
  ```bash
  kubectl config current-context
  # production: arn:aws:eks:us-east-1:111122223333:cluster/<your-cluster>
  ```
* You can create cluster-wide objects (CRDs, namespaces, webhooks): i.e. you are a cluster admin.
* For step 6: permission to edit the Route 53 zone `example.com`.

## 1.3 Cluster requirements

| Requirement | Why | Check |
|---|---|---|
| Kubernetes **1.29 or newer** | native sidecars (`nativeSidecar: true`) | `kubectl version` |
| **AWS Load Balancer Controller** installed | turns the gateway's `Service` into an NLB using the annotations in `30-envoyproxy.yaml` | `kubectl -n kube-system get deploy aws-load-balancer-controller` |
| An **ACM certificate** for your domains in the same region | TLS on port 443 at the NLB (its ARN goes in `30-envoyproxy.yaml`) | AWS console → Certificate Manager |
| The **VPC CNI network-policy agent** (only for the optional `42-networkpolicy...yaml`) | enforces Kubernetes NetworkPolicy | |
| **VictoriaMetrics operator + Grafana** (only for step 7) | the scrape/alert objects in `manifests/50-53` are VictoriaMetrics operator kinds (`VMPodScrape`, `VMRule`) | `kubectl get crd | grep victoriametrics` |
| **Vector** log collector (only for step 7 logs) | tags request logs `edge_access` / `mesh_access` | |

If you use Prometheus instead of VictoriaMetrics, translate the three monitoring files (50, 52, 53) to `PodMonitor` /
`PrometheusRule`; the metric names and queries are identical.

## 1.4 Capacity (read this, it bit us)

Each meshed pod gets one extra container. In production it requests **25 millicores of CPU and 32 MiB of memory**, and uses
about 5-10 MiB. With 26 meshed pods that is 0.65 vCPU and 0.8 GiB reserved. The control plane adds about 0.6 vCPU / 0.6 GiB
(two copies of each of three components, plus the policy controller). Envoy Gateway adds two proxy pods
(100m CPU / 128 MiB each) and one controller.

Before you start, `kubectl top nodes` should show memory **below about 80%** on every node. We started at 96-100% and had to
free memory first (see [13-lessons-learned.md](13-lessons-learned.md), "Capacity").

## 1.5 Decide these before you begin

| Decision | Our answer | Where it matters |
|---|---|---|
| Domains served by the gateway | `*.example.com` hostnames (`exchange`, `admin`, `api`, `socket`, `chart`, `prod-argocd`, `prod-jenkins`, `prod-grafana`, `prod-phpmyadmin`) | `35-httproutes.yaml`, `37-security-policy-admin.yaml`, the CORS list in `32-...yaml` |
| Office/VPN IP addresses allowed to reach admin tools | five addresses | `37-security-policy-admin.yaml` |
| ACM certificate ARN | `arn:aws:acm:us-east-1:111122223333:certificate/00000000-...` | `30-envoyproxy.yaml` |
| Which namespaces hold meshed applications | `prod-app-ns` | annotations, `41-serviceaccounts.yaml`, alert rules |
| Opaque ports (raw-TCP ports) | `25,587,3306,4444,5432,6379,9092,9093,9300,11211,26379` | `11-linkerd-control-plane-values.yaml` |

## 1.6 Safety rules we follow

1. Install in the order of this guide. Each step ends with a check; **do not continue if the check fails.**
2. Change one thing at a time; one service at a time when meshing applications.
3. Never test with the production domain first: use the `Host` header trick against the new load balancer (step 4).
4. Keep a rollback for each step (each document has a "Roll back" section).

Next: [02-step-1-cert-manager-and-identity.md](02-step-1-cert-manager-and-identity.md).
