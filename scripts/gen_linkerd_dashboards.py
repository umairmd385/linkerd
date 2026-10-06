#!/usr/bin/env python3
"""Grafana dashboards that replace linkerd-viz, built on the Linkerd proxy metrics in VictoriaMetrics.

    python3 scripts/gen_linkerd_dashboards.py          # writes manifests/51-grafana-linkerd-dashboards.yaml
    python3 scripts/gen_linkerd_dashboards.py --check  # also runs every query against VictoriaMetrics (port-forward)

What linkerd-viz offered -> where it lives now
  Top-line (success rate, RPS, latency)    -> "Linkerd / Overview"
  Namespace / Deployment / Pod pages       -> "Linkerd / Workload"   (+ callers, callees, status codes, proxy CPU/RAM)
  Edges / Top (who talks to whom)          -> "Linkerd / Service graph"
  Health (mTLS, policy, control plane)     -> "Linkerd / Security & control plane"
  Tap (live request stream)                -> NOT reproducible from metrics. Closest substitute: the live access-log
                                              panel in "Linkerd / Workload" (VictoriaLogs, needs the namespace
                                              annotation config.linkerd.io/access-log=json). Tap is intentionally gone:
                                              it let anyone with the right RBAC read live request metadata.

Metric/label facts (verified against the running cluster, 2026-10-05): request_total, response_total{classification},
response_latency_ms_bucket, tcp_open_total; labels direction, deployment, statefulset, namespace, pod, client_id, tls,
authority, dst_deployment, dst_namespace, authz_name, srv_name. Added by 50-vmpodscrape-linkerd.yaml's relabelling.
"""
import argparse
import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import yaml

HERE = pathlib.Path(__file__).resolve().parent.parent
OUT = HERE / "manifests/51-grafana-linkerd-dashboards.yaml"
DS = {"type": "prometheus", "uid": "${datasource}"}
LOGS_DS = {"type": "victoriametrics-logs-datasource", "uid": "VictoriaLogs"}
RI = "$__rate_interval"
NS = 'namespace=~"$namespace"'
CALLER = r'label_replace(label_replace(%s, "caller", "$1", "client_id", "^([^.]+)\\..*"), "caller_ns", "$1", "client_id", "^[^.]+\\.([^.]+)\\..*")'
_exprs = []          # every query, for --check


# ---------------------------------------------------------------- panel helpers
class Grid:
    def __init__(self):
        self.y, self.x, self.h = 0, 0, 0

    def place(self, w, h):
        if self.x + w > 24:
            self.y += self.h
            self.x, self.h = 0, 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.h = max(self.h, h)
        return pos

    def row(self):
        self.y += self.h
        self.x, self.h = 0, 0


_id = [0]


def nid():
    _id[0] += 1
    return _id[0]


def tgt(expr, legend="", ref="A", instant=False, table=False, ds=DS):
    _exprs.append(expr)
    t = {"datasource": ds, "expr": expr, "refId": ref, "legendFormat": legend}
    if instant:
        t["instant"], t["range"] = True, False
    if table:
        t["format"] = "table"
    return t


def row_panel(g, title):
    g.row()
    p = {"id": nid(), "type": "row", "title": title, "collapsed": False, "panels": [],
         "gridPos": {"x": 0, "y": g.y, "w": 24, "h": 1}}
    g.y += 1
    g.x, g.h = 0, 0
    return p


def stat(g, title, expr, unit="short", w=4, thresholds=None, desc=""):
    th = thresholds or [("green", None)]
    return {"id": nid(), "type": "stat", "title": title, "description": desc, "datasource": DS,
            "gridPos": g.place(w, 4), "targets": [tgt(expr)],
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "graphMode": "none"},
            "fieldConfig": {"defaults": {"unit": unit, "noValue": "-",
                                         "thresholds": {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in th]}}}}


