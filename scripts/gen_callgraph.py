#!/usr/bin/env python3
"""Learn who really calls whom, from Linkerd's own metrics in VictoriaMetrics, to build least-privilege policy.

Needs: services running with their own ServiceAccount (gen_serviceaccounts.py) and the Linkerd proxies scraped
(manifests/50-vmpodscrape-linkerd.yaml). Every inbound request and TCP connection that reaches a meshed pod carries
`client_id` = the caller's identity, so the observed (callee, caller) pairs ARE the call graph.

    python3 scripts/gen_callgraph.py --window 30d > callgraph.json

Use at least 30 days so monthly jobs (cron, settlements, reports) appear; anything rarer must be added by hand with
`gen_linkerd_policy.py --extra-caller <deployment>=<serviceaccount>`. Then roll out in `audit` mode first.
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request

NS = "prod-app-ns"
SUFFIX = f".{NS}.serviceaccount.identity.linkerd.cluster.local"


def query(base, q):
    url = f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": q})
    return json.load(urllib.request.urlopen(url, timeout=60))["data"]["result"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="30d")
    ap.add_argument("--port", type=int, default=18428)
    a = ap.parse_args()

    pf = subprocess.Popen(["kubectl", "-n", "monitoring-ns", "port-forward", "svc/vmsingle-vmstack", f"{a.port}:8428"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)
        base = f"http://127.0.0.1:{a.port}"
        graph = {}
        for metric in ("request_total", "tcp_open_total"):      # HTTP/gRPC and opaque TCP (Redis, ...)
            q = (f'sum by (deployment, statefulset, client_id) (increase({metric}{{direction="inbound",namespace="{NS}",'
                 f'tls="true"}}[{a.window}])) > 0')
            for r in query(base, q):
                m = r["metric"]
                dep, cid = m.get("deployment") or m.get("statefulset"), m.get("client_id", "")
                if not dep or not cid.endswith(SUFFIX):
                    continue                                    # edge/unmeshed callers are handled separately
                graph.setdefault(dep, set()).add(cid[: -len(SUFFIX)])
        if not graph:
            sys.exit("no inbound mTLS traffic found - are the proxies scraped and are pods meshed?")
        print(json.dumps({d: sorted(s) for d, s in sorted(graph.items())}, indent=2))
        for d, s in sorted(graph.items()):
            print(f"# {d:45s} <- {', '.join(sorted(s))}", file=sys.stderr)
        defaults = sorted({c for s in graph.values() for c in s if c == "default"})
        if defaults:
            print("\nWARNING: callers still using the 'default' ServiceAccount were seen; finish rolling out "
                  "per-service ServiceAccounts before generating policy.", file=sys.stderr)
    finally:
        pf.terminate()


if __name__ == "__main__":
    main()
