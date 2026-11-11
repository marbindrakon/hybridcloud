# Router advertisement service

For the platform team and the administrators of an OpenStack site. The router advertisement service is a small
program that runs on an OpenStack site's control plane. It does one thing: it puts the `nat` token into the OVN
option that decides what a Neutron router advertises over the site's EVPN, for router interface ports of the
caller's own project, and keeps it there. With it, the floating IPs behind a router whose interface is on a network
the platform has placed on the site are reachable from the fabric. The platform uses it when the site has it and
assumes the site advertises floating IPs by itself when it does not.

## Why it exists, and why it is temporary

OVN (25.03 and later) advertises a router's NAT addresses over EVPN when the router's port on the network carries
`nat` in `options:dynamic-routing-redistribute`. Neutron's EVPN service plugin sets that option with
`connected-as-host` only, and the option cannot be set through the Neutron API. The platform reaches an OpenStack
site only through the OpenStack API, so something on the site's control plane has to write the option.

The service is that something until Neutron sets `nat` itself. Once the site's Neutron does, every call is a no-op
and the guard finds nothing to restore; the service can then be removed from the site with no change in the
platform.

## What it can and cannot do

- It reads and writes only the `nat` token in `options:dynamic-routing-redistribute`, and its own marker
  `options:hybrid-fabric-nat=wanted`, on the OVN logical router port `lrp-<port id>` of a Neutron router interface
  port. Every other option key and every other token stays as it is, in its order. Nothing else in OVN is read or
  written.
- It never creates the redistribute option: a router port whose network is not attached with host advertisement is
  refused (`NotAnAdvertisingPort`).
- It holds no OpenStack credential. Each call carries the caller's Keystone token; the service validates the token
  against Keystone and asks Neutron, with that same token, for the port. A caller may switch `nat` only on router
  interface ports of the project its token is scoped to. The platform's own routers are in the platform's project,
  so a tenant's token cannot touch them.
- It has no state of its own. The wish ("this port should advertise NAT addresses") is the marker on the port,
  written in the same database transaction as the token. A restarted or replaced service finds every wish in the
  database.
- **The guard.** Neutron can rewrite the option between two platform runs (its repair, or the router interface's
  advertisement being set again). The service watches the router ports and, at its start, after every reconnect to
  the database, on every change of a port and in a full pass every `GUARD_INTERVAL_SECONDS` (default 60), appends
  `nat` again on every marked port whose redistribute option is there without it. Neutron keeps option keys it does
  not manage, so the marker survives its rewrites; OVN ignores the marker. One log line per restore
  (`guard: restored nat on lrp-…`).
  - A marked port whose redistribute option has disappeared is left alone and logged once: the network no longer
    advertises hosts, and the next platform run decides.
  - At most 5 restores of one port in 5 minutes: after that the guard stops restoring that port, logs an error and
    lists the port under `suspended` in `/healthz`, so it does not fight a writer that keeps removing the token.
    The next `PUT …/nat {"enabled": true}` for the port lifts the suspension.
  - A router interface that is removed and added again is a new port without the marker. The platform's network
    placement asks for `nat` again in the same run; a port recreated by hand stays without `nat` until the next
    platform run.
