{{- /* Fabric health (fabric-health.yaml): the metrics configuration of its kube-state-metrics instance and the alert
rules, as named templates (the rules are also rendered on their own by the chart test). */}}
{{- define "shc-cluster-base.fabricMetricsConfig" -}}
kind: CustomResourceStateMetrics
spec:
  resources:
    {{- range $kind := list "HybridFabric" "CloudGateway" }}
    - groupVersionKind: {group: hybridsovereign.redhat, version: v1alpha1, kind: {{ $kind }}}
      metricNamePrefix: shc_fabric
      labelsFromPath:
        name: [metadata, name]
        namespace: [metadata, namespace]
      metrics:
        - name: info
          help: One series per object.
          each:
            type: Info
            info:
              labelsFromPath:
                phase: [status, phase]
        - name: condition
          help: The object's conditions; the value is the condition's last transition time (Unix seconds).
          each:
            type: Gauge
            gauge:
              path: [status, conditions]
              labelsFromPath:
                type: [type]
                status: [status]
                reason: [reason]
              valueFrom: [lastTransitionTime]
        - name: node_reachable
          help: >-
            Per node of the latest health check, 1 while the node answered; labels node and the node's worst counted
            finding (reason, message; empty when none).
          each:
            type: Gauge
            gauge:
              path: [status, health, nodes]
              labelsFromPath:
                node: [name]
                reason: [reason]
                message: [message]
              valueFrom: [reachable]
        - name: health_checked_at
          help: When the health check last read the nodes (Unix seconds).
          each:
            type: Gauge
            gauge:
              path: [status, health]
              valueFrom: [checkedAt]
        {{- if eq $kind "HybridFabric" }}
        - name: health_last_run_at
          help: When the health check last ran for the fabric, whether or not it read the nodes (Unix seconds).
          each:
            type: Gauge
            gauge:
              path: [status, health]
              valueFrom: [lastRunAt]
        - name: health_interval_seconds
          help: The health check's interval.
          each:
            type: Gauge
            gauge:
              path: [status, realization, health]
              valueFrom: [intervalSeconds]
        - name: health_enabled
          help: 1 while the health check is on for the fabric, 0 when the realization turns it off.
          each:
            type: Gauge
            gauge:
              path: [status, realization, health]
              valueFrom: [enabled]
        - name: egress6_node_healthy
          help: Per node, 1 while the IPv6 egress has an upstream session and the default route.
          each:
            type: Gauge
            gauge:
              path: [status, realization, egress, ipv6, nodes]
              labelFromKey: node
              valueFrom: [healthy]
        {{- range $m := list (list "pool_blocks_free" "blocksFree") (list "pool_blocks_used" "blocksUsed") (list "pool_blocks_held_down" "blocksHeldDown") }}
        - name: {{ index $m 0 }}
          help: Platform address pool blocks ({{ index $m 1 }}).
          each:
            type: Gauge
            gauge:
              path: [status, realization, ipam, pools]
              labelsFromPath:
                pool: [name]
              valueFrom: [{{ index $m 1 }}]
        {{- end }}
        {{- end }}
    {{- end }}
{{- end }}

