#!/usr/bin/env python3
"""Roll the services one at a time so their Linkerd proxy picks up the smaller resource request (manifests/11: cpu 25m, memory 32Mi).

Proxy settings are read when a pod is CREATED, so after `helm upgrade` of the control plane (step 1 below) every existing pod still
holds the old 100m request until it is replaced. This script replaces them safely:

    python3 scripts/resize_proxy_requests.py            # dry run: shows the order and the CPU that will be released
    python3 scripts/resize_proxy_requests.py --go       # restart one deployment at a time

For each Deployment: `kubectl rollout restart` (maxSurge 1 / maxUnavailable 0 = no downtime), wait for the rollout, require every
pod of the namespace to be Ready for 20 s, then continue. It stops at the first problem. ArgoCD does not revert the restart annotation.
The Redis StatefulSet is NOT touched here (a Redis restart drops every backend's connection): do it in a quiet period, see README.
"""
import json
import subprocess
import sys
import time

NS = "prod-app-ns"
TARGET = "25m"
# least critical first; the money path (matching engine, orders, wallets) last
LAST = ["prod-backend-wallet-cron-service", "prod-backend-wallet-custody-service", "prod-backend-wallet-main-service",
        "prod-backend-orders-service"]
# Not restarted automatically: the matching engine may hold the order book in memory. Restart it yourself in a quiet period
# (it has 2 replicas and a rolling update), together with the Redis StatefulSet.
SKIP = {"prod-backend-matching-engine-service"}


def sh(*a, check=False):
    r = subprocess.run(a, capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"FAILED: {' '.join(a)}\n{r.stdout}{r.stderr}")
    return r.stdout


def kj(*a):
    return json.loads(sh("kubectl", *a, "-o", "json"))


def cpu_m(v):
    return int(v[:-1]) if v.endswith("m") else int(float(v) * 1000)


def proxy_req(pod):
    for c in pod["spec"].get("initContainers", []) + pod["spec"]["containers"]:
        if c["name"] == "linkerd-proxy":
            return cpu_m(c["resources"].get("requests", {}).get("cpu", "0"))
    return None


def not_ready():
    bad = []
    for p in kj("-n", NS, "get", "pods")["items"]:
        if p["status"]["phase"] == "Running" and not all(c["ready"] for c in p["status"].get("containerStatuses", [])):
            bad.append(p["metadata"]["name"])
    return bad


def main():
    go = "--go" in sys.argv
    pods = kj("-n", NS, "get", "pods")["items"]
    todo, release = {}, 0
    for p in pods:
        r = proxy_req(p)
        owner = (p["metadata"].get("ownerReferences") or [{}])[0]
        if (r is None or r > cpu_m(TARGET)) and owner.get("kind") == "ReplicaSet" and "-".join(owner["name"].split("-")[:-1]) not in SKIP:
            dep = "-".join(owner["name"].split("-")[:-1])
            todo[dep] = todo.get(dep, 0) + 1
            release += (r - cpu_m(TARGET)) if r else 0     # r is None: pod was created WITHOUT a proxy (injector was restarting) - restart it too
    order = sorted(todo, key=lambda d: (d in LAST, LAST.index(d) if d in LAST else 0, d))
    print(f"{len(order)} deployments, {sum(todo.values())} pods, releases {release} m CPU of requests")
    for d in order:
        print(f"  {d:46s} {todo[d]} pod(s)")
    if not go:
        print("\n(dry run) add --go to restart them one at a time")
        return
    if not_ready():
        sys.exit(f"STOP: pods not Ready before starting: {not_ready()}")
    for d in order:
        print(f"== {d}", flush=True)
        sh("kubectl", "-n", NS, "rollout", "restart", f"deploy/{d}", check=True)
        sh("kubectl", "-n", NS, "rollout", "status", f"deploy/{d}", "--timeout=300s", check=True)
        time.sleep(20)
        bad = not_ready()
        if bad:
            sys.exit(f"STOP after {d}: not Ready: {bad}\nInvestigate before continuing; re-run later (finished services are skipped).")
    print("done:", sh("kubectl", "top", "nodes").strip())


if __name__ == "__main__":
    main()