- **It only makes floating IPs reachable from outside the site.** It changes what the site advertises, not how the
  site forwards. Whether a tenant router whose gateway is on a placed network (a tunnelled network without a
  physical network) carries traffic at all depends on the site's Neutron: some versions do not schedule such a
  gateway on any chassis, and then the router has no internet access and its floating IPs answer nobody, with or
  without `nat`. An administrator sees it on the router's gateway port: `openstack port list --device-owner
  network:router_gateway --network <network>` shows its status `DOWN` (and `openstack port show <port id>` no
  hosting chassis in its binding), where a working gateway is `ACTIVE`.

## API

JSON over HTTPS; an internal API of the platform, not registered in Keystone. The `/v1` calls need the header
`X-Auth-Token: <Keystone token>`.

| Call | Result |
|---|---|
| `GET /healthz` | 200 `{"ok": true, "ovn": "connected" or "unreachable", "guard": {"wanted": <n>, "restoredSinceStart": <n>, "suspended": [<port id>, ...]}}`; no authentication, port ids only; the liveness probe |
| `GET /readyz` | 200 `{"ready": true, "ovn": "connected"}`, or 503 `{"ready": false, "ovn": "unreachable"}` while the northbound database is not connected; no authentication; the readiness probe |
| `GET /v1/router-ports/{port_id}` | 200 `{"portId", "lrp", "redistribute": [...], "nat": true or false, "wanted": true or false, "restores": <n since start>, "lastRestoredAt": <time or null>}` |
| `PUT /v1/router-ports/{port_id}/nat` with `{"enabled": true}` or `{"enabled": false}` | 200 `{"portId", "lrp", "before": [...], "after": [...], "changed": true or false}`; `true` appends `nat` and sets the marker, `false` removes both; repeating a call changes nothing |

Errors are `{"error": "<reason>", "message": "..."}`:

| Status | Reason | When |
|---|---|---|
| 400 | `BadRequest` | the port id is not a lower-case UUID, or the body is not exactly `{"enabled": <boolean>}` |
| 401 | `Unauthenticated` | no token, or Keystone does not accept it |
| 403 | `NotProjectScoped` | the token is not scoped to a project |
| 403 | `NotYourPort` | Neutron does not show the port to the caller, or the port belongs to another project |
| 403 | `NotARouterInterface` | the port is not a router interface |
| 404 | `NoSuchRouterPort` | OVN has no router port for it |
| 409 | `NotAnAdvertisingPort` | the router port has no redistribute option |
| 502 | `OvnUnavailable`, `IdentityUnavailable`, `NetworkUnavailable` | the northbound database, Keystone or Neutron did not answer in time |

`changed` is true when the call wrote anything (the token or the marker). Every write is logged in one line with
the user id, the project id, the port id and the token lists before and after. Tokens are never logged.

## Settings

Environment variables of the service; a missing required setting stops the start, naming the setting.

| Setting | Meaning |
|---|---|
| `OVN_NB_CONNECTION` | required: the northbound database, `ssl:<host>:<port>` or `tcp:<host>:<port>`, comma-separated for a cluster |
| `OVN_NB_PRIVATE_KEY`, `OVN_NB_CERTIFICATE`, `OVN_NB_CA_CERT` | required for `ssl:`: the client key, certificate and the CA of the database |
| `KEYSTONE_URL`, `NEUTRON_URL` | required: the site's internal identity and network endpoints |
| `OS_CACERT` | optional: the CA bundle for those endpoints |
| `LISTEN_ADDRESS`, `LISTEN_PORT` | default `0.0.0.0` and `8080` |
| `TLS_CERTIFICATE`, `TLS_PRIVATE_KEY` | optional, together: the service serves TLS itself (a renewed certificate is loaded within a minute) |
| `OVN_TIMEOUT_SECONDS` | default 10: the longest wait for one database transaction |
| `API_TIMEOUT_SECONDS` | default 10: the longest wait for one Keystone or Neutron call |
| `GUARD_INTERVAL_SECONDS` | default 60: the guard's full pass |
| `LOG_LEVEL` | default `INFO` |

## Install

The service has no image of its own: it runs in the site's own Neutron server image, which carries what it needs
(Python, `ovsdbapp`, the OVS Python bindings), with the program mounted in.

- OpenStack on OpenShift: [deploy/rhoso](../../site-components/router-advertisement/deploy/rhoso/README.md) (an
  OpenShift template: ServiceAccount without API access, the OVN client certificate from the namespace's OVN CA,
  Service, Route, Deployment; optionally a second template for the Route's certificate from the issuer of the
  site's public endpoints, so that the CA bundle of the platform's site credential verifies it).
- Control planes that run services as containers on hosts:
  [deploy/container](../../site-components/router-advertisement/deploy/container/README.md).

Then tell the platform where it is. Nothing is registered in Keystone: the deployment's realization (the
installation's `FABRIC_REALIZATION`) names the service's URL per site, under `placement.routerAdvertisementBySite`
(default `{}`):
```yaml
placement:
  routerAdvertisementBySite:
    <site name>:
      url: https://<service host>
```
- `<site name>` is the site as the platform knows it; `url` is the service's root, without `/v1`.
- The URL only has to be reachable from where the platform's jobs run; it need not be public. It must be
  `https://`: an `http://` URL is refused (`InvalidSetting`, naming the key). The jobs verify the certificate with
  the CA bundle of the site's credential when it has one, otherwise with the system's trust.
- A site without an entry is a site without the service: the platform assumes the site advertises floating IPs by
  itself.

## Verify

```bash
curl -sS https://<service host>/healthz
```
`"ovn": "connected"` means the database connection is up. With a project token and a router interface port of
that project on an advertised network, `GET /v1/router-ports/<port id>` shows the port's token list and whether
`nat` is wanted. After the platform has placed a network with router attachments on the site, `wanted` is `true` on
its router's port, and a floating IP associated behind that router appears in the network's routes on the border
gateway nodes within seconds.

## Remove

Take the site's entry out of `placement.routerAdvertisementBySite` first (the platform then goes back to assuming
the site advertises floating IPs), then remove the deployment. The tokens and markers stay on the ports; the markers are inert
and leave with the ports. To take `nat` off a port, call `PUT …/nat` with `{"enabled": false}` before removing the
service.