{{- define "shc-cluster-base.fabricHealthRules" -}}
{{- $f := .Values.ops.fabricHealth -}}
- name: shc-fabric-health
  rules:
    # per node (shc_fabric_node_reachable carries the node's worst counted finding: reason and message): a node or
    # the configuration on it does not work (a fabric's unreachable node: only on the HybridFabric, its gateways read
    # the same). A gateway of a paused or not-checked site has no node rows and never matches
    - alert: ShcFabricNodeUnhealthy
      expr: >-
        max by (customresource_kind, namespace, name, node, reason, message) (
          shc_fabric_node_reachable{reason=~"StartFailed|StartIncomplete|ServiceFailed|RoutingDaemonDown"}
          or shc_fabric_node_reachable{customresource_kind="HybridFabric", reason="NodeUnreachable"}
        )
      for: {{ $f.alerts.nodeUnhealthyFor }}
      labels:
        severity: critical
      annotations:
        summary: "{{ "{{" }} $labels.customresource_kind {{ "}}" }} {{ "{{" }} $labels.namespace {{ "}}" }}/{{ "{{" }} $labels.name {{ "}}" }}: {{ "{{" }} $labels.reason {{ "}}" }} on {{ "{{" }} $labels.node {{ "}}" }}"
        description: "{{ "{{" }} $labels.message {{ "}}" }}. The fabric health check requested a full run where one repairs it; status.health has every finding."
    - alert: ShcFabricRoutingDaemonRestarted
      expr: >-
        max by (customresource_kind, namespace, name, node, message) (shc_fabric_node_reachable{reason="RoutingDaemonRestarted"})
      labels:
        severity: warning
      annotations:
        summary: "{{ "{{" }} $labels.customresource_kind {{ "}}" }} {{ "{{" }} $labels.namespace {{ "}}" }}/{{ "{{" }} $labels.name {{ "}}" }}: a routing daemon crashed on {{ "{{" }} $labels.node {{ "}}" }}"
        description: "{{ "{{" }} $labels.message {{ "}}" }}. A core dump is on the node."
    - alert: ShcFabricDriftNotRepaired
      expr: >-
        max by (customresource_kind, namespace, name, node) (shc_fabric_node_reachable{reason="ConfigurationDrift"})
      for: {{ $f.alerts.driftFor }}
      labels:
        severity: warning
      annotations:
        summary: "{{ "{{" }} $labels.customresource_kind {{ "}}" }} {{ "{{" }} $labels.namespace {{ "}}" }}/{{ "{{" }} $labels.name {{ "}}" }}: {{ "{{" }} $labels.node {{ "}}" }} differs from its configuration"
        description: "The node's state differs from what the last apply or routing configuration load left, and a full run did not repair it. status.health has the lines that differ."
    - alert: ShcFabricSiteSessionsDown
      expr: >-
        max by (namespace, name, node) (shc_fabric_node_reachable{customresource_kind="CloudGateway", reason="SiteSessionsDown"})
      for: {{ $f.alerts.siteSessionsFor }}
      labels:
        severity: warning
      annotations:
        summary: "CloudGateway {{ "{{" }} $labels.namespace {{ "}}" }}/{{ "{{" }} $labels.name {{ "}}" }}: no routing session with its site on {{ "{{" }} $labels.node {{ "}}" }}"
        description: "The node holds no session with the site. A site that is off on purpose is paused (CloudInfrastructure spec.paused), which this alert ignores."
    # the fabric's service VRF: a node with a platform circuit but no established session with the hub's router
    - alert: ShcFabricServiceSessionsDown
      expr: >-
        max by (namespace, name, node) (shc_fabric_node_reachable{customresource_kind="HybridFabric", reason="ServiceSessionsDown"})
      for: {{ $f.alerts.serviceSessionsFor }}
      labels:
        severity: warning
      annotations:
        summary: "HybridFabric {{ "{{" }} $labels.namespace {{ "}}" }}/{{ "{{" }} $labels.name {{ "}}" }}: no session with the hub's router on {{ "{{" }} $labels.node {{ "}}" }}"
        description: "The node has a platform circuit (AttachmentCircuit ceKind platform) but no established BGP session on it; the platform reaches the clusters on tenant networks through the other nodes, or not at all when every node reports it."
    # a network's service link: the node's platform circuit is up, the iBGP session between the service VRF and that
    # network's VRF is not (the network's VRF on the node lacks the hub's ranges)
    - alert: ShcFabricServiceLinkDown
      expr: >-
        max by (namespace, name, node) (shc_fabric_node_reachable{customresource_kind="HybridFabric", reason="ServiceLinkDown"})
      for: {{ $f.alerts.serviceLinksFor }}
      labels:
        severity: warning
      annotations:
        summary: "HybridFabric {{ "{{" }} $labels.namespace {{ "}}" }}/{{ "{{" }} $labels.name {{ "}}" }}: a network's session with the platform's connection is down on {{ "{{" }} $labels.node {{ "}}" }}"
        description: "The node's platform circuit is up but the session over a network's service link is not, so that network's VRF on the node does not learn the hub's ranges; the network reaches the hub through the other nodes, or not at all when every node reports it. status.health names the network."
    - alert: ShcFabricIPv6EgressUpstreamDown
      expr: >-
        max by (namespace, name) (shc_fabric_condition{customresource_kind="HybridFabric", type="IPv6EgressUpstream", status="False"})
      for: {{ $f.alerts.upstreamDownFor }}
      labels:
        severity: warning
      annotations:
        summary: "HybridFabric {{ "{{" }} $labels.namespace {{ "}}" }}/{{ "{{" }} $labels.name {{ "}}" }}: a node has no IPv6 upstream"
        description: "A node has no IPv6 upstream session or default route; the networks' IPv6 internet traffic takes the other node(s)."
    # a wrong service.* setting: the nodes keep the platform's connection for clusters on networks as last applied (or,
    # never applied, it is not realized); the condition ServiceRouting names the setting
    - alert: ShcFabricServiceSettingInvalid
      expr: >-
        max by (namespace, name) (shc_fabric_condition{customresource_kind="HybridFabric", type="ServiceRouting", status="False"})
      for: {{ $f.alerts.upstreamDownFor }}
      labels:
        severity: warning
      annotations:
        summary: "HybridFabric {{ "{{" }} $labels.namespace {{ "}}" }}/{{ "{{" }} $labels.name {{ "}}" }}: a setting of the platform's connection for clusters on networks is wrong"
        description: "The fabric keeps the connection as last applied on its nodes (or, never applied, does not realize it) until the setting is fixed: no new cluster address is carried. The condition ServiceRouting names the setting."
    - alert: ShcFabricIPv6EgressDown
      expr: >-
        count by (namespace, name) (shc_fabric_egress6_node_healthy)
        unless on (namespace, name) (shc_fabric_egress6_node_healthy == 1)
      for: {{ $f.alerts.upstreamDownFor }}
      labels:
        severity: critical
      annotations:
        summary: "HybridFabric {{ "{{" }} $labels.namespace {{ "}}" }}/{{ "{{" }} $labels.name {{ "}}" }}: no node has an IPv6 upstream"
        description: "IPv6 internet egress is down for every network of the fabric."
    # the check itself: not run (AAP, the schedule or the job broken, the nodes' lease held for long) or never run
    # the check itself: no run reported (AAP, the schedule or the job broken)
    - alert: ShcFabricHealthStale
      expr: >-
        (time() - shc_fabric_health_last_run_at{customresource_kind="HybridFabric"})
        > on (namespace, name) ({{ $f.alerts.staleIntervals }} * shc_fabric_health_interval_seconds{customresource_kind="HybridFabric"})
        unless on (namespace, name) (shc_fabric_health_enabled == 0)
      for: 10m
      labels:
        severity: warning
      annotations:
        summary: "HybridFabric {{ "{{" }} $labels.namespace {{ "}}" }}/{{ "{{" }} $labels.name {{ "}}" }}: no fabric health run for {{ "{{" }} $value | humanizeDuration {{ "}}" }}"
        description: "The fabric health check has not run for {{ "{{" }} $value | humanizeDuration {{ "}}" }} (more than {{ $f.alerts.staleIntervals }} intervals). Check the fabric-health job template's schedule and its last runs."
    # runs report but leave the nodes alone: changes to them held the configuration lease for that long (a long
    # rollout, or a lease that is not released)
    - alert: ShcFabricNodesNotRead
      expr: >-
        (time() - shc_fabric_health_checked_at{customresource_kind="HybridFabric"})
        > on (namespace, name) ({{ $f.alerts.notReadIntervals }} * shc_fabric_health_interval_seconds{customresource_kind="HybridFabric"})
        unless on (namespace, name) (shc_fabric_health_enabled == 0)
      for: 10m
      labels:
        severity: info
      annotations:
        summary: "HybridFabric {{ "{{" }} $labels.namespace {{ "}}" }}/{{ "{{" }} $labels.name {{ "}}" }}: the nodes have not been health-checked for {{ "{{" }} $value | humanizeDuration {{ "}}" }}"
        description: "The health check runs but leaves the nodes alone while a change to them holds the configuration lease (status.health.skipped). Expected during a long rollout; otherwise check which job holds the lease fabric-config-<fabric>."
    - alert: ShcFabricHealthNeverChecked
      expr: >-
        shc_fabric_info{customresource_kind="HybridFabric"}
        unless on (namespace, name) shc_fabric_health_last_run_at{customresource_kind="HybridFabric"}
        unless on (namespace, name) (shc_fabric_health_enabled == 0)
      for: 1h
      labels:
        severity: warning
      annotations:
        summary: "HybridFabric {{ "{{" }} $labels.namespace {{ "}}" }}/{{ "{{" }} $labels.name {{ "}}" }}: the fabric health check never ran"
        description: "The fabric-health job template has no schedule, or its runs fail before they read the nodes."
    - alert: ShcPlatformPoolLow
      expr: >-
        shc_fabric_pool_blocks_free
        / on (namespace, name, pool) (shc_fabric_pool_blocks_free + shc_fabric_pool_blocks_used + shc_fabric_pool_blocks_held_down)
        < {{ $f.alerts.poolFreePercent }} / 100
      for: 1h
      labels:
        severity: warning
      annotations:
        summary: "HybridFabric {{ "{{" }} $labels.namespace {{ "}}" }}/{{ "{{" }} $labels.name {{ "}}" }}: platform address pool {{ "{{" }} $labels.pool {{ "}}" }} below {{ $f.alerts.poolFreePercent }} % free"
        description: "Few free blocks are left in the pool (blocks in hold-down count as not free). Add prefixes to the pool or shorten its hold-down."
{{- end }}
