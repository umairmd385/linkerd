#!/usr/bin/env python3
"""Generate Linkerd Server / AuthorizationPolicy objects for prod-app-ns from the LIVE cluster.

Run AFTER Envoy Gateway is installed and its pods are running (its ServiceAccount name is generated):

    GW_SA=$(kubectl -n envoy-gateway-system get pod \
        -l gateway.envoyproxy.io/owning-gateway-name=prod-edge-gateway \
        -o jsonpath='{.items[0].spec.serviceAccountName}')

Three stages, increasing in strictness (RUNBOOK phase 6):

 A) Baseline - any pod in the namespace may call any service (what UAT did):
      python3 scripts/gen_linkerd_policy.py --gateway-sa "$GW_SA" > manifests/40-linkerd-policy.yaml

 B) Least privilege - each service accepts only the callers actually observed (needs per-service ServiceAccounts,
    scripts/gen_serviceaccounts.py, and >= 30 days of traffic, scripts/gen_callgraph.py):
      python3 scripts/gen_linkerd_policy.py --gateway-sa "$GW_SA" --callgraph callgraph.json \
          --access-policy audit > manifests/40-linkerd-policy.yaml      # audit first: nothing is blocked

 C) Enforce: re-run with --access-policy deny after the audit period shows no unexpected denials.

Model:
  * every Service port gets a Server (accessPolicy: all-unauthenticated | audit | deny)
  * http ports of publicly routed services also accept the Envoy Gateway identity; gRPC and other ports never do
  * Redis (opaque TCP) accepts only the services that use it; phpMyAdmin accepts only the gateway
  * redis-insight is intentionally left without a Server (reached by `kubectl port-forward`, which arrives from the
    node and cannot present an identity)
Kubelet probes are always allowed by Linkerd.
"""
import argparse
import json
import pathlib
import subprocess

import yaml

NS = "prod-app-ns"
TRUST = "serviceaccount.identity.linkerd.cluster.local"
HERE = pathlib.Path(__file__).resolve().parent.parent
SKIP = {"prod-redis-insight-service"}
GATEWAY_ONLY = {"phpmyadmin-service"}            # not called by other services
OPAQUE = {"prod-redis-service"}                  # plain TCP, no HTTP


def kj(*args):
    return json.loads(subprocess.run(["kubectl", *args, "-o", "json"], check=True, capture_output=True, text=True).stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway-sa", required=True)
    ap.add_argument("--access-policy", default="all-unauthenticated", choices=["all-unauthenticated", "audit", "deny"])
    ap.add_argument("--callgraph", help="JSON {deployment: [caller serviceaccount names]} from gen_callgraph.py")
    ap.add_argument("--extra-caller", action="append", default=[],
                    help="deployment=serviceaccount, add a caller the call graph did not see (repeatable)")
    a = ap.parse_args()

    cg = json.load(open(a.callgraph)) if a.callgraph else None
    if a.extra_caller and cg is None:
        ap.error("--extra-caller needs --callgraph")
    for e in a.extra_caller:
        d, sa = e.split("=")
        cg.setdefault(d, []).append(sa)

    routed = set()
    for d in yaml.safe_load_all((HERE / "manifests/35-httproutes.yaml").read_text()):
        if d and d.get("kind") == "HTTPRoute" and d["metadata"]["namespace"] == NS:
            for r in d["spec"]["rules"]:
                routed.update(b["name"] for b in r["backendRefs"])

    app_to_dep = {}
    for kind in ("deploy", "statefulset"):                       # Redis is a StatefulSet
        for dep in kj("-n", NS, "get", kind)["items"]:
            lbl = dep["spec"]["selector"]["matchLabels"].get("app")
            if lbl:
                app_to_dep[lbl] = dep["metadata"]["name"]

    out = [{"apiVersion": "policy.linkerd.io/v1alpha1", "kind": "MeshTLSAuthentication",
            "metadata": {"name": "gateway-mtls", "namespace": NS},
            "spec": {"identities": [f"{a.gateway_sa}.envoy-gateway-system.{TRUST}"]}}]
    if cg is None:
        out.append({"apiVersion": "policy.linkerd.io/v1alpha1", "kind": "MeshTLSAuthentication",
                    "metadata": {"name": "same-namespace-mtls", "namespace": NS},
                    "spec": {"identities": [f"*.{NS}.{TRUST}"]}})
    made_auth = set()

    def callers_auth(dep_name):
        """MeshTLSAuthentication for the callers of one deployment (least-privilege mode)."""
        sas = sorted(set(cg.get(dep_name, [])))
        if not sas:
            return None
        n = f"callers-{dep_name.replace('prod-', '', 1)}"
        if n not in made_auth:
            made_auth.add(n)
            out.append({"apiVersion": "policy.linkerd.io/v1alpha1", "kind": "MeshTLSAuthentication",
                        "metadata": {"name": n, "namespace": NS},
                        "spec": {"identities": [f"{sa}.{NS}.{TRUST}" for sa in sas]}})
        return n

    for svc in kj("-n", NS, "get", "svc")["items"]:
        name = svc["metadata"]["name"]
        sel = svc["spec"].get("selector")
        if not sel or name in SKIP:
            continue
        dep_name = app_to_dep.get(sel.get("app"))
        for p in svc["spec"]["ports"]:
            tp = p.get("targetPort", p["port"])
            tp = tp if isinstance(tp, int) else p["port"]
            sname = f"{name}-{tp}".replace("prod-", "", 1)
            spec = {"podSelector": {"matchLabels": {"app": sel["app"]}}, "port": tp,
                    "proxyProtocol": "opaque" if name in OPAQUE else "unknown", "accessPolicy": a.access_policy}
            out.append({"apiVersion": "policy.linkerd.io/v1beta3", "kind": "Server",
                        "metadata": {"name": sname, "namespace": NS}, "spec": spec})
            refs = []
            if name in GATEWAY_ONLY:
                refs = ["gateway-mtls"]
            else:
                if cg is None:
                    refs.append("same-namespace-mtls")
                else:
                    n = callers_auth(dep_name) if dep_name else None
                    if n:
                        refs.append(n)
                if name in routed and p["name"] == "http":
                    refs.append("gateway-mtls")
            if not refs:
                print(f"# NOTE: {sname} has no allowed callers - it will refuse everything", flush=True)
            for ref in refs:
                out.append({"apiVersion": "policy.linkerd.io/v1alpha1", "kind": "AuthorizationPolicy",
                            "metadata": {"name": f"{sname}-{'gateway' if ref == 'gateway-mtls' else 'callers'}",
                                         "namespace": NS},
                            "spec": {"targetRef": {"group": "policy.linkerd.io", "kind": "Server", "name": sname},
                                     "requiredAuthenticationRefs": [{"group": "policy.linkerd.io",
                                                                     "kind": "MeshTLSAuthentication", "name": ref}]}})
    mode = "least-privilege" if cg is not None else "baseline (same-namespace)"
    print(f"# GENERATED by scripts/gen_linkerd_policy.py - mode={mode}, accessPolicy={a.access_policy} - do not hand-edit")
    print("---")
    print("\n---\n".join(yaml.safe_dump(o, sort_keys=False, width=120) for o in out))


if __name__ == "__main__":
    main()
