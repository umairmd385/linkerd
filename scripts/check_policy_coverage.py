#!/usr/bin/env python3
"""Find the gaps in the mesh security policy. Run after applying manifests/40-linkerd-policy.yaml.

Reports (1) pods in prod-app-ns WITHOUT a linkerd-proxy - they enforce no Linkerd policy at all,
(2) container ports with no Server (they fall back to the namespace default, which is permissive), and
(3) Servers still in all-unauthenticated/audit mode. Exit code 1 if anything needs attention.
"""
import json
import subprocess
import sys

NS = "prod-app-ns"


def kj(*a):
    return json.loads(subprocess.run(["kubectl", *a, "-o", "json"], check=True, capture_output=True, text=True).stdout)


servers = kj("-n", NS, "get", "servers.policy.linkerd.io")["items"]
by_port = {}
for s in servers:
    sel = s["spec"].get("podSelector", {}).get("matchLabels", {})
    by_port.setdefault((sel.get("app"), s["spec"]["port"]), s)

bad = 0
print("== pods without a linkerd-proxy (NO policy enforcement)")
for p in kj("-n", NS, "get", "pods")["items"]:
    if p["status"].get("phase") != "Running":
        continue
    # nativeSidecar: true (the installed Linkerd) puts the proxy in initContainers; older mode uses containers
    names = [c["name"] for c in p["spec"]["containers"] + p["spec"].get("initContainers", [])]
    if "linkerd-proxy" not in names:
        bad += 1
        print(f"  UNMESHED  {p['metadata']['name']}")
    app = p["metadata"]["labels"].get("app")
    for c in p["spec"]["containers"]:
        if c["name"] == "linkerd-proxy":
            continue
        for port in c.get("ports", []):
            if (app, port["containerPort"]) not in by_port:
                print(f"  NO SERVER  {p['metadata']['name']} port {port['containerPort']} ({port.get('name','')})")
                bad += 1
print("== servers not enforcing")
for (app, port), s in sorted(by_port.items(), key=lambda x: str(x[0])):
    mode = s["spec"].get("accessPolicy", "deny")
    if mode != "deny":
        print(f"  {mode:20s} {s['metadata']['name']}")
        bad += 1
print("OK - full enforcement" if not bad else f"{bad} item(s) need attention")
sys.exit(1 if bad else 0)
