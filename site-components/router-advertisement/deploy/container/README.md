# Router advertisement service on a control plane that runs services as containers on hosts

What the service is: [the platform page](../../../../docs/platform/router-advertisement-service.md). On a control
plane whose services run as containers on its hosts (for example a kolla-style deployment), run the same program in
the site's own Neutron server image on one controller, behind the site's API front end. Nothing is installed: the
program is bind-mounted.

## Run it

Copy `router_advertisement.py` to the controller (here `/etc/router-advertisement/router_advertisement.py`) and put
the OVN northbound client key, certificate and CA where the container can read them: the same files the Neutron
server uses for the northbound database fit. Then, with the image the Neutron server container runs:

```bash
podman run -d --name router_advertisement --restart unless-stopped \
  --network host --read-only --tmpfs /tmp:size=16m --user <non-root uid of the image> \
  --cap-drop ALL --security-opt no-new-privileges \
  -v /etc/router-advertisement/router_advertisement.py:/opt/router-advertisement/router_advertisement.py:ro \
  -v <directory with the OVN client files>:/etc/router-advertisement/ovn:ro \
  -e OVN_NB_CONNECTION=<ssl:host:port[,ssl:host:port...] of the northbound database> \
  -e OVN_NB_PRIVATE_KEY=/etc/router-advertisement/ovn/<key file> \
  -e OVN_NB_CERTIFICATE=/etc/router-advertisement/ovn/<certificate file> \
  -e OVN_NB_CA_CERT=/etc/router-advertisement/ovn/<CA file> \
  -e KEYSTONE_URL=<internal identity endpoint> \
  -e NEUTRON_URL=<internal network endpoint> \
  -e LISTEN_ADDRESS=<controller's internal address> -e LISTEN_PORT=<free port> \
  -e HOME=/tmp \
  <Neutron server image> python3 -I -B /opt/router-advertisement/router_advertisement.py
```
Add `-e OS_CACERT=<path>` (and its mount) when the internal endpoints use a CA the image does not trust, and
`TLS_CERTIFICATE` / `TLS_PRIVATE_KEY` when the front end connects with TLS. A `tcp:` northbound connection needs no
client files. The service refuses to start, naming the setting, when one is missing; its log is the container's.

## Publish it

Add a front end and back end on the site's API load balancer (for example HAProxy) that terminates TLS like the
other APIs, on an address the platform's jobs can reach, admits only the networks those jobs call from, and forwards to `<controller's internal address>:<port>`;
health check `GET /healthz`. Verify with `curl -sS https://<front end>/healthz`, then add the site's entry to the deployment's
realization as in [the OpenStack-on-OpenShift instructions](../rhoso/README.md#6-tell-the-platform-where-it-is),
with the URL of that front end.

## Remove

Take the site's entry out of the realization, then delete the front end and the container
(`podman rm -f router_advertisement`).
