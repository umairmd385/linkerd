# 2. Step 1: cert-manager and Linkerd's certificates

**Goal:** a root certificate (trust anchor) and a self-renewing issuer certificate exist, before Linkerd is installed.
**Time:** about 10 minutes. **Impact:** none on running traffic.

## 2.1 Why this step exists

Every pod in the mesh needs a certificate to prove its identity. Linkerd's *identity* component creates those, but it needs a
signing certificate (the **issuer**), which in turn must be signed by a **trust anchor**.

If you create these by hand (the Linkerd CLI default), they expire after **one year** and mTLS then stops cluster-wide on that
exact date. We avoid this: **cert-manager** creates and renews them automatically.

```
 self-signed Issuer ──signs──> trust anchor Certificate (10 years, ECDSA)
                                      │ (stored as Secret linkerd-trust-anchor)
 Issuer "linkerd-trust-anchor" ──signs──> identity issuer Certificate (48 h, renewed ~25 h before expiry)
                                      │ (Secret linkerd-identity-issuer)
 Linkerd identity ──uses it to sign──> every pod's own certificate (24 h)
```

## 2.2 Install cert-manager

```bash
helm repo add jetstack https://charts.jetstack.io
helm repo update
helm install cert-manager jetstack/cert-manager --version v1.21.2 \
  -n cert-manager --create-namespace \
  --set crds.enabled=true \
  --set prometheus.enabled=true \
  --set resources.requests.cpu=25m --set resources.requests.memory=64Mi --set resources.limits.memory=256Mi \
  --set cainjector.resources.requests.cpu=10m --set cainjector.resources.requests.memory=64Mi --set cainjector.resources.limits.memory=256Mi \
  --set webhook.resources.requests.cpu=10m --set webhook.resources.requests.memory=32Mi --set webhook.resources.limits.memory=128Mi
kubectl -n cert-manager rollout status deploy/cert-manager deploy/cert-manager-cainjector deploy/cert-manager-webhook
```

These are exactly the settings used in production (minus a node preference). `crds.enabled=true` installs cert-manager's CRDs
together with the chart; `prometheus.enabled=true` exposes its metrics on port 9402, which step 7 scrapes so the
certificate-expiry alerts work.

## 2.3 Create the `linkerd` namespace (with its labels)

The Linkerd chart does **not** create its namespace, and the namespace needs specific labels. Create it first:

```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Namespace
metadata:
  name: linkerd
  annotations:
    linkerd.io/inject: disabled
  labels:
    config.linkerd.io/admission-webhooks: disabled
    linkerd.io/control-plane-ns: linkerd
    linkerd.io/is-control-plane: "true"
    pod-security.kubernetes.io/enforce: privileged
EOF
```

What the labels mean: `linkerd.io/inject: disabled` stops Linkerd from injecting a proxy into its own control-plane pods
(they carry their own); `admission-webhooks: disabled` keeps the Linkerd webhooks from intercepting the control plane's own
pods; `pod-security ... privileged` lets the `proxy-init` init container (which edits firewall rules) run.

## 2.4 Create the certificates

[`manifests/10-cert-manager-linkerd-identity.yaml`](../manifests/10-cert-manager-linkerd-identity.yaml) defines four objects:

| Object | Purpose |
|---|---|
| `Issuer linkerd-selfsigned` | a throw-away self-signer used only to create the root |
| `Certificate linkerd-trust-anchor` | the **root**: 10 years, ECDSA-256, `isCA: true`; stored in Secret `linkerd-trust-anchor` |
| `Issuer linkerd-trust-anchor` | a CA issuer that signs with that root |
| `Certificate linkerd-identity-issuer` | the **issuer**: `duration: 48h`, `renewBefore: 25h`, `isCA: true`; stored in Secret `linkerd-identity-issuer` |

```bash
kubectl apply -f manifests/10-cert-manager-linkerd-identity.yaml
kubectl -n linkerd wait --for=condition=Ready certificate/linkerd-trust-anchor certificate/linkerd-identity-issuer --timeout=2m
kubectl -n linkerd get certificate
```

You should see both `READY True`.

## 2.5 Export the trust anchor for the next step

The Linkerd chart needs the *public* part of the root certificate as a file:

```bash
kubectl -n linkerd get secret linkerd-trust-anchor -o jsonpath='{.data.ca\.crt}' | base64 -d > /tmp/linkerd-ca.crt
openssl x509 -in /tmp/linkerd-ca.crt -noout -subject -enddate
```

Expected: subject `CN=root.linkerd.cluster.local`, an end date about 10 years away. **Put that end date in the team calendar**
(the alert `LinkerdTrustAnchorExpiring` also warns 90 days before). Replacing a trust anchor is a planned multi-step operation
(see [10-operations.md](10-operations.md)).

## 2.6 Check before continuing

* [ ] `kubectl -n linkerd get certificate` shows both certificates `READY True`
* [ ] `kubectl -n linkerd get secret linkerd-identity-issuer -o jsonpath='{.type}'` prints `kubernetes.io/tls`
* [ ] `/tmp/linkerd-ca.crt` exists and has the expected subject and date

## 2.7 Roll back

Nothing uses these yet: `kubectl delete -f manifests/10-cert-manager-linkerd-identity.yaml`. (Do not do this once Linkerd runs:
the mesh would lose its issuer.)

Next: [03-step-2-install-linkerd.md](03-step-2-install-linkerd.md).