def ts(g, title, targets, unit="short", w=12, h=8, stack=False, desc="", minv=None, maxv=None):
    d = {"unit": unit, "custom": {"lineWidth": 1, "fillOpacity": 12 if stack else 0, "showPoints": "never",
                                  "stacking": {"mode": "normal" if stack else "none"}}}
    if minv is not None:
        d["min"] = minv
    if maxv is not None:
        d["max"] = maxv
    return {"id": nid(), "type": "timeseries", "title": title, "description": desc, "datasource": DS,
            "gridPos": g.place(w, h), "targets": targets, "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max"]},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def table(g, title, queries, rename, units, sort, w=24, h=9, desc="", success_cols=()):
    """queries: [(expr, refId)] all instant, merged on their label columns."""
    targets = [tgt(e, ref=r, instant=True, table=True) for e, r in queries]
    over = []
    for col, unit in units.items():
        o = {"matcher": {"id": "byName", "options": col}, "properties": [{"id": "unit", "value": unit}]}
        if col in success_cols:
            o["properties"] += [
                {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                    {"color": "red", "value": None}, {"color": "orange", "value": 0.95}, {"color": "green", "value": 0.99}]}}]
        over.append(o)
    return {"id": nid(), "type": "table", "title": title, "description": desc, "datasource": DS,
            "gridPos": g.place(w, h), "targets": targets,
            "transformations": [
                {"id": "merge"},
                {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": rename}}],
            "options": {"showHeader": True, "sortBy": [{"displayName": sort, "desc": True}]},
            "fieldConfig": {"defaults": {"custom": {"align": "auto"}}, "overrides": over}}


def dashboard(uid, title, desc, panels, variables, tags=None):
    return {"uid": uid, "title": title, "description": desc, "tags": ["linkerd"] + (tags or []), "timezone": "browser",
            "schemaVersion": 39, "version": 1, "editable": True, "refresh": "30s", "graphTooltip": 1,
            "time": {"from": "now-1h", "to": "now"}, "panels": panels,
            "templating": {"list": variables}, "annotations": {"list": []}}


def var_ds():
    return {"name": "datasource", "label": "Data source", "type": "datasource", "query": "prometheus",
            "regex": "/^VictoriaMetrics$/", "hide": 0, "refresh": 1, "current": {}}


def var_q(name, query, label, multi=False, include_all=False, depends=None):
    v = {"name": name, "label": label, "type": "query", "datasource": DS, "refresh": 2, "sort": 1,
         "query": {"query": query, "refId": f"v_{name}"}, "multi": multi, "includeAll": include_all,
         "allValue": ".+" if include_all else None, "hide": 0, "current": {}}
    if not include_all:
        v.pop("allValue")
    return v


# ---------------------------------------------------------------- shared query fragments
def succ(extra=""):
    return (f'sum(rate(response_total{{direction="inbound",classification="success",{NS}{extra}}}[{RI}]))'
            f' / sum(rate(response_total{{direction="inbound",{NS}{extra}}}[{RI}]))')


def lat(q, by="deployment", extra=""):
    return (f'histogram_quantile({q}, sum by (le, {by}) (rate(response_latency_ms_bucket'
            f'{{direction="inbound",{NS}{extra}}}[{RI}])))')


# ---------------------------------------------------------------- 1. overview
def overview():
    g = Grid()
    p = []
    p += [stat(g, "Meshed pods", 'count(count by (namespace, pod) (request_total{%s}))' % NS, w=4,
               desc="Pods with a Linkerd proxy that report traffic"),
          stat(g, "Success rate", succ(), "percentunit", thresholds=[("red", None), ("orange", 0.95), ("green", 0.99)]),
          stat(g, "Requests / s", f'sum(rate(request_total{{direction="inbound",{NS}}}[{RI}]))', "reqps"),
          stat(g, "p99 latency", lat(0.99, by="namespace"), "ms", thresholds=[("green", None), ("orange", 500), ("red", 2000)]),
          stat(g, "mTLS requests", f'sum(increase(request_total{{direction="inbound",tls="true",{NS}}}[$__range])) or vector(0)',
               "short", thresholds=[("green", None)],
               desc="Requests in the selected range that arrived over mTLS, i.e. with a verified caller identity (edge gateway or another service). "
                    "Kubelet health probes are not counted: they come from the node and cannot carry an identity, and they make up almost all plaintext requests, so a percentage would mislead."),
          stat(g, "Policy denials / s", f'sum(rate(inbound_http_authz_deny_total{{{NS}}}[{RI}])) + sum(rate(inbound_tcp_authz_deny_total{{{NS}}}[{RI}]))',
               "reqps", thresholds=[("green", None), ("orange", 0.01), ("red", 1)])]
    p.append(row_panel(g, "Workloads (like linkerd viz: namespace page)"))
    rng = "$__range"
    inb = f'direction="inbound",{NS}'
    p.append(table(g, "Workloads (selected time range)", [
        (f'sum by (namespace, deployment) (rate(request_total{{{inb}}}[{rng}]))', "A"),
        (f'sum by (namespace, deployment) (rate(response_total{{{inb},classification="success"}}[{rng}])) / sum by (namespace, deployment) (rate(response_total{{{inb}}}[{rng}]))', "B"),
        (f'histogram_quantile(0.5, sum by (le, namespace, deployment) (rate(response_latency_ms_bucket{{{inb}}}[{rng}])))', "C"),
        (f'histogram_quantile(0.95, sum by (le, namespace, deployment) (rate(response_latency_ms_bucket{{{inb}}}[{rng}])))', "D"),
        (f'histogram_quantile(0.99, sum by (le, namespace, deployment) (rate(response_latency_ms_bucket{{{inb}}}[{rng}])))', "E"),
        (f'sum by (namespace, deployment) (rate(request_total{{{inb},tls="true"}}[{rng}]))', "F")],
        {"Value #A": "RPS", "Value #B": "Success", "Value #C": "p50 ms", "Value #D": "p95 ms", "Value #E": "p99 ms", "Value #F": "mTLS req/s"},
        {"RPS": "reqps", "Success": "percentunit", "p50 ms": "ms", "p95 ms": "ms", "p99 ms": "ms", "mTLS req/s": "reqps"},
        sort="RPS", success_cols=("Success",), h=10,
        desc="Equivalent of the linkerd viz namespace/deployment table. Click a deployment name in the Workload dashboard."))
    p.append(row_panel(g, "Trends"))
    p += [ts(g, "Requests / s by workload", [tgt(f'sum by (deployment) (rate(request_total{{direction="inbound",{NS}}}[{RI}]))', "{{deployment}}")], "reqps", stack=True),
          ts(g, "Success rate by workload", [tgt(f'sum by (deployment) (rate(response_total{{direction="inbound",classification="success",{NS}}}[{RI}])) / sum by (deployment) (rate(response_total{{direction="inbound",{NS}}}[{RI}]))', "{{deployment}}")], "percentunit", minv=0, maxv=1),
          ts(g, "p95 latency by workload", [tgt(lat(0.95), "{{deployment}}")], "ms"),
          ts(g, "p99 latency by workload", [tgt(lat(0.99), "{{deployment}}")], "ms"),
          ts(g, "5xx / s by workload", [tgt(f'sum by (deployment) (rate(response_total{{direction="inbound",status_code=~"5..",{NS}}}[{RI}]))', "{{deployment}}")], "reqps"),
          ts(g, "TCP connections opened / s", [tgt(f'sum by (deployment) (rate(tcp_open_total{{direction="inbound",{NS}}}[{RI}]))', "{{deployment}}")], "cps",
             desc="Includes opaque protocols such as Redis and Kafka.")]
    vars_ = [var_ds(), var_q("namespace", "label_values(request_total, namespace)", "Namespace", multi=True, include_all=True)]
    return dashboard("linkerd-overview", "Linkerd / Overview", "Top-line health of the mesh. Replaces linkerd-viz top-line and namespace pages.", p, vars_)


# ---------------------------------------------------------------- 2. workload
def workload():
    g = Grid()
    sel = 'namespace="$namespace",deployment="$deployment"'
    inb = f'direction="inbound",{sel}'
    p = [stat(g, "Requests / s", f'sum(rate(request_total{{{inb}}}[{RI}]))', "reqps", w=4),
         stat(g, "Success rate", f'sum(rate(response_total{{{inb},classification="success"}}[{RI}])) / sum(rate(response_total{{{inb}}}[{RI}]))', "percentunit", w=5,
              thresholds=[("red", None), ("orange", 0.95), ("green", 0.99)]),
         stat(g, "p50", f'histogram_quantile(0.5, sum by (le) (rate(response_latency_ms_bucket{{{inb}}}[{RI}])))', "ms", w=5),
         stat(g, "p95", f'histogram_quantile(0.95, sum by (le) (rate(response_latency_ms_bucket{{{inb}}}[{RI}])))', "ms", w=5),
         stat(g, "p99", f'histogram_quantile(0.99, sum by (le) (rate(response_latency_ms_bucket{{{inb}}}[{RI}])))', "ms", w=5,
              thresholds=[("green", None), ("orange", 500), ("red", 2000)])]
    p.append(row_panel(g, "Golden metrics (inbound)"))
    p += [ts(g, "Requests / s", [tgt(f'sum(rate(request_total{{{inb}}}[{RI}]))', "requests")], "reqps", w=8),
          ts(g, "Success rate", [tgt(f'sum(rate(response_total{{{inb},classification="success"}}[{RI}])) / sum(rate(response_total{{{inb}}}[{RI}]))', "success")], "percentunit", w=8, minv=0, maxv=1),
          ts(g, "Latency percentiles", [tgt(f'histogram_quantile({q}, sum by (le) (rate(response_latency_ms_bucket{{{inb}}}[{RI}])))', f"p{int(q*100)}", ref=r)
                                         for q, r in ((0.5, "A"), (0.95, "B"), (0.99, "C"))], "ms", w=8),
          ts(g, "Responses by status code", [tgt(f'sum by (status_code) (rate(response_total{{{inb}}}[{RI}]))', "{{status_code}}")], "reqps", stack=True, w=12),
          ts(g, "Pods (requests / s)", [tgt(f'sum by (pod) (rate(request_total{{{inb}}}[{RI}]))', "{{pod}}")], "reqps", w=12,
             desc="Load balancing across replicas (replaces the viz pod list).")]
    p.append(row_panel(g, "Who calls this workload (inbound, by caller identity)"))
    rng = "$__range"
    p.append(table(g, "Callers", [
        (CALLER % f'sum by (client_id) (rate(request_total{{{inb}}}[{rng}]))', "A"),
        (CALLER % f'sum by (client_id) (rate(response_total{{{inb},classification="success"}}[{rng}])) / sum by (client_id) (rate(response_total{{{inb}}}[{rng}]))', "B"),
        (CALLER % f'histogram_quantile(0.95, sum by (le, client_id) (rate(response_latency_ms_bucket{{{inb}}}[{rng}])))', "C")],
        {"Value #A": "RPS", "Value #B": "Success", "Value #C": "p95 ms", "caller": "Caller (ServiceAccount)", "caller_ns": "Namespace"},
        {"RPS": "reqps", "Success": "percentunit", "p95 ms": "ms"}, sort="RPS", success_cols=("Success",), w=12, h=8,
        desc="Caller identity as seen by mTLS. 'default' means the pod still uses the shared default ServiceAccount; empty = a plaintext/unmeshed caller."))
    p.append(ts(g, "Requests / s by caller", [tgt(CALLER % f'sum by (client_id) (rate(request_total{{{inb}}}[{RI}]))', "{{caller}}")], "reqps", w=12, h=8))
    p.append(row_panel(g, "What this workload calls (outbound)"))
    outb = f'direction="outbound",namespace="$namespace",deployment="$deployment"'
    p.append(table(g, "Callees", [
        (f'sum by (dst_namespace, dst_deployment, dst_service) (rate(request_total{{{outb},dst_deployment!=""}}[{rng}]))', "A"),
        (f'sum by (dst_namespace, dst_deployment, dst_service) (rate(tcp_open_total{{{outb},dst_deployment!=""}}[{rng}]))', "B")],
        {"Value #A": "Requests/s", "Value #B": "TCP conns/s", "dst_namespace": "Namespace", "dst_deployment": "Callee", "dst_service": "Service"},
        {"Requests/s": "reqps", "TCP conns/s": "cps"}, sort="Requests/s", w=12, h=8))
    p.append(ts(g, "Outbound requests / s by callee", [tgt(f'sum by (dst_deployment) (rate(request_total{{{outb},dst_deployment!=""}}[{RI}]))', "{{dst_deployment}}")], "reqps", w=12, h=8))
    p.append(row_panel(g, "Proxy resources"))
    cs = 'namespace="$namespace",container="linkerd-proxy",pod=~"$deployment-.*"'
    p += [ts(g, "linkerd-proxy CPU (cores)", [tgt(f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{cs}}}[{RI}]))', "{{pod}}")], "none", w=12, h=7),
          ts(g, "linkerd-proxy memory", [tgt(f'sum by (pod) (container_memory_working_set_bytes{{{cs}}})', "{{pod}}")], "bytes", w=12, h=7)]
    p.append(row_panel(g, "Live requests (replacement for linkerd viz tap)"))
    logs = {"id": nid(), "type": "logs", "title": "Access log, newest first", "datasource": LOGS_DS,
            "description": "Needs the namespace annotation config.linkerd.io/access-log=json (Phase 3) and Vector tagging log_type:mesh_access "
                           "(60-vector-process-logs-linkerd.yaml). Unlike tap this is a stored log, searchable and RBAC-controlled through Grafana.",
            "gridPos": g.place(24, 10),
            "targets": [{"datasource": LOGS_DS, "refId": "A", "expr": 'log_type:mesh_access kubernetes.pod_namespace:"$namespace" workload:"$deployment*"',
                         "queryType": "instant", "maxLines": 200}],
            "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True}}
    p.append(logs)
    vars_ = [var_ds(),
             var_q("namespace", "label_values(request_total, namespace)", "Namespace"),
             var_q("deployment", 'label_values(request_total{namespace="$namespace",direction="inbound"}, deployment)', "Workload")]
    return dashboard("linkerd-workload", "Linkerd / Workload", "One deployment in depth. Replaces linkerd-viz deployment/pod pages and tap.", p, vars_)


# ---------------------------------------------------------------- 3. service graph
def graph():
    g = Grid()
    rng = "$__range"
    inb = f'direction="inbound",{NS}'
    p = [row_panel(g, "Edges: who talks to whom (like linkerd viz edges)")]
    p.append(table(g, "Caller -> callee, measured at the callee", [
        (CALLER % f'sum by (client_id, namespace, deployment) (rate(request_total{{{inb},client_id!=""}}[{rng}]))', "A"),
        (CALLER % f'sum by (client_id, namespace, deployment) (rate(response_total{{{inb},client_id!="",classification="success"}}[{rng}])) / sum by (client_id, namespace, deployment) (rate(response_total{{{inb},client_id!=""}}[{rng}]))', "B"),
        (CALLER % f'histogram_quantile(0.95, sum by (le, client_id, namespace, deployment) (rate(response_latency_ms_bucket{{{inb},client_id!=""}}[{rng}])))', "C")],
        {"Value #A": "RPS", "Value #B": "Success", "Value #C": "p95 ms", "caller": "Caller", "caller_ns": "Caller ns", "deployment": "Callee", "namespace": "Callee ns"},
        {"RPS": "reqps", "Success": "percentunit", "p95 ms": "ms"}, sort="RPS", success_cols=("Success",), h=12,
        desc="Built from the callee's inbound metrics and the caller's mTLS identity, so it also shows success and latency per edge. "
             "This is also the data that scripts/gen_callgraph.py turns into least-privilege policy."))
    p.append(table(g, "Workload -> workload, measured at the caller (outbound)", [
        (f'sum by (namespace, deployment, dst_namespace, dst_deployment) (rate(request_total{{direction="outbound",{NS},dst_deployment!=""}}[{rng}]))', "A")],
        {"Value #A": "Requests/s", "namespace": "From ns", "deployment": "From", "dst_namespace": "To ns", "dst_deployment": "To"},
        {"Requests/s": "reqps"}, sort="Requests/s", h=10, w=12,
        desc="Independent of ServiceAccounts, so it works before per-service identities are rolled out."))
    p.append(table(g, "Top destinations by authority (like linkerd viz top)", [
        (f'topk(25, sum by (deployment, authority) (rate(request_total{{direction="outbound",{NS},authority!=""}}[{rng}])))', "A")],
        {"Value #A": "Requests/s", "deployment": "From", "authority": "Destination"}, {"Requests/s": "reqps"}, sort="Requests/s", h=10, w=12))
    p.append(row_panel(g, "External and unmeshed destinations"))
    p.append(table(g, "Outbound connections to destinations outside the mesh", [
        (f'sum by (namespace, deployment, dst_service, dst_namespace, no_tls_reason) (rate(tcp_open_total{{direction="outbound",{NS},tls!="true"}}[{rng}]))', "A")],
        {"Value #A": "Conns/s", "namespace": "From ns", "deployment": "From", "dst_service": "Service", "dst_namespace": "To ns", "no_tls_reason": "Why not mTLS"},
        {"Conns/s": "cps"}, sort="Conns/s", h=10,
        desc="Kafka, Redis, Aurora, the Kubernetes API and external APIs show up here. Useful to see what the egress surface looks like before any egress policy is considered."))
    vars_ = [var_ds(), var_q("namespace", "label_values(request_total, namespace)", "Namespace", multi=True, include_all=True)]
    return dashboard("linkerd-graph", "Linkerd / Service graph", "Who talks to whom, with success and latency per edge. Replaces linkerd-viz edges and top.", p, vars_)


# ---------------------------------------------------------------- 4. security + control plane
def security():
    g = Grid()
    rng = "$__range"
    cm = 'name=~"linkerd-(identity-issuer|trust-anchor)"'
    p = [stat(g, "Identity issuer expires in", 'min((certmanager_certificate_expiration_timestamp_seconds{name="linkerd-identity-issuer"} - time()) / 3600)', "h", w=4,
              thresholds=[("red", None), ("orange", 12), ("green", 20)],
              desc="cert-manager renews the 48 h issuer around the 25 h mark; the value should bounce between about 25 and 48 h."),
         stat(g, "Trust anchor expires in", 'min((certmanager_certificate_expiration_timestamp_seconds{name="linkerd-trust-anchor"} - time()) / 86400)', "d", w=4,
              thresholds=[("red", None), ("orange", 90), ("green", 365)]),
         stat(g, "Control plane pods ready", 'sum(kube_deployment_status_replicas_available{namespace="linkerd"})', "none", w=4),
         stat(g, "Pods without a proxy", 'count((kube_pod_status_phase{namespace="prod-app-ns",phase="Running"} == 1) unless on (namespace, pod) count by (namespace, pod) (kube_pod_container_info{container="linkerd-proxy"} or kube_pod_init_container_info{container="linkerd-proxy"})) or vector(0)',
              "none", w=4, thresholds=[("green", None), ("red", 1)],
              desc="Pods in the production namespace with NO Linkerd proxy: they enforce no policy."),
         stat(g, "mTLS requests / s", f'sum(rate(request_total{{direction="inbound",tls="true"}}[{RI}])) or vector(0)', "reqps", w=4, thresholds=[("green", None)],
              desc="Requests per second authenticated with a mesh identity (edge gateway and service-to-service). Health probes are not counted."),
         stat(g, "Denials / s", f'(sum(rate(inbound_http_authz_deny_total[{RI}])) + sum(rate(inbound_tcp_authz_deny_total[{RI}]))) or vector(0)', "reqps", w=4,
              thresholds=[("green", None), ("orange", 0.01), ("red", 1)])]
    p.append(row_panel(g, "Encryption"))
    p += [ts(g, "mTLS requests/s by caller identity", [tgt(f'sum by (client_id) (rate(request_total{{direction="inbound",tls="true"}}[{RI}]))', "{{client_id}}")], "reqps",
              desc="Who is calling over mTLS. The edge gateway appears here for public traffic; services appear for internal calls."),
          table(g, "Inbound requests without a mesh identity (kubelet probes and any unmeshed client)", [
              (f'sum by (namespace, deployment, tls) (rate(request_total{{direction="inbound",tls!="true"}}[{rng}]))', "A")],
              {"Value #A": "Requests/s", "namespace": "Namespace", "deployment": "Workload", "tls": "tls"}, {"Requests/s": "reqps"}, sort="Requests/s", w=12, h=8,
              desc="Kubelet health probes come from the node and cannot carry an identity, so every workload with probes is listed (about 0.2 to 0.6 req/s per pod is normal). Investigate a workload whose rate is far higher: that is an unmeshed client.")]
    p.append(row_panel(g, "Authorization policy (allow / deny)"))
    p += [ts(g, "Allowed vs denied (HTTP + TCP)", [
              tgt(f'sum(rate(inbound_http_authz_allow_total[{RI}])) + sum(rate(inbound_tcp_authz_allow_total[{RI}]))', "allowed", ref="A"),
              tgt(f'sum(rate(inbound_http_authz_deny_total[{RI}])) + sum(rate(inbound_tcp_authz_deny_total[{RI}]))', "denied", ref="B")], "reqps", w=12),
          table(g, "Denials by workload and caller", [
              (CALLER % f'sum by (namespace, deployment, client_id, srv_name) (rate(inbound_http_authz_deny_total[{rng}]))', "A")],
              {"Value #A": "Denied/s", "namespace": "Namespace", "deployment": "Workload", "srv_name": "Server", "caller": "Caller", "caller_ns": "Caller ns"},
              {"Denied/s": "reqps"}, sort="Denied/s", w=12, h=8,
              desc="Every row is either an unexpected caller or a service missing from the policy. In audit mode these are the requests that WOULD be blocked.")]
    p.append(row_panel(g, "Control plane"))
    p += [ts(g, "Control plane CPU", [tgt('sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="linkerd",container!="",container!="POD"}[5m]))', "{{pod}}")], "none", w=8, h=7),
          ts(g, "Control plane memory", [tgt('sum by (pod) (container_memory_working_set_bytes{namespace="linkerd",container!="",container!="POD"})', "{{pod}}")], "bytes", w=8, h=7),
          ts(g, "Control plane restarts", [tgt('sum by (pod, container) (increase(kube_pod_container_status_restarts_total{namespace="linkerd"}[1h]))', "{{pod}}/{{container}}")], "none", w=8, h=7),
          ts(g, "Certificate refreshes (proxies)", [tgt(f'sum(rate(control_identity_cert_refreshes_total[{RI}]))', "refreshes/s")], "ops", w=12, h=7,
             desc="Each meshed proxy renews its 24 h identity certificate before it expires."),
          ts(g, "Identity certificate time left (proxies)", [tgt('min by (namespace) ((control_identity_cert_expiration_timestamp_seconds - time()) / 3600)', "{{namespace}}")], "h", w=12, h=7)]
    vars_ = [var_ds()]
    return dashboard("linkerd-security", "Linkerd / Security & control plane", "Encryption coverage, policy decisions, certificate health and the control plane. Replaces linkerd-viz health checks.", p, vars_, tags=["security"])


# ---------------------------------------------------------------- output + query check
def configmap(d):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": f"dashboard-{d['uid']}", "namespace": "monitoring-ns", "labels": {"grafana_dashboard": "1"},
                         "annotations": {"grafana_folder": "Linkerd"}},
            "data": {f"{d['uid']}.json": json.dumps(d, indent=1)}}


