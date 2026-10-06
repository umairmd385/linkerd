# 7. Step 6: move public DNS to the new load balancer

**Goal:** `exchange.example.com`, `api...`, `admin...`, etc. resolve to the new Envoy Gateway NLB. **Impact:** a short DNS
switch (about a minute for Route 53 alias records, longer for clients that cache DNS). Both load balancers can serve the same
backends in the meantime, so the overlap is harmless.

> Skip this step on a brand-new environment with no previous load balancer: just create the DNS records once.

## 7.1 Before you start

* [ ] Step 4 tests passed against the new NLB (all hostnames answer correctly with the `Host` header trick).
* [ ] Step 5 done for at least the services behind those hostnames (or you accept plain edge → service traffic for now).
* [ ] You know the Route 53 **hosted zone ID** and have permission to change records.
* [ ] A quiet period is chosen, and someone is watching Grafana (**Linkerd / Overview**) and the edge access log.

## 7.2 Collect the facts

```bash
ZONE=$(aws route53 list-hosted-zones-by-name --dns-name example.com --max-items 1 \
        --query 'HostedZones[0].Id' --output text | sed 's#/hostedzone/##')
NLB=$(kubectl -n envoy-gateway-system get gateway prod-edge-gateway -o jsonpath='{.status.addresses[0].value}')
NLB_ZONE=$(aws elbv2 describe-load-balancers --query "LoadBalancers[?DNSName=='$NLB'].CanonicalHostedZoneId" --output text)
echo "zone=$ZONE  nlb=$NLB  nlb_zone=$NLB_ZONE"

# save today's records: this file is your rollback
aws route53 list-resource-record-sets --hosted-zone-id $ZONE --output json > zone-before-cutover.json
```

## 7.3 Build the change batch

For every public hostname currently pointing at the old load balancer, UPSERT an **alias A record** to the new NLB. A template
for one record (repeat per hostname):

```json
{
  "Comment": "Move to Envoy Gateway NLB",
  "Changes": [
    {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "exchange.example.com.",
        "Type": "A",
        "AliasTarget": {
          "HostedZoneId": "<NLB_ZONE>",
          "DNSName": "<NLB dns name>.",
          "EvaluateTargetHealth": false
        }
      }
    }
  ]
}
```

Hostnames in production: `admin`, `api`, `chart`, `exchange`, `socket`, `prod-argocd`, `prod-grafana`, `prod-jenkins`,
`prod-phpmyadmin` (all under `example.com`). **Do not move records that do not currently point at the old NLB.**

Prepare the **rollback batch** at the same time: the same UPSERTs, using the old values from `zone-before-cutover.json`.

## 7.4 Apply and watch

```bash
aws route53 change-resource-record-sets --hosted-zone-id $ZONE --change-batch file://cutover.json

# after ~60 seconds
for h in exchange api admin socket chart; do printf "%-10s %s\n" $h "$(dig +short $h.example.com | head -2 | tr '\n' ' ')"; done
dig +short $NLB | head -2                      # the addresses above should match these
```

Then, for at least 30 minutes:

* log in and use the exchange; open a WebSocket session (`socket`);
* Grafana **Linkerd / Overview**: success rate stays at ~100%, no new 5xx;
* edge access log: `log_type:edge_access` in VictoriaLogs shows requests from real client addresses;
* the old load balancer's request count drops to zero.

## 7.5 Roll back

```bash
aws route53 change-resource-record-sets --hosted-zone-id $ZONE --change-batch file://rollback.json
```

## 7.6 After a clean day

Delete the old load balancer's records, the old ingress, and anything that was only there for it. Production kept the old stack
for the observation period, then removed Istio completely (6 October 2026). **Before deleting the old stack, confirm its access
log shows no requests for several minutes**, because slow DNS caches can still send some clients to it.

Next: [08-step-7-monitoring-and-logs.md](08-step-7-monitoring-and-logs.md).
