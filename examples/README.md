# examples/: small templates to copy

Each file is a **commented** template for a task you will do repeatedly. They are not applied by any install step, but each
passed a server-side dry run against the live cluster (`kubectl apply --dry-run=server`), so the field names are correct.

| File | Task | Used in |
|---|---|---|
| [`mesh-a-service.yaml`](mesh-a-service.yaml) | Put one service into the mesh: its ServiceAccount and the three pod-template lines (`serviceAccountName`, `linkerd.io/inject`, `config.linkerd.io/access-log`) | [docs/06](../docs/06-step-5-mesh-your-services.md), [docs/10](../docs/10-operations.md) |
| [`httproute.yaml`](httproute.yaml) | Expose a service on a public hostname (and several services behind one host); an admin-IP `SecurityPolicy` as a commented extra | [docs/05](../docs/05-step-4-configure-the-edge.md) |
| [`policy-example.yaml`](policy-example.yaml) | Allow only known callers into one service: `MeshTLSAuthentication`, `Server`, `AuthorizationPolicy` | [docs/09](../docs/09-step-8-security-policy.md) |

Replace every name (`my-service`, hostnames, namespaces) before applying. They use production names so the intent is clear.
