# Getting started as a tenant

Your organisation is a **tenant**. A **site** is one place where infrastructure runs (an OpenShift
Virtualization cluster, an OpenStack cloud, an AWS region, a Google Cloud region). Inside your
tenant you create **environments** (each one is your slice of a site: a VM environment, an
OpenStack project, an AWS account or a Google Cloud project) and **networks** (private address
spaces that can span environments and sites). You attach a network to an environment to get an
address block there. **Access** says which groups of people hold which role in the tenant.
Every environment gets a DNS zone (public where the site supports it).

This page takes you from your first sign-in to a VM on a network. The examples use the tenant
`example-corp`; replace it with your tenant's name. Your tenant's objects live in the tenant
namespace `entity-<tenant>` on the services cluster.

## 1. Where you work

You can do everything on this page in either of these, with the same sign-in:

- the **tenant dashboard**, `https://tenancy-dashboard-sovereign-cloud.apps.<services apps domain>/`.
  It opens on your tenant at `/t/<tenant>`;
- the **OpenShift web console** of the services cluster, with the Sovereign Cloud pages for
  tenants, or the command line:

  ```bash
  oc login --web https://api.<services cluster domain>:6443
  ```

Ask your platform team for the two domains.

## 2. Sign in

On the services cluster the sign-in page offers two buttons:

- **sovereign-services**: this is yours. It takes you to the platform's sign-in page, where you
  sign in with the account the platform team gave you.
- **kube:admin** (where it is shown): the platform team's emergency account. Do not use it.

On an OpenShift Virtualization site (to work with your VMs, step 6), the button is
**sovereign-tenants**.

**Signed in, but no tenant?** Then you are not in any of your tenant's access groups yet, or
your access group holds no role. Ask your tenant admin or the platform team to add you (step 8).

## 3. Find your tenant and what you may do

The tenant dashboard shows the tenants you belong to; pick yours. On the command line:

```bash
oc get projects                      # your tenant namespace entity-<tenant> is listed
oc auth can-i create hybridnetworks -n entity-<tenant>
```

What you can create depends on the roles your access groups hold (for example network admin,
VM environment admin, tenant admin). Your tenant admin knows which ones you have.

The sites your tenant may use are listed by the platform team. A tenant profile that lists them
in the product is not available yet; ask the platform team for the site names.

## 4. Create a network

A network is a private, routed address space that can span sites. It is isolated from every
other network, so its addresses may repeat elsewhere. Pick an address space that does not clash
with the networks you connect to it, for example a `/16`:

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: HybridNetwork
metadata:
  name: example-net
  namespace: entity-example-corp
spec:
  description: Network of the example application.
  prefixes:
    - 10.20.0.0/16
```

Save it as `example-net.yaml` and apply it with `oc apply -f example-net.yaml`, or create it on
the **Networks** page of the dashboard.

## 5. Create a VM environment and attach the network

A VM environment (OpenShift Virtualization environment) is your slice of an OpenShift
Virtualization site. Each namespace you list becomes a namespace on the site; a namespace that
names a network gets that network as its VMs' network.

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: CloudVirt
metadata:
  name: example-vms
  namespace: entity-example-corp
spec:
  cloudRef:
    name: site-vm
  namespaces:
    - name: vms
      network: example-net
  toolRbac:
    editRbac:
      - example-network-team
```

`cloudRef.name` is the site (ask the platform team); `toolRbac.editRbac` lists your access
groups whose members may create and run VMs there.

Then attach the network to the environment. A network placement gives the network an address
block in this environment, here a `/24` from the network's address space:

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: NetworkPlacement
metadata:
  name: example-net-vms
  namespace: entity-example-corp
spec:
  network: example-net
  backend:
    kind: CloudVirt
    name: example-vms
  ipv4:
    prefixLength: 24
```

An **Attach network** dialog on the environment page is not available yet; create the placement
on the **Network placements** page (under **Networks**) or with `oc apply`.

## 6. Wait until it is ready, then open the site console

```bash
oc get hybridnetworks,cloudvirts,networkplacements -n entity-<tenant>
```

Each object shows whether it is ready, and `status.message` says what the platform is doing or
waiting for. A new environment and its placement are usually ready within a few minutes. The
placement waits for the environment, and the environment's namespace joins the network once the
placement is ready.

When the environment is ready, its status tells you where to go:

```bash
oc get cloudvirt example-vms -n entity-<tenant> -o jsonpath='{.status.site.console}{"\n"}'
oc get cloudvirt example-vms -n entity-<tenant> -o jsonpath='{range .status.namespaces[*]}{.namespace}{"\n"}{end}'
```

Open the console, sign in with **sovereign-tenants**, and create your VM in the namespace the
second command printed. Members of the access groups in `toolRbac` can sign in there; others see
nothing.

## 7. DNS

Every environment gets a DNS zone `<environment>.<tenant>.<tenant DNS domain>`, public where the
site supports it. Ask the platform team for the tenant DNS domain.

## 8. Add a colleague

People belong to access groups; access groups hold roles. To add a colleague, ask the platform
team to add them to the right access group (`<tenant>/<access group>` in the sign-in directory).
Adding people yourself is not available yet. After the change, your colleague signs out and in
again.

## 9. When something does not get ready

Read `status.message` and the conditions (`oc describe` shows both). If the cause is fixed and
the object still does not move, ask the platform to process it again now (**Retry now**):

```bash
oc annotate hybridnetwork example-net -n entity-<tenant> \
  ansible.sdk.operatorframework.io/reconcileNow="$(date -u +%Y-%m-%dT%H:%M:%SZ)" --overwrite
```

## 10. Delete, in order

Delete placements first, then the environments and networks. A delete that has to wait says what
it is waiting for in its status. An object with `spec.deletionProtection: true` is not deleted
with the tenant; turn protection off first.

## Next

- [Hybrid networks tutorial](../../architecture/docs/tutorial/09-hybrid-networks.md): networks
  across several sites, OpenStack placements, internet egress.
- [Samples](../../samples/README.md): one example per kind of object.
