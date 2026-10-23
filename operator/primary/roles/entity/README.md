# entity (primary operator role)

Reconciles an Entity on the services cluster: creates the tenant namespace `entity-<name>`,
deploys the tenant's namespace operator into it (`tasks/deploy_namespace_operator.yml`, on every
reconcile, before the "already processed" skip) and publishes the Entity event for the EDA jobs.

## Namespace operator image and the canary annotation

Every tenant runs the default namespace operator image, `NAMESPACE_OPERATOR_IMAGE` of the primary
operator (chart values `namespaceOperator.image.repository` / `namespaceOperator.image.tag`).

One tenant can run another image of the **same repository** first:

```
oc -n sovereign-cloud annotate entity <name> hybridsovereign.redhat/namespace-operator-image=<tag>
#   <tag>                     e.g. img-0.1.22           -> <default repository>:<tag>
#   <repository>:<tag>        same repository only
#   <repository>@sha256:<64 hex>
```

The annotation is watched, so setting or removing it re-runs the Entity and rolls only that
tenant's Deployment. The pod template carries
`hybridsovereign.redhat/namespace-operator-image-source: canary|default`. A value outside the
default repository, or a malformed one, is ignored: the default image is used and a Warning Event
`NamespaceOperatorImageRejected` is written on the Entity.

Check which image a tenant runs:

```
oc -n entity-<name> get deploy hybridsovereign-namespace-operator \
  -o jsonpath='{.spec.template.spec.containers[0].image}{" "}{.spec.template.metadata.annotations.hybridsovereign\.redhat/namespace-operator-image-source}{"\n"}'
```

Promotion procedure:

1. Push the new namespace operator image (new tag; never reuse a pushed tag).
2. Annotate one low-stakes tenant with the tag; watch its operator (pod Ready, its objects keep
   reconciling, no restarts). Then **Retry now** on one of the tenant's objects (annotation
   `ansible.sdk.operatorframework.io/reconcileNow`): a forced reconcile always publishes, so the
   object must show `Published=True` with a `status.delivery.publishedAt` later than the request.
   An image that cannot reach the event brokers fails here and nowhere earlier. Then a second tenant.
3. Set the tag as the default (`namespaceOperator.image.tag` in the platform values) and sync the
   primary operator. Its startup resync redeploys every tenant's operator with the new default.
4. Remove the annotation from the canary tenants (they now run the default anyway).

Rollback of a canary: remove the annotation. Rollback of a promotion: restore the previous tag.

## Kafka protocol version of the event producer

The operators publish their events with a pinned Kafka protocol version (the image default), so a
publish does not first probe the brokers. Chart value `amqKafka.apiVersion` overrides it for the
primary operator (environment `KAFKA_API_VERSION`), and this role passes the same value to every
tenant's namespace operator: empty keeps the image default, `auto` probes the brokers, anything
else is a dotted version such as `2.6.0`. Changing it restarts the primary operator; each tenant's
Deployment picks it up on that tenant's next Entity reconcile (its startup resync).

The selection logic is in `tasks/namespace_operator_image.yml`; its cases run without a cluster:

```
NAMESPACE_OPERATOR_IMAGE=quay.example/shc/hybridsovereign-namespace-operator:img-0.1.21 \
  ansible-playbook -i localhost, -c local operator/primary/roles/entity/tests/test_namespace_operator_image.yml
```

Note: any change to the Deployment template in this role (arguments, probes, annotations) also
redeploys every tenant's operator when the new primary operator starts, with the image each tenant
already runs.
