# Router advertisement service on an OpenStack-on-OpenShift control plane

What the service is and why it exists: [the platform page](../../../../docs/platform/router-advertisement-service.md).
This directory installs it into the namespace of the OpenStack control plane, beside Neutron and the OVN databases.
Run the commands as a cluster user who may create these objects in that namespace. Replace every `<...>`.

## 1. Read the values from the running control plane

```bash
NS=<control plane namespace>
# The Neutron server image the site runs (the service runs in it unchanged):
oc -n "$NS" get neutronapi -o jsonpath='{.items[0].spec.containerImage}{"\n"}'
# The northbound database (cluster-internal address, ssl:<host>:<port>):
oc -n "$NS" get ovndbcluster -o jsonpath='{range .items[*]}{.spec.dbType}{" "}{.status.internalDbAddress}{"\n"}{end}'
# The Issuer that signs the OVN database certificates:
oc -n "$NS" get issuers
# The internal endpoints:
openstack endpoint list --interface internal --service identity -f value -c URL
openstack endpoint list --interface internal --service network -f value -c URL
# The Secret with the CA bundle of the internal endpoints (when they are TLS):
oc -n "$NS" get secrets | grep -i ca-bundle
```

## 2. The program

```bash
oc -n "$NS" create configmap router-advertisement-program \
  --from-file=router_advertisement.py=site-components/router-advertisement/router_advertisement.py \
  --dry-run=client -o yaml | oc apply -f -
```
After a change of the program, apply the ConfigMap again and restart: `oc -n "$NS" rollout restart deployment/router-advertisement`.

## 3. The objects

```bash
oc process -f site-components/router-advertisement/deploy/rhoso/router-advertisement.yaml \
  -p NAMESPACE="$NS" \
  -p IMAGE=<Neutron server image> \
  -p OVN_CA_ISSUER=<OVN CA issuer> \
  -p OVN_NB_CONNECTION=<ssl:host:port of the northbound database> \
  -p KEYSTONE_URL=<internal identity endpoint> \
  -p NEUTRON_URL=<internal network endpoint> \
  -p CA_BUNDLE_SECRET=<CA bundle Secret> \
  -p OS_CACERT=/etc/router-advertisement/ca/tls-ca-bundle.pem \
  -p ROUTE_HOST=<host name of the Route, or leave out> \
  -p ROUTE_ALLOWED_SOURCES="<CIDRs the platform's jobs call from, space-separated>" \
  | oc apply -f -
oc -n "$NS" rollout status deployment/router-advertisement
oc -n "$NS" logs deployment/router-advertisement | head
```
Leave `OS_CACERT` out when the internal endpoints are plain HTTP or signed by a CA the image already trusts. The
template creates a ServiceAccount without API permissions or token, the OVN client Certificate (and through it the
Secret `router-advertisement-ovn-client`), a Service (and through its annotation the service-CA Secret
`router-advertisement-tls`), a Route, a NetworkPolicy and the Deployment (non-root, read-only root file system,
resource limits, liveness on `/healthz`, readiness on `/readyz`: a pod that cannot reach the northbound database
takes no traffic). The NetworkPolicy admits to the pod only the cluster's ingress routers (the namespace
`INGRESS_NAMESPACE`, default `openshift-ingress`, and routers on the host network, which OpenShift labels
`policy-group.network.openshift.io/ingress`), on port 8443: no other pod of the cluster can reach the service or hold
its connections. The probes have a listener of their own on port 8081 (plain HTTP, `/healthz` and `/readyz` only),
so they never wait for a request slot; the kubelet reaches it from the node, which a NetworkPolicy does not block.
If the namespace denies egress by default, allow egress to the northbound database and the two internal endpoints.

### The Route's certificate

The platform's jobs verify the service's certificate with the CA bundle of the site's credential (or the system's
trust when the credential has none). Choose one:

- **A certificate from the CA of the site's public endpoints (recommended).** When a cert-manager issuer signs the
  site's public endpoints (on an OpenStack-on-OpenShift control plane, the namespace's public root CA issuer; list
  them with `oc -n "$NS" get issuers`), have it issue the service's certificate, and let the pod present it behind
  a passthrough Route:
  ```bash
  oc process -f site-components/router-advertisement/deploy/rhoso/route-certificate.yaml \
    -p NAMESPACE="$NS" -p ROUTE_HOST=<host name of the Route> -p ROUTE_CERT_ISSUER=<issuer> | oc apply -f -
  oc -n "$NS" wait certificate/router-advertisement-route --for=condition=Ready --timeout=120s
  ```
  then add to the `oc process` of the main template above: `-p ROUTE_HOST=<the same host name>
  -p ROUTE_TERMINATION=passthrough -p SERVING_CERT_SECRET=router-advertisement-route-tls`. The service loads a
  renewed certificate by itself.
- **The cluster's default ingress certificate (the default).** The Route re-encrypts to the pod and presents the
  certificate of the cluster's ingress. Then the CA bundle of the site's credential must also contain the CA that
  signs that certificate: for the ingress operator's own default certificate, the bundle in
  `oc -n openshift-config-managed get configmap default-ingress-cert -o jsonpath='{.data.ca-bundle\.crt}'`; for a
  custom default certificate, the CA that issued it.

## 4. Verify

```bash
HOST=$(oc -n "$NS" get route router-advertisement -o jsonpath='{.spec.host}')
curl -sS "https://$HOST/healthz"
# expected: {"guard": {"restoredSinceStart": 0, "suspended": [], "wanted": 0}, "ok": true, "ovn": "connected"}
```
With a project token and a router interface port of that project on a network attached with host advertisement:
```bash
TOKEN=$(openstack token issue -f value -c id)
curl -sS -H "X-Auth-Token: $TOKEN" "https://$HOST/v1/router-ports/<port id>"
```

## 5. Remove

In this order:
1. Take the site's entry out of the deployment's realization (step 6): the platform then treats the site as one
   without the service.
2. Optionally take `nat` off ports first: `PUT /v1/router-ports/<port id>/nat` with `{"enabled": false}`. Otherwise
   the tokens and the service's markers stay on the router ports (the markers are inert and leave with the ports);
   without the service nothing restores the token after Neutron rewrites the option.
3. Every object the template, the optional certificate template and the ConfigMap step created, and the Secrets
   cert-manager and the service CA made for them (cert-manager does not delete a Certificate's Secret):
   ```bash
   oc -n "$NS" delete --ignore-not-found \
     deployment/router-advertisement \
     route/router-advertisement \
     networkpolicy/router-advertisement \
     service/router-advertisement \
     secret/router-advertisement-tls \
     certificate/router-advertisement-route \
     secret/router-advertisement-route-tls \
     certificate/router-advertisement-ovn-client \
     secret/router-advertisement-ovn-client \
     serviceaccount/router-advertisement \
     configmap/router-advertisement-program
   ```

## 6. Tell the platform where it is

Nothing is registered in Keystone. Add this entry for the site to the deployment's realization (the installation's
`FABRIC_REALIZATION`, key `placement.routerAdvertisementBySite`), with the site's name as the platform knows it and
the URL of the service's root (without `/v1`):
```yaml
placement:
  routerAdvertisementBySite:
    <site name>:
      url: https://<service host>
```
The URL only has to be reachable from where the platform's jobs run; it need not be public. It must be `https://`,
with a certificate that the CA bundle of the site's credential verifies (or the system's trust when the credential
has none): see [the Route's certificate](#the-routes-certificate).
