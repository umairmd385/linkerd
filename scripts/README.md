# scripts/: helper tools

Plain Python 3 with `pyyaml`. They talk to the cluster through `kubectl` (so use the right context) and only **print** output or
**write a file**, except `resize_proxy_requests.py --go`, which restarts deployments. Run them from the `linkerd/` directory:
`python3 scripts/<name>.py`.

| Script | One-line purpose | Output | When |
|---|---|---|---|
| `gen_linkerd_dashboards.py` | Build the four Grafana dashboards | writes `manifests/51-grafana-linkerd-dashboards.yaml`; `--check` runs every query | after changing a panel |
| `gen_callgraph.py` | Learn who really calls whom from the proxies' metrics (`--window 30d`) | JSON on stdout, a readable summary on stderr | step 8, before generating policy |
| `gen_linkerd_policy.py` | Generate `Server` / `AuthorizationPolicy` / `MeshTLSAuthentication` for every service | YAML on stdout (`--access-policy audit|deny`, `--callgraph`, `--extra-caller`) | step 8 |
| `check_policy_coverage.py` | Find pods without a proxy, ports without a Server, Servers still permissive | text; exit code 1 if anything needs attention | after applying policy. *Before* any policy it lists everything (expected) |
| `resize_proxy_requests.py` | Restart deployments one at a time so they pick up new proxy settings, or that have no proxy | dry-run by default; `--go` restarts | after a proxy-default change ([docs/10](../docs/10-operations.md)) |

Needs: `kubectl` access; VictoriaMetrics reachable through `kubectl -n monitoring-ns port-forward svc/vmsingle-vmstack` (the scripts
open it themselves; change the service name inside if yours differs).

Details of each script (what it reads, what it writes, caveats): [`../docs/12-file-reference.md`](../docs/12-file-reference.md), section 12.4.
