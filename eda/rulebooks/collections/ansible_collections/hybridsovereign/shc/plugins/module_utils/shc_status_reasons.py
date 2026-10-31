# -*- coding: utf-8 -*-
# Reasons registry of the hybridsovereign.redhat status contract.
#
# Every condition reason written by the platform has one entry:
#   actor   A = automatic (nothing to do), T = the tenant can act, P = the platform team acts
#   class   ready | progress | waiting | failed | blocked
#   summary tenant-safe sentence (<= 140 characters) shown as status.summary
#
# Pure data, no imports: copied verbatim into charts/charts/shc-cluster-base/files/ by
# scripts/sync-shc-status.sh. Edit only this file.
from __future__ import absolute_import, division, print_function

__metaclass__ = type

READY = "ready"
PROGRESS = "progress"
WAITING = "waiting"
FAILED = "failed"
BLOCKED = "blocked"
PAUSED = "paused"


def _r(actor, klass, summary):
    return {"actor": actor, "class": klass, "summary": summary}


REASONS = {
    # ---- ready -------------------------------------------------------------------------
    "Ready": _r("A", READY, "Ready."),
    "AsExpected": _r("A", READY, "Ready."),
    "EDASucceeded": _r("A", READY, "Ready."),
    "ReconcileSucceeded": _r("A", READY, "Ready."),
    "Synced": _r("A", READY, "Ready; in sync."),
    "Verified": _r("A", READY, "Verified."),
    "Registered": _r("A", READY, "Registered."),
    "Declared": _r("A", READY, "Declared."),
    "PlatformEdge": _r("A", READY, "Ready."),
    "FabricUnderlayEstablished": _r("A", READY, "Ready."),
    "NeutronEvpnExtension": _r("A", READY, "Ready."),
    "ClusterBuildCompleted": _r("A", READY, "The cluster is built and ready."),
    "GroupClaimVerified": _r("A", READY, "Sign-in verified for a member of a granted access group."),
    "Granted": _r("A", READY, "Access granted to the named access groups."),
    # ---- progress ----------------------------------------------------------------------
    "Queued": _r("A", PROGRESS, "Queued; the change starts shortly."),
    "AwaitingEDA": _r("A", PROGRESS, "Queued; the change starts shortly."),
    "Publishing": _r("A", PROGRESS, "Queued; the change starts shortly."),
    "Published": _r("A", PROGRESS, "Queued; the change starts shortly."),
    "JobRunning": _r("A", PROGRESS, "Applying the change."),
    "InProgress": _r("A", PROGRESS, "Applying the change."),
    "ClusterBuildCreated": _r("A", PROGRESS, "The cluster build has started."),
    "ClusterBuildProvisioned": _r("A", PROGRESS, "The cluster is being installed."),
    "Deleting": _r("A", PROGRESS, "Being deleted."),
    "DeletionRequested": _r("A", PROGRESS, "Being deleted."),
    "Deleted": _r("A", PROGRESS, "Deleted; being removed."),
    # ---- waiting -----------------------------------------------------------------------
    "Waiting": _r("A", WAITING, "Waiting for a prerequisite."),
    "WaitingForDependencies": _r("A", WAITING, "Waiting for the objects this one depends on."),
    "WaitingForDependents": _r("A", WAITING, "Waiting for the objects that depend on this one to be deleted."),
    "WaitingForSiteEdge": _r("A", WAITING, "Waiting for the site connection to be ready."),
    "WaitingForSecret": _r("A", WAITING, "Waiting for a required credential to be available."),
    "WaitingForGateway": _r("A", WAITING, "Waiting for the site connection to be ready."),
    "WaitingForFabric": _r("A", WAITING, "Waiting for the network fabric to be ready."),
    "WaitingForAddressSpace": _r("T", WAITING, "Waiting for free address space. The message says what is full; adding a prefix or freeing a block lets it continue."),
    "WaitingForAccessGroup": _r("T", WAITING, "Waiting for the named access groups to be ready."),
    "WaitingForCluster": _r("T", WAITING, "Waiting for a cluster: name the OpenShift cluster to deploy onto."),
    "LinkedNotReady": _r("A", WAITING, "Waiting for its team setup, app projects and OpenShift cluster to be ready."),
    "ReferenceDeleting": _r("T", WAITING, "An object this one refers to is being deleted."),
    "NoMembersYet": _r("T", WAITING, "Ready; sign-in will be verified once a granted access group has a member."),
    "CredentialsUnavailable": _r("P", WAITING, "The site credentials are not available yet. The platform team is looking at it."),
    "NoJobStarted": _r("P", BLOCKED, "Changes in this tenant are delayed. The platform team is alerted; nothing you created is lost."),
    # ---- failed: tenant can fix ----------------------------------------------------------
    "Failed": _r("P", FAILED, "Could not be completed; see the reason."),
    "NotReady": _r("A", PROGRESS, "Not ready yet."),
    "InvalidSpec": _r("T", FAILED, "The request is not valid; correct the fields named in the message."),
    "EntityNotAllowed": _r("T", FAILED, "This site is not available to this tenant."),
    "SiteNotAllowed": _r("T", FAILED, "This site is not available to this tenant."),
    "SiteTypeMismatch": _r("T", FAILED, "This kind of environment cannot be created on the chosen site."),
    "SiteNotVirtualizationCapable": _r("T", FAILED, "The chosen site does not offer virtual machines."),
    "SiteNotEvpnCapable": _r("T", FAILED, "The chosen site cannot join hybrid networks."),
    "TypeNotSupported": _r("T", FAILED, "This type is not supported here."),
    "RegionNotSupported": _r("T", FAILED, "This region is not supported."),
    "ZoneMismatch": _r("T", FAILED, "The zone does not match the site."),
    "FabricMismatch": _r("T", FAILED, "The network and the site belong to different fabrics."),
    "FabricAmbiguous": _r("P", FAILED, "More than one fabric could serve this request. The platform team must resolve it."),
    "NamespaceTaken": _r("T", FAILED, "The namespace name is already in use on the site. Choose another name."),
    "EnvironmentNameTaken": _r("T", FAILED, "This environment name is already in use. Choose another name."),
    "NameInUse": _r("T", FAILED, "This name is already used by an OpenStack environment of another tenant. Create it with another name."),
    "PrefixInvalid": _r("T", FAILED, "The address prefix is not valid."),
    "PrefixInUse": _r("T", FAILED, "The address prefix overlaps one already in use: a placement's subnet or another network of this tenant."),
    "PrefixReserved": _r("T", FAILED, "The address prefix overlaps address space the platform keeps for itself. Choose another prefix."),
    "FamilyUnavailable": _r("T", FAILED, "This address family or IPv6 option is not offered here. The message says whether the site or the network lacks it."),
    "IPv6ModeInUse": _r("T", FAILED, "The network's IPv6 mode can't change while it has placements; remove them or create a new network."),
    "AttachmentNotSupported": _r("T", FAILED, "This attachment is not supported on the chosen site."),
    "TransportNotSupported": _r("T", FAILED, "This transport is not supported."),
    "AsnConflict": _r("T", FAILED, "The AS number conflicts with one already in use."),
    "PeerAsnReserved": _r("T", FAILED, "This AS number is reserved by the platform. Configure your router with another AS number."),
    "NoAccount": _r("T", FAILED, "No cloud account is set up for this environment."),
    "NoProject": _r("T", FAILED, "No cloud project is set up for this environment."),
    "ConfigNotFound": _r("T", FAILED, "The named connection does not exist."),
    "AccessNotMapped": _r("T", FAILED, "No access group named here exists in this tenant; nobody can sign in yet."),
    "ProjectDomainNotFound": _r("T", FAILED, "The referenced project or domain does not exist."),
    "DnsProjectNotFound": _r("P", FAILED, "The DNS project does not exist. The platform team is looking at it."),
    "ExternalNetworkNotFound": _r("P", FAILED, "The site's external network was not found. The platform team is looking at it."),
    "PolicyRefused": _r("T", FAILED, "The request was refused by a platform policy."),
    "NotRealizable": _r("T", FAILED, "The request cannot be realized on the chosen site."),
    # ---- failed: platform acts -----------------------------------------------------------
    "OperatorFailed": _r("P", FAILED, "The request could not be queued. The platform team is looking at it."),
    "EDAFailed": _r("P", FAILED, "Could not be completed; see the reason."),
    "PublishFailed": _r("P", FAILED, "The request could not be queued. The platform team is looking at it."),
    "ProvisionFailed": _r("P", FAILED, "Creating or updating did not complete. The platform team is looking at it."),
    "TeardownFailed": _r("P", FAILED, "Deleting did not complete. The platform team is looking at it."),
    "CredentialsMissing": _r("P", FAILED, "The site credentials are missing. The platform team is looking at it."),
    "CredentialsInvalid": _r("P", FAILED, "The site credentials were refused. The platform team is looking at it."),
    "InvalidSecret": _r("P", FAILED, "A required credential is not valid. The platform team is looking at it."),
    "SiteUnreachable": _r("P", FAILED, "The site does not answer. The platform team is looking at it."),
    "ProbeFailed": _r("P", FAILED, "The site check failed. The platform team is looking at it."),
    "CnvNotAvailable": _r("P", FAILED, "The site does not offer virtual machines yet. The platform team is looking at it."),
    "RoutesMissing": _r("P", FAILED, "Network routes are missing. The platform team is looking at it."),
    "NamespaceNotOwned": _r("P", FAILED, "The tenant namespace already exists and was not created for this tenant. The platform team must remove or rename it."),
    "MappingFailed": _r("P", FAILED, "The sign-in mapping could not be set up. The platform team is looking at it."),
    "ClaimMissing": _r("P", FAILED, "Members of the granted access groups cannot sign in yet. The platform team is looking at it."),
    "MapperMissing": _r("P", FAILED, "Sign-in is not set up for this service yet. The platform team is looking at it."),
    "NoDefaultConfig": _r("P", FAILED, "No default connection is configured for this service. Ask the platform team."),
    # Warning Event on an Entity (never a condition): its operator-version annotation names an image that is not allowed
    "NamespaceOperatorImageRejected": _r("P", FAILED, "The requested operator version for this tenant is not allowed; the default version is used."),
    # ---- paused (an administrator holds the object; nothing changes until it is resumed) --
    "Paused": _r("P", PAUSED, "Paused by the platform team; nothing changes on this site until it is resumed."),
    "SitePaused": _r("A", PAUSED, "Held: its site is paused. It keeps its last state until the site is resumed."),
    # ---- blocked (deletion) ------------------------------------------------------------
    "ProtectedChildren": _r("T", BLOCKED, "Deletion waits: some objects have deletion protection turned on."),
    "UnknownKind": _r("P", BLOCKED, "Deletion waits: an object of an unknown kind is in the tenant. The platform team is looking at it."),
    # ---- tenancy (ControllerAvailable / DeliveryStalled) ---------------------------------
    "ControllerUnavailable": _r("P", BLOCKED, "Changes in this tenant are delayed. The platform team is alerted; nothing you created is lost."),
    "ControllerRestarting": _r("P", BLOCKED, "Changes in this tenant are delayed. The platform team is alerted; nothing you created is lost."),
    "ControllerOutOfMemory": _r("P", BLOCKED, "Changes in this tenant are delayed. The platform team is alerted; nothing you created is lost."),
}

