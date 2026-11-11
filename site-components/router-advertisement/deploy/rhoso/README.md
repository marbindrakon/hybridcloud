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
template creates a ServiceAccount without API permissions or token, the OVN client Certificate, a Service with a
serving certificate from the cluster's service CA, a re-encrypting Route and the Deployment (non-root, read-only
root file system, resource limits, liveness on `/healthz`, readiness on `/readyz`, which fails while the northbound
database does not answer). The Route admits only `ROUTE_ALLOWED_SOURCES`. If the namespace has NetworkPolicies,
allow ingress to port 8443 from the cluster's ingress routers and egress to the northbound database and the two
internal endpoints.

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

Take the site's entry out of the deployment's realization first (the platform then treats the site as one without
the service), then:
```bash
oc process -f site-components/router-advertisement/deploy/rhoso/router-advertisement.yaml -p NAMESPACE="$NS" \
  -p IMAGE=x -p OVN_CA_ISSUER=x -p OVN_NB_CONNECTION=x -p KEYSTONE_URL=x -p NEUTRON_URL=x -p CA_BUNDLE_SECRET=x \
  | oc delete -f -
oc -n "$NS" delete configmap router-advertisement-program
```
The `nat` tokens and the service's markers stay on the router ports; without the service nothing restores the token
after Neutron rewrites the option. To take the token off a port first, call `PUT /v1/router-ports/<port id>/nat`
with `{"enabled": false}` before removing the service.

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
has none): with the Route above, that is the certificate of the cluster's ingress.