def check():
    subprocess.Popen(["kubectl", "-n", "monitoring-ns", "port-forward", "svc/vmsingle-vmstack", "18428:8428"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(4)
    bad = 0
    subs = [('namespace="$namespace"', 'namespace="linkerd"'),            # single-select variable (Workload dashboard)
            ("$__rate_interval", "5m"), ("$__range", "1h"),
            ("$namespace", ".+"),                                         # multi/all variable
            ("$deployment", "linkerd-destination")]
    for e in dict.fromkeys(_exprs):
        q = e
        for k, v in subs:
            q = q.replace(k, v)
        try:
            url = "http://127.0.0.1:18428/api/v1/query?" + urllib.parse.urlencode({"query": q})
            r = json.load(urllib.request.urlopen(url, timeout=60))
            n = len(r["data"]["result"])
            print(f"  OK  {n:3d} series  {e[:96]}")
        except Exception as ex:
            bad += 1
            body = getattr(ex, "read", lambda: b"")().decode()[:200]
            print(f"  BAD {e[:96]}\n      {ex} {body}")
    subprocess.run(["pkill", "-f", "port-forward svc/vmsingle-vmstack 18428"])
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    dashes = [overview(), workload(), graph(), security()]
    OUT.write_text("# GENERATED by scripts/gen_linkerd_dashboards.py - replacement for linkerd-viz. Do not hand-edit.\n---\n"
                   + "\n---\n".join(yaml.safe_dump(configmap(d), sort_keys=False, width=10000) for d in dashes))
    print(f"wrote {OUT.relative_to(HERE)}: " + ", ".join(f"{d['title']} ({len([x for x in d['panels'] if x['type'] != 'row'])} panels)" for d in dashes))
    if a.check:
        sys.exit(1 if check() else 0)


if __name__ == "__main__":
    main()