# Reasons written on a part of an object's status (an egress family of a network, its translation to IPv4, the IPv6
# internet access of a placement), never as the reason of its Ready condition: they do not change the object's
# phase. Same entry shape.
PART_REASONS = {
    "NotOffered": _r("A", READY, "Not offered on this fabric yet: internet access for an address family, or translation to IPv4 for a network without IPv4."),
    "AddressChoice": _r("T", READY, "Not offered for tenant-provided IPv6 prefixes: IPv6 internet access, or translation to IPv4."),
    "Degraded": _r("A", READY, "IPv6 internet access works with one upstream down; some connections may reset when traffic moves to the other path."),
    # the HybridFabric's IPv6EgressUpstream condition and its Warning event: a node without an upstream session
    "IPv6EgressUpstreamDown": _r("P", READY, "IPv6 internet access works through the other node; a node's upstream sessions are down."),
    # the HybridFabric's IPv6Translation condition and its Warning event: the platform's translation canary on a site
    # fails one of its checks, has not run for a while or cannot be reached
    "CanaryFailed": _r("P", READY, "The platform's translation canary on a site fails or is stale; the platform team is looking at it."),
    "IPv6TranslationCanaryFailed": _r("P", READY, "Translation to IPv4 may not work on a site: the platform's canary there has failed for several minutes; the platform team is alerted."),
}

# Deletion requests written by the operator are named <Kind>DeleteRequested.
DELETE_REQUESTED_SUFFIX = "DeleteRequested"

# Fallback summary per phase (used when the deciding reason is unknown or does not fit the phase).
PHASE_SUMMARIES = {
    "Waiting": "Waiting for a prerequisite.",
    "Creating": "Being created.",
    "Updating": "Applying the latest change.",
    "Ready": "Ready.",
    "Degraded": "Working with reduced health.",
    "Failed": "Could not be completed; see the reason.",
    "Deleting": "Being deleted.",
    "Paused": "Paused; nothing changes until it is resumed.",
}


def lookup(reason):
    """Registry entry for a reason, or None."""
    if not reason:
        return None
    entry = REASONS.get(reason)
    if entry is None and reason.endswith(DELETE_REQUESTED_SUFFIX):
        entry = REASONS["DeletionRequested"]
    return entry


def reason_class(reason):
    entry = lookup(reason)
    return entry["class"] if entry else None
