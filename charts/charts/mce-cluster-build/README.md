# mce-cluster-build

Helm chart that builds an OpenShift cluster through MCE/Hive on the management cluster: a `ClusterDeployment` with
its secrets (pull secret, SSH key, provider credentials from the secret store through ExternalSecrets), the install
configuration per provider, optional install-time manifests, and the `ManagedCluster` import. Providers
(`provider.type`): `AWS` (Mint and STS), `GCP` (workload identity), `OpenStack`, `vSphere` and `Agent` (bare metal
through assisted-service, `AgentClusterInstall` on a shared or dedicated `InfraEnv`).

Every value is documented in [values.yaml](values.yaml). Installation-specific values (names, domains, address
ranges) belong to the installation's values files, not to the chart.

## Agent provider

- `provider.agent.infraEnvType`: `shared` binds the cluster's Agents out of an existing late-binding `InfraEnv`
  (`sharedInfraEnv`); `dedicated` renders an `InfraEnv` and NMState configurations for the cluster.
- `provider.agent.useExternalLB`: `true` leaves API and ingress to an external load balancer (user-managed
  networking); `false` makes the cluster run its own VIPs, given as `cluster.apiVIP` / `cluster.ingressVIP`.
- `provider.agent.machineCIDR`: the machine network when `provider.agent.networking` names none.
- `provider.agent.networking` (optional): the cluster's networks per address family, each list in the
  `AgentClusterInstall` shape. Without it the chart renders the single-stack defaults (cluster network
  `10.128.0.0/14` with hostPrefix 23, service network `172.30.0.0/16`, `machineCIDR`). A dual-stack build lists the
  IPv4 entry first in each list:

  ```yaml
  cluster:
    apiVIPs: [198.51.100.5, "2001:db8:1:238::5"]        # optional: a VIP per family instead of apiVIP/ingressVIP
    ingressVIPs: [198.51.100.7, "2001:db8:1:238::7"]
  provider:
    type: Agent
    agent:
      networking:
        clusterNetwork:
          - {cidr: 10.128.0.0/14, hostPrefix: 23}
          - {cidr: "fd01::/48", hostPrefix: 64}
        serviceNetwork: [172.30.0.0/16, "fd02::/112"]
        machineNetwork:
          - {cidr: 198.51.100.0/24}
          - {cidr: "2001:db8:1:238::/64"}
  ```

  With `cluster.apiVIPs` / `cluster.ingressVIPs` empty, a dual-stack cluster keeps IPv4-only VIPs (`apiVIP`,
  `ingressVIP`): the API and ingress are then reached over IPv4 while nodes, pods and services are dual-stack.
- The networks are install-time settings: the chart does not convert a running cluster. The day-2 conversion of a
  single-stack cluster is described in the platform documentation
  ([Convert an OpenShift Virtualization site to dual-stack](../../../docs/platform/metal-dual-stack-conversion.md));
  a rebuild with the values above is its clean rollback.

## Versions

The chart version (`Chart.yaml`) is bumped on every change; the management cluster's values pin the version the
cluster-build jobs deploy.
