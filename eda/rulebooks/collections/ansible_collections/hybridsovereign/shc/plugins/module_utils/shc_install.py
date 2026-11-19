# -*- coding: utf-8 -*-
# The installation of an OpenShift cluster (PlatformOpenshift) as the platform observes it on the hub: the stage,
# the attempt, the last failed attempt and its cause, and whether the job that watches it must end; whether a stopped
# build may be removed before a new installation; which spec fields the installer reads once.
#
# Pure Python (standard library only). Every decision here is a function of what was read; the module install_observe
# and the role's tasks only read, call and write. Nothing read from the installer's log or from Hive's messages is
# ever copied into a result: the messages are fixed sentences of the platform with values that match a strict
# pattern (numbers, a resource word from a fixed list, a flavor, instance type or image name the tenant typed).
from __future__ import absolute_import, division, print_function

__metaclass__ = type

import datetime
import hashlib
import json
import re

STAGES = ("Preparing", "CleaningUp", "Installing", "Finishing", "Installed", "Stopped")
# stages in which the hub may hold a running or finished installation
LIVE_STAGES = ("CleaningUp", "Installing", "Finishing", "Installed")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# The whole stored log is scanned (a warning near its start can decide the cause). The bound is above the size an
# object can have on the API server, so a stored log always fits; text beyond it (never seen) is scanned as its first
# and last halves.
LOG_SCAN_LIMIT = 4 * 1024 * 1024
# a value the tenant typed (flavor, instance or machine type, image) is shown only when it has this form (whole value)
TENANT_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")
HIVE_CODE = re.compile(r"[A-Z][A-Za-z0-9]{2,63}")

# causes -> condition reasons (the catalogue's)
QUOTA, SETTING, CREDENTIALS, NOT_STARTED, OTHER = "quota", "setting", "credentials", "not-started", "other"
CAUSE_REASONS = {QUOTA: "QuotaExceeded", SETTING: "InstallSettingRefused", CREDENTIALS: "InstallCredentialsRefused",
                 NOT_STARTED: "InstallNotStarted", OTHER: "InstallFailed"}
OVERDUE_REASON = "InstallOverdue"
# what a tenant can do about an installation that could not start, once it stopped (the message knows the state; the
# reason's summary is true while the platform still tries as well)
NOT_STARTED_ADVICE = " Retry it once the platform team has fixed the cause, or delete the cluster."

# ---- install-time fields ------------------------------------------------------------------------------------------
# Each field of a provider block is one of three kinds; a test checks every field of the CRD's blocks against these
# lists, the CRD's rules and the pages' list.
# Install-time: the installer reads it once (install-config and the build's values) and the platform does not apply it
# to an installed cluster. The CRD refuses a change while a cluster is installing or installed.
INSTALL_TIME_FIELDS = {
    "openstack": ("controlPlaneCount", "workerCount", "controlPlaneFlavor", "workerFlavor", "ocpImageSet",
                  "glanceImageOverride", "trustBundle", "pullSecretVaultPath", "pullSecretVaultProperty",
                  "sshKeyVaultPath"),
    "aws": ("publish", "clusterType", "controlPlaneCount", "controllerFlavor", "workerFlavor"),
    "gcp": ("publish", "controlPlaneCount", "controllerFlavor", "workerFlavor"),
}
# Applied to an installed cluster by the platform (the worker pool's size on AWS and Google Cloud: every run renders the
# pool and the hub scales it): refused only while the cluster is being installed.
INSTALLED_CHANGEABLE_FIELDS = {"openstack": (), "aws": ("workerCount",), "gcp": ("workerCount",)}
# Fixed from creation (their own rules in the CRD): a change needs a new cluster.
IMMUTABLE_FIELDS = {"openstack": ("environment", "network", "externalNetwork"),
                    "aws": ("environment", "network", "region"),
                    "gcp": ("environment", "network", "region")}
# the spec fields outside the provider blocks that are fixed from creation
IMMUTABLE_TOP_FIELDS = ("type",)
# the part of the spec the installer reads: its digest at a stop tells a later change of it from any other change
INSTALLER_SPEC_KEYS = ("type", "openstack", "aws", "gcp")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha(value):
    return hashlib.sha256(_canonical(value).encode("ascii")).hexdigest()


# the fields whose values the digest covers, per provider (install-time, and applied-to-installed: both are what an
# installation is made with); a schema default added to another field, or a field added to a block, changes nothing
DIGEST_FIELDS = {p: sorted(set(INSTALL_TIME_FIELDS[p]) | set(INSTALLED_CHANGEABLE_FIELDS[p]))
                 for p in INSTALL_TIME_FIELDS}
# The version of that list together with the API's defaults of its fields (a default added later to a listed field
# changes what a stored digest means): the first 16 hex digits of the sha256 of {provider: {field: default or null}}.
# A digest recorded under another version (an older or newer product) is not compared. A test derives it from the
# CRD base: change it (and the operator's copy) when the list or a default of a listed field changes.
DIGEST_VERSION = "de042de6a02d21db"


def spec_digest(spec):
    """sha256 of the installer's settings in a spec: spec.type and the values of DIGEST_FIELDS of its provider (a field
    not set is null), as canonical JSON (sorted keys, no spaces, ASCII). The operator computes the same digest in Jinja
    (to_json with the same options | hash) from the same list."""
    spec = spec if isinstance(spec, dict) else {}
    ptype = spec.get("type")
    block = spec.get(ptype) if isinstance(ptype, str) and isinstance(spec.get(ptype), dict) else {}
    fields = DIGEST_FIELDS.get(ptype, []) if isinstance(ptype, str) else []
    return _sha({"type": ptype, "fields": {f: block.get(f) for f in fields}})


# ---- time -----------------------------------------------------------------------------------------------------------
def parse_time(value):
    """An RFC 3339 time as an aware datetime, or None."""
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:\d{2})$", text)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(1) + m.group(3).replace(":", ""), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


def fmt_time(dt):
    return dt.astimezone(datetime.timezone.utc).strftime(TIME_FORMAT)


def _minutes_since(then, now):
    t = parse_time(then)
    if t is None:
        return None
    return (now - t).total_seconds() / 60.0


# ---- reading Hive's objects ---------------------------------------------------------------------------------------
def _cond(obj, ctype):
    for c in (((obj or {}).get("status") or {}).get("conditions") or []):
        if isinstance(c, dict) and c.get("type") == ctype:
            return c
    return None


def _true(obj, ctype):
    c = _cond(obj, ctype)
    return c is not None and str(c.get("status", "")).lower() == "true"


def _bool(value):
    """A setting's truth as a job passes it (a bool, or the text a template made of one)."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "1")


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def hub_installed(cd):
    """True when the hub's build is installed (spec.installed, or Provisioned True)."""
    if not isinstance(cd, dict):
        return False
    return bool((cd.get("spec") or {}).get("installed") is True or _true(cd, "Provisioned"))


def hub_stopped(cd):
    return isinstance(cd, dict) and _true(cd, "ProvisionStopped")


# ---- the cause of a failed attempt ---------------------------------------------------------------------------------
OPENSTACK_QUOTA_WORDS = {
    "Cores": "Cores", "RAM": "RAM (MB)", "Instances": "Instances", "ServerGroups": "Server groups",
    "ServerGroupMembers": "Server group members", "Port": "Ports", "Router": "Routers", "Network": "Networks",
    "Subnet": "Subnets", "SecurityGroup": "Security groups", "SecurityGroupRule": "Security group rules",
}
AWS_QUOTA_WORDS = {
    "ec2/L-1216C47A": "Standard instance vCPUs", "ec2/L-DB2E81BA": "G and VT instance vCPUs",
    "ec2/L-7295265B": "X instance vCPUs", "ec2/L-0263D0A3": "Elastic IP addresses",
    "vpc/L-E79EC296": "Security groups", "vpc/L-F678F1CE": "VPCs", "vpc/L-A4707A72": "Internet gateways",
    "vpc/L-FE5A380F": "NAT gateways",
}
GCP_QUOTA_WORDS = {
    "compute.googleapis.com/cpus": "CPUs", "compute.googleapis.com/networks": "Networks",
    "compute.googleapis.com/subnetworks": "Subnetworks", "compute.googleapis.com/routers": "Routers",
    "compute.googleapis.com/firewalls": "Firewall rules", "compute.googleapis.com/health_checks": "Health checks",
    "compute.googleapis.com/forwarding_rules": "Forwarding rules",
    "compute.googleapis.com/target_pools": "Target pools",
    "compute.googleapis.com/regional_static_addresses": "Static addresses",
    "compute.googleapis.com/backend_services": "Backend services",
    "compute.googleapis.com/ssd_total_storage": "SSD storage (GB)",
    "compute.googleapis.com/disks_total_storage": "Disk storage (GB)",
    "iam.googleapis.com/quota/service-account-count": "Service accounts",
}
QUOTA_WORDS = dict(OPENSTACK_QUOTA_WORDS, **AWS_QUOTA_WORDS)
QUOTA_WORDS.update(GCP_QUOTA_WORDS)
# CPUs of one machine family on Google Cloud: compute.googleapis.com/<family>_cpus
GCP_FAMILY_CPUS = re.compile(r"^compute\.googleapis\.com/([a-z][a-z0-9]{0,5})_cpus$")

# the installer's quota sentences (pkg/asset/quota: "<name> is not available[ in <region>] because <report>")
QUOTA_SENTENCE = re.compile(
    r"(?<![A-Za-z0-9_./-])([A-Za-z][A-Za-z0-9_./-]{0,63}) is not available(?: in [a-z0-9-]{1,40})? because the "
    r"required number of resources \((\d{1,12})\) is more than (the limit|remaining quota) of (-?\d{1,12})")

# Hive's own reason codes (its install-log table) the platform knows, -> (cause, fixed words for the sentence)
HIVE_CODES = {
    "AWSEC2QuotaExceeded": (QUOTA, "EC2 instances"),
    "VcpuLimitExceeded": (QUOTA, "vCPUs"),
    "AWSNATGatewayLimitExceeded": (QUOTA, "NAT gateways"),
    "AWSVPCLimitExceeded": (QUOTA, "VPCs"),
    "S3BucketsLimitExceeded": (QUOTA, "S3 buckets"),
    "LoadBalancerLimitExceeded": (QUOTA, "Load balancers"),
    "EIPAddressLimitExceeded": (QUOTA, "Elastic IP addresses"),
    "Gp3VolumeLimitExceeded": (QUOTA, "gp3 storage"),
    "TooManyRoute53Zones": (QUOTA, "Hosted zones"),
    "GCPComputeQuotaExceeded": (QUOTA, "CPUs"),
    "GCPServiceAccountQuotaExceeded": (QUOTA, "Service accounts"),
    "GCPQuotaSSDTotalGBExceeded": (QUOTA, "SSD storage (GB)"),
    "FallbackQuotaExceeded": (QUOTA, ""),
    "FallbackResourceLimitExceeded": (QUOTA, ""),
    "InvalidCredentials": (CREDENTIALS, ""),
    "AWSInsufficientPermissions": (CREDENTIALS, ""),
    "AWSDeniedBySCP": (CREDENTIALS, ""),
    "AWSAccessDeniedSLR": (CREDENTIALS, ""),
    "InstallConfigNetworkAuthFail": (CREDENTIALS, ""),
    "GCPInstanceTypeNotFound": (SETTING, "instance-type"),
}
# Hive's codes that say nothing about the cause
HIVE_UNSPECIFIC = ("UnknownError", "")
# Hive's codes for other causes that may be shown by name (its install-log table and its own attempt limits); a code
# outside this list is not shown
HIVE_SHOWN = frozenset("""
AttemptDeadlineExceeded AuthenticationOperatorDegraded AWSAccountIsBlocked AWSAPIRateLimitExceeded
AWSIAMRoleTagLimitExceeded AWSInsufficientCapacity AWSRoute53Timeout AWSSubnetDoesNotExist
AWSSubnetInsufficientIPSpace AWSSubnetTagLimitExceeded AWSUnableToFindMatchingRouteTable AWSVPCDoesNotExist
BootstrapFailed DefaultEbsKmsKeyInsufficientPermissions DNSAlreadyExists ErrorCreatingNetworkLoadBalancer
ErrorDeletingIAMRole FallbackInstancesFailedToBecomeReady FallbackInvalidInstallConfig GCPInvalidProjectID
GCPPreconditionFailed GeneralClusterOperatorsStillUpdating GeneralOperatorDegraded GenericBootstrapFailed
InconsistentTerraformResult IngressOperatorDegraded InstallConfigNetworkBadCACert InstallerFailedToDestroyResources
InvalidAWSTags InvalidInstallConfigSubnet KubeAPIWaitFailed KubeAPIWaitTimeout MissingPublicSubnetForZone
MonitoringOperatorStillUpdating MultipleRoute53ZonesFound NATGatewayFailed NoMatchingRoute53Zone NoWorkerNodesReady
PendingVerification PrivateSubnetInMultipleZones ProxyInvalidCABundle ProxyTimeout S3AccessControlListNotSupported
TargetGroupNotFound UserInitiatedShutdown
""".split())

# a machine pool's type refused by the installer's validation (k8s field errors in the log)
SETTING_FIELD = re.compile(
    r"(controlPlane|compute\[\d{1,2}\])\.platform\.(openstack|aws|gcp)\.type: (Not found|Invalid value): "
    r"\\?\"([^\"\\]{1,100})\\?\"(?::\s*(Flavor did not meet|instance type \S{1,100} not found))?")
# the installer could not find a flavor while it gathered the site's details (a warning; it then skips its own checks
# and fails much later): a refused setting only when the flavor is one the cluster asked for
FLAVOR_NOT_FOUND = re.compile(r"Unable to find flavor with name ([A-Za-z0-9][A-Za-z0-9._:-]{0,99})(?![A-Za-z0-9._:-])")
IMAGE_NOT_FOUND = re.compile(r"Unable to find image with name ([A-Za-z0-9][A-Za-z0-9._:-]{0,99})(?![A-Za-z0-9._:-])")
# the installer's own sentence when the cluster's temporary control plane did not come up in its time
BOOTSTRAP_FAILED = re.compile(r"level=error msg=\"?Bootstrap failed to complete")
IMAGE_FIELD = re.compile(r"platform\.openstack\.clusterOSImage: (Not found|Invalid value): \\?\"([^\"\\]{1,200})\\?\"")
CREDENTIAL_TEXT = re.compile(
    r"(Authentication failed|The request you have made requires authentication|InvalidClientTokenId|"
    r"AuthFailure: AWS was not able to validate the provided access credentials|"
    r"oauth2: cannot fetch token|invalid_grant|failed to create a network client: Authentication failed)")

API_UNREACHABLE_CODES = ("KubeAPIWaitTimeout", "KubeAPIWaitFailed")
API_UNREACHABLE_TEXT = re.compile(r"waiting for Kubernetes API: context deadline exceeded|Failed waiting for Kubernetes API")

POOL_WORDS = {"openstack": "flavor", "aws": "instance type", "gcp": "machine type"}


def _pool_label(pool, provider):
    who = "control plane" if pool == "controlPlane" else "worker"
    return "%s %s" % (who, POOL_WORDS.get(provider, "flavor"))


def _tenant_value(value, tenant_values, key):
    want = str((tenant_values or {}).get(key) or "")
    if value and want and value == want and TENANT_VALUE.fullmatch(value):
        return value
    return ""


def _quota_word(name):
    if name in QUOTA_WORDS:
        return QUOTA_WORDS[name]
    m = GCP_FAMILY_CPUS.match(name)
    if m:
        return "%s CPUs" % m.group(1).upper()
    return ""


def parse_quota(log):
    """The quota shortfalls the installer wrote, as [(word, needed, limit_or_left, kind)] with kind 'limit' or
    'left'; only resources of the fixed lists (an unknown name gives the word '')."""
    found = []
    seen = set()
    for m in QUOTA_SENTENCE.finditer(log):
        word = _quota_word(m.group(1))
        key = (word, m.group(2), m.group(4), m.group(3))
        if key in seen:
            continue
        seen.add(key)
        found.append((word, int(m.group(2)), int(m.group(4)), "limit" if m.group(3) == "the limit" else "left"))
    return found


def _quota_sentence(items):
    parts = []
    unnamed = False
    for word, needed, have, kind in items:
        if not word:
            unnamed = True
            continue
        if kind == "limit":
            parts.append("%s (%d needed, limit %d)" % (word, needed, have))
        else:
            parts.append("%s (%d needed, %d left)" % (word, needed, have))
    if parts:
        text = "the site reports too little quota: " + ", ".join(parts[:6])
        if unnamed or len(parts) > 6:
            text += ", and more"
        return text
    return "the site reports too little quota"


def _bounded(text):
    """The text to scan: all of it up to LOG_SCAN_LIMIT characters, else its first and last halves."""
    if not isinstance(text, str):
        return ""
    if len(text) <= LOG_SCAN_LIMIT:
        return text
    half = LOG_SCAN_LIMIT // 2
    return text[:half] + "\n" + text[-half:]


def classify(provider, hive_reason, install_log, tenant_values=None, pod_not_started=False, log_read=True,
             network_path=False, second=""):
    """The cause of one failed attempt: {"cause", "reason", "text"}. text is a fixed sentence of the platform (no
    final full stop) with values that match a strict pattern only.

    provider       openstack | aws | gcp
    hive_reason    the ClusterProvisionFailed / ProvisionFailed reason Hive gave (its code), or ''
    install_log    the provision's spec.installLog (untrusted text), or None / ''
    tenant_values  {controlPlane, compute, image}: the values the tenant set (shown only when the log names exactly
                   one of them)
    pod_not_started  Hive says the install pod could not be started (InstallLaunchError, InstallPodStuck)
    log_read       False when the attempt's log was not available to read (an empty log then says nothing)
    network_path   the cluster is on a hybrid network: an installer that could not reach the cluster's API is named
                   with what the platform needs on a network
    second         Hive's condition message for the attempt (untrusted: on some Hive versions the whole log), read
                   with the same patterns after the log
    """
    code = str(hive_reason or "")
    if not HIVE_CODE.fullmatch(code):
        code = ""
    own = _bounded(install_log)
    extra = _bounded(second)
    log = own + ("\n" + extra if extra else "")
    tv = tenant_values if isinstance(tenant_values, dict) else {}

    def out(cause, text):
        return {"cause": cause, "reason": CAUSE_REASONS[cause], "text": text}

    # 1. quota: the installer's own sentences (with numbers), else Hive's quota code (words only)
    items = parse_quota(log) if log else []
    if items:
        return out(QUOTA, _quota_sentence(items))
    if code in HIVE_CODES and HIVE_CODES[code][0] == QUOTA:
        word = HIVE_CODES[code][1]
        return out(QUOTA, "the site reports too little quota" + (": %s" % word if word else ""))
    # 2. a refused setting of the tenant's
    if log:
        m = SETTING_FIELD.search(log)
        if m:
            pool, prov, kind, value, detail = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5) or ""
            pool = "controlPlane" if pool == "controlPlane" else "compute"
            label = _pool_label(pool, prov if prov in POOL_WORDS else provider)
            shown = _tenant_value(value, tv, pool)
            if kind == "Not found" or detail.startswith("instance type"):
                why = "it does not exist"
            elif detail.startswith("Flavor did not meet"):
                why = "it is smaller than the installer requires"
            else:
                why = "the installer cannot use it"
            if shown:
                return out(SETTING, "the site refused the %s \"%s\": %s" % (label, shown, why))
            return out(SETTING, "the site refused the %s: %s" % (label, why))
        for m in FLAVOR_NOT_FOUND.finditer(log):
            name = m.group(1)
            pools = [pool for pool in ("controlPlane", "compute") if _tenant_value(name, tv, pool)]
            if pools:
                label = _pool_label(pools[0], provider) if len(pools) == 1 else "flavor of the nodes"
                return out(SETTING, "the site refused the %s \"%s\": it does not exist" % (label, name))
        for m in IMAGE_NOT_FOUND.finditer(log):
            if _tenant_value(m.group(1), tv, "image"):
                return out(SETTING, "the site refused the image \"%s\": it does not exist" % m.group(1))
        m = IMAGE_FIELD.search(log)
        if m:
            shown = _tenant_value(m.group(2), tv, "image")
            if shown:
                return out(SETTING, "the site refused the image \"%s\"" % shown)
            return out(SETTING, "the site refused the image of the nodes")
    if code in HIVE_CODES and HIVE_CODES[code][0] == SETTING:
        return out(SETTING, "the site refused the %s" % ("instance or machine type of the nodes"))
    # 3. refused credentials
    if (code in HIVE_CODES and HIVE_CODES[code][0] == CREDENTIALS) or (log and CREDENTIAL_TEXT.search(log)):
        return out(CREDENTIALS, "the site refused the credentials the installer uses")
    # 4. could not start, or ended without a stored log (it may have run: the cause is unknown)
    if pod_not_started:
        return out(NOT_STARTED, "the installation could not be started on the platform")
    if log_read and not own.strip():
        return out(OTHER, "the attempt left no installer log to read")
    # 5. the installer could not reach the cluster's API (on a hybrid network most likely a network cause)
    if network_path and (code in API_UNREACHABLE_CODES or (log and API_UNREACHABLE_TEXT.search(log))):
        return out(OTHER, "the installer could not reach the cluster's API; a cluster on a network needs the "
                          "platform's connection to the network and a site that advertises floating IPs")
    # 6. the installer's bootstrap did not complete (its own sentence; nothing of the log is copied)
    if log and BOOTSTRAP_FAILED.search(log):
        return out(OTHER, "the installer's bootstrap of the cluster did not complete")
    # 7. anything else: Hive's specific code, never log text
    if code in HIVE_SHOWN:
        return out(OTHER, "the installer reported %s" % code)
    return out(OTHER, "the cause is not one the platform recognizes")


# ---- the observation ---------------------------------------------------------------------------------------------
def _attempt_text(attempt, limit):
    if limit:
        return "Attempt %d of %d" % (attempt, limit)
    return "Attempt %d" % attempt


def _failure_message(attempt, limit, text):
    return "%s failed: %s." % (_attempt_text(attempt, limit), text)


def _provision_failure(provision):
    """(hive_reason, failed) of a provision."""
    if not isinstance(provision, dict):
        return "", False
    stage = str((provision.get("spec") or {}).get("stage") or "")
    c = _cond(provision, "ClusterProvisionFailed")
    failed = stage == "failed" or (c is not None and str(c.get("status", "")).lower() == "true")
    return (str((c or {}).get("reason") or ""), failed)


def _attempt_of(provision):
    return _int(((provision or {}).get("spec") or {}).get("attempt"), -1)


def _latest(provisions):
    items = [p for p in provisions or [] if isinstance(p, dict)]
    return max(items, key=_attempt_of) if items else None


def _latest_failed(provisions, current):
    """The failed provision of the highest attempt among the list and the current one, or None."""
    items = [p for p in list(provisions or []) + [current] if isinstance(p, dict) and _provision_failure(p)[1]]
    return max(items, key=_attempt_of) if items else None


def _ready(reason, message):
    return {"type": "Ready", "status": "False", "reason": reason, "message": message}


def _stalled(status, reason="AsExpected", message=""):
    return {"type": "Stalled", "status": status, "reason": reason, "message": message}


def _same_conditions(current, wanted):
    index = {c.get("type"): c for c in current or [] if isinstance(c, dict)}
    for w in wanted:
        c = index.get(w["type"])
        if c is None:
            if w["type"] == "Stalled" and w["status"] == "False":
                continue
            return False
        for k in ("status", "reason", "message"):
            if str(c.get(k) or "") != str(w.get(k) or ""):
                return False
    return True


def _strip(install):
    return {k: v for k, v in (install or {}).items() if k != "observedAt"}


def _cond_time(obj, ctype):
    """The lastTransitionTime of a condition as RFC 3339 text, or None."""
    t = parse_time((_cond(obj, ctype) or {}).get("lastTransitionTime"))
    return fmt_time(t) if t is not None else None


def _being_removed(cd):
    return isinstance(cd, dict) and bool((cd.get("metadata") or {}).get("deletionTimestamp"))


def observe(obj, job, cd, provision, now, settings, obj_error=False, hub_error=False, provisions=None):
    """One observation of an installation. Returns a dict:

    end        the job must end now
    outcome    gone | replaced | deleting | superseded | installed | stopped | overdue | window | None (go on)
    write      a status write is due (status.install and conditions changed, or the check time is due)
    action     the status write's action (reconciling while it runs, failed on a stop or past the deadline,
               installing for "still installing")
    install    status.install as it should stand
    conditions the conditions to write (Ready, and Stalled when it changes)
    status     the legacy status word to write with them
    log        one line for the job's own log (never installer or hub text)

    obj        the cluster object as read now (None: it does not exist)
    job        {uid, eventId, startedAt}: the job's object identity, its event and when the job started
    cd         the hub's ClusterDeployment (None: none exists yet)
    provision  the ClusterProvision named by its status.provisionRef (None: none, or Hive cleared the reference)
    provisions every ClusterProvision of the build (Hive keeps the last ones); the latest is used when provision is
               None, and the latest failed one gives the last failure
    now        aware datetime
    settings   {windowMinutes, deadlineMinutes, writeEveryMinutes, provider, tenantValues, networkPath,
               releaseFailed (the job's release of the build failed and no ClusterDeployment exists)}
    obj_error  the object could not be read (unknown: go on, write nothing)
    hub_error  the hub could not be read (unknown: go on, write nothing; never a stop)

    Past the deadline an installation that neither finished nor stopped is shown failed (InstallOverdue) without
    Stalled: the platform keeps checking it at its periodic runs, one observation each; it becomes ready when it
    finishes and stopped (with its cause) when the installer stops.
    """
    job = job or {}
    settings = settings or {}
    res = {"end": False, "outcome": None, "write": False, "action": "reconciling", "install": None,
           "conditions": [], "status": "reconciling", "log": ""}
    window = float(settings["windowMinutes"])
    deadline = float(settings["deadlineMinutes"])
    write_every = float(settings["writeEveryMinutes"])
    provider = str(settings.get("provider") or "")
    net = _bool(settings.get("networkPath"))
    release_failed = _bool(settings.get("releaseFailed"))
    job_minutes = _minutes_since(job.get("startedAt"), now)
    window_reached = job_minutes is not None and job_minutes >= window

    # -- is this still the job's object and the job's request
    if obj_error:
        res["log"] = "the cluster object could not be read; checking again"
        if window_reached:
            res.update(end=True, outcome="window", log="the watch window ended while the object could not be read")
        return res
    if obj is None:
        res.update(end=True, outcome="gone", log="the cluster object no longer exists")
        return res
    meta = obj.get("metadata") or {}
    if job.get("uid") and meta.get("uid") and meta.get("uid") != job.get("uid"):
        res.update(end=True, outcome="replaced", log="the cluster object is another object now (new uid)")
        return res
    if meta.get("deletionTimestamp"):
        res.update(end=True, outcome="deleting", log="the cluster object is being deleted")
        return res
    status = obj.get("status") or {}
    current_event = ((status.get("delivery") or {}).get("eventId") or "")
    if job.get("eventId") and current_event and current_event != job.get("eventId"):
        res.update(end=True, outcome="superseded", log="a newer request for the cluster replaced this job's")
        return res

    cur = dict(status.get("install") or {})
    install = dict(cur)
    install.setdefault("run", 1)
    if hub_error or _being_removed(cd):
        # unknown, or a build that is being removed (a removal for a new installation, or the delete): no conclusion
        res["log"] = ("the hub could not be read; checking again" if hub_error
                      else "the hub's build is being removed; checking again")
        if window_reached:
            res.update(end=True, outcome="window", log="the watch window ended with no conclusion: %s" % res["log"])
        return res

    nowt = fmt_time(now)
    install["observedAt"] = nowt
    conditions = []
    stalled_now = str((_find(status, "Stalled") or {}).get("status", "")) == "True"

    if cd is None and release_failed:
        # the release of the build failed and Hive has no build: the installation could not start; it waits for a
        # retry like any stop
        lf = {"attempt": None, "reason": CAUSE_REASONS[NOT_STARTED],
              "message": "The installation could not be started: the platform's release of the build on the hub "
                         "failed.", "time": nowt}
        install.update(stage="Stopped", lastFailure=lf)
        install.setdefault("startedAt", nowt)
        install["endedAt"] = cur.get("endedAt") or nowt
        message = lf["message"] + NOT_STARTED_ADVICE
        _record_stop(install, obj, status, job)
        conditions = [_ready(lf["reason"], message), _stalled("True", lf["reason"], message)]
        res.update(end=True, outcome="stopped", write=True, action="failed", status="failed",
                   log="the build's release failed and the hub has no build: %s" % lf["reason"])
    elif cd is None:
        # the release is made but Hive has no build yet: nothing to conclude
        install["stage"] = "Installing"
        install.setdefault("startedAt", nowt)
        conditions.append(_ready("Installing", "The installation is starting."))
        res["log"] = "no build on the hub yet"
    else:
        if provision is None and provisions:
            provision = _latest(provisions)
        limit = _int((cd.get("spec") or {}).get("installAttemptsLimit"))
        restarts = _int((cd.get("status") or {}).get("installRestarts"), 0) or 0
        prov_attempt = _int(((provision or {}).get("spec") or {}).get("attempt")) if isinstance(provision, dict) \
            else None
        attempt = (prov_attempt + 1) if prov_attempt is not None else restarts + 1
        if limit:
            install["attemptsLimit"] = limit
            attempt = min(attempt, max(limit, 1))
        install["attempt"] = attempt
        install.setdefault("startedAt", nowt)
        cd_failed = _cond(cd, "ProvisionFailed")
        not_started = _true(cd, "InstallLaunchError") or _true(provision, "InstallPodStuck") or \
            _true(cd, "InstallImagesNotResolved")
        cd_failed_true = cd_failed is not None and str(cd_failed.get("status", "")).lower() == "true"
        prev = cur.get("lastFailure") if isinstance(cur.get("lastFailure"), dict) else {}
        failed_prov = _latest_failed(provisions, provision)
        if failed_prov is not None:
            # the latest failed attempt: its own log says why (Hive's condition message is a second source for the
            # same patterns: on some Hive versions it is the whole log)
            failed_attempt = (_int((failed_prov.get("spec") or {}).get("attempt"), 0) or 0) + 1
            fcond = _cond(failed_prov, "ClusterProvisionFailed") or {}
            reason = str(fcond.get("reason") or (cd_failed or {}).get("reason") or "")
            second = str(fcond.get("message") or (cd_failed or {}).get("message") or "")
            if _int(prev.get("attempt")) == failed_attempt and prev.get("message"):
                install["lastFailure"] = prev
            else:
                cause = classify(provider, reason, (failed_prov.get("spec") or {}).get("installLog"),
                                 settings.get("tenantValues"), pod_not_started=not_started, network_path=net,
                                 second=second)
                when = _cond_time(failed_prov, "ClusterProvisionFailed") or _cond_time(cd, "ProvisionFailed") or nowt
                install["lastFailure"] = {"attempt": failed_attempt, "reason": cause["reason"],
                                          "message": _failure_message(failed_attempt, limit, cause["text"]),
                                          "time": when}
        elif cd_failed_true and not prev:
            # an earlier attempt failed and no provision of it is left: Hive's code and its condition message
            failed_attempt = max(attempt - 1, 1) if provision is not None else max(restarts, 1)
            cause = classify(provider, str(cd_failed.get("reason") or ""), None, settings.get("tenantValues"),
                             log_read=False, network_path=net, second=str(cd_failed.get("message") or ""))
            install["lastFailure"] = {"attempt": failed_attempt, "reason": cause["reason"],
                                      "message": _failure_message(failed_attempt, limit, cause["text"]),
                                      "time": _cond_time(cd, "ProvisionFailed") or nowt}
        if hub_installed(cd):
            install["stage"] = "Finishing"
            # the merge patch removes the key (an omitted key would keep the stored failure)
            install["lastFailure"] = None
            if not cur.get("endedAt"):
                install["endedAt"] = _cond_time(cd, "Provisioned") or nowt
            conditions.append(_ready("InstallFinishing", "The cluster is installed; the platform is connecting it."))
            if stalled_now:
                conditions.append(_stalled("False"))
            res.update(end=True, outcome="installed", write=True, log="the hub's build is installed")
        elif hub_stopped(cd) and cur.get("stage") == "Stopped":
            # the stop is already recorded: nothing of it is recorded again (its handled request stays)
            res.update(end=True, outcome="stopped", log="the hub's build stopped; the stop is already recorded")
            res["install"] = cur
            res["conditions"] = []
            return res
        elif hub_stopped(cd):
            install["stage"] = "Stopped"
            install["endedAt"] = cur.get("endedAt") or _cond_time(cd, "ProvisionStopped") or nowt
            lf = install.get("lastFailure")
            if not isinstance(lf, dict) or not lf.get("message"):
                cause = classify(provider, str((cd_failed or {}).get("reason") or ""), None,
                                 settings.get("tenantValues"), pod_not_started=not_started, log_read=False,
                                 network_path=net)
                lf = {"attempt": attempt, "reason": cause["reason"],
                      "message": _failure_message(attempt, limit, cause["text"]), "time": install["endedAt"]}
                install["lastFailure"] = lf
            stop_reason = str((_cond(cd, "ProvisionStopped") or {}).get("reason") or "")
            if stop_reason == "InstallAttemptsLimitReached":
                message = lf["message"] + (" The installer stopped after %d attempts." % attempt if attempt > 1
                                           else " The installer stopped after 1 attempt.")
            else:
                message = lf["message"] + " The installer stopped."
            if lf.get("reason") == CAUSE_REASONS[NOT_STARTED]:
                message += NOT_STARTED_ADVICE
            _record_stop(install, obj, status, job)
            conditions.append(_ready(lf.get("reason") or CAUSE_REASONS[OTHER], message))
            conditions.append(_stalled("True", lf.get("reason") or CAUSE_REASONS[OTHER], message))
            res.update(end=True, outcome="stopped", write=True, action="failed", status="failed",
                       log="the hub's build stopped: %s" % lf.get("reason"))
        else:
            install["stage"] = "Installing"
            lf = install.get("lastFailure") if isinstance(install.get("lastFailure"), dict) else None
            if not_started and not lf:
                conditions.append(_ready("InstallNotStarted",
                                         "%s could not be started on the platform yet; the platform keeps checking."
                                         % _attempt_text(attempt, limit)))
            elif lf and limit and _int(lf.get("attempt"), 0) >= limit:
                # every attempt failed and Hive has not stopped yet: no new attempt comes
                conditions.append(_ready("InstallRetrying", "%s No attempts are left; the installation stops "
                                                            "shortly." % (lf.get("message") or "")))
            elif lf:
                conditions.append(_ready("InstallRetrying", "%s The installer is trying again."
                                         % (lf.get("message") or "")))
            else:
                conditions.append(_ready("Installing", "Installing, %s." % _attempt_text(attempt, limit).lower()))
            res["log"] = "installing, attempt %d" % attempt

    if not res["end"]:
        # an installation runs: a Stalled left by an earlier state never stands beside it
        if stalled_now:
            conditions.append(_stalled("False"))
        # the platform's own deadline for an installation that neither finished nor stopped: shown failed, checked on
        started = _minutes_since(install.get("startedAt"), now)
        if started is not None and started >= deadline:
            att = _int(install.get("attempt"))
            message = ("%s has not finished; the installation has run for more than %d minutes."
                       % (_attempt_text(att, _int(install.get("attemptsLimit"))), int(deadline)) if att
                       else "The installation has run for more than %d minutes." % int(deadline))
            conditions = [c for c in conditions if c["type"] != "Ready"] + [_ready(OVERDUE_REASON, message)]
            res.update(end=True, outcome="overdue", write=True, action="failed", status="failed",
                       log="the installation is past the deadline; the periodic run checks it again")
        elif window_reached:
            res.update(end=True, outcome="window", write=True, action="installing",
                       log="the watch window ended; the next run goes on watching")

    res["install"] = install
    res["conditions"] = conditions
    if not res["write"]:
        changed = _strip(install) != _strip(cur) or not _same_conditions(status.get("conditions"), conditions)
        last = _minutes_since(cur.get("observedAt"), now)
        res["write"] = changed or last is None or last >= write_every
    return res


def overdue(status, now, deadline_minutes):
    """True when the recorded installation runs (stage Installing) and started at least deadline_minutes ago: a run
    then makes one observation and ends (the platform keeps checking it at its periodic runs)."""
    install = (status or {}).get("install") or {}
    if install.get("stage") != "Installing" or not _int(deadline_minutes, 0):
        return False
    started = _minutes_since(install.get("startedAt"), now)
    return started is not None and started >= float(deadline_minutes)


def _record_stop(install, obj, status, job):
    """What a later run compares to know whether someone asked since the stop: the generation, the digest of the
    installer's settings, and the Retry request handled at the stop: the newest request present when the stop is
    first written (a request made before the tenant could see the stop asked about the installation, not for a new
    one). Only a request made after this write starts a new installation. A stop already recorded is never recorded
    again (see observe)."""
    meta = obj.get("metadata") or {}
    install["stoppedGeneration"] = _int(meta.get("generation"), 0)
    install["stoppedSpec"] = spec_digest(obj.get("spec"))
    install["stoppedSpecVersion"] = DIGEST_VERSION
    install["stoppedRequest"] = str(status.get("lastHandledReconcileRequest") or "")


def _find(status, ctype):
    for c in (status or {}).get("conditions") or []:
        if isinstance(c, dict) and c.get("type") == ctype:
            return c
    return None


def same_object(obj, uid, event_id, generation=None):
    """Whether a job may go on with its object: (ok, why). The checks during a removal and right before the job makes
    the release. generation: the generation the job read its spec at; the check before the release passes it, so a
    job never installs a spec other than the one it read (the newer request runs with the new spec)."""
    if obj is None:
        return False, "the cluster object no longer exists"
    meta = obj.get("metadata") or {}
    if uid and meta.get("uid") and meta.get("uid") != uid:
        return False, "the cluster object is another object now (new uid)"
    if meta.get("deletionTimestamp"):
        return False, "the cluster object is being deleted"
    cur = (((obj.get("status") or {}).get("delivery") or {}).get("eventId") or "")
    if event_id and cur and cur != event_id:
        return False, "a newer request for the cluster replaced this job's"
    want = _int(generation)
    if want is not None and want > 0 and _int(meta.get("generation"), 0) != want:
        return False, "the cluster's spec changed after this job read it; a new run installs with the new spec"
    return True, ""


# ---- status.install written by the role ------------------------------------------------------------------------
def install_preparing(status, now):
    """status.install for the first check of a cluster whose installation has not started, else None (no change)."""
    status = status or {}
    cur = status.get("install") or {}
    if cur:
        return None
    if status.get("installStarted") is True or status.get("clusterInstalled") is True:
        return None
    return {"stage": "Preparing", "run": 1}


def install_starting(status, now):
    """status.install right before the release is made: a new installation (Installing, startedAt now) when none runs,
    else None (no change). A stopped installation never gets here: the removal step ends the job or records
    CleaningUp (the next run) first."""
    status = status or {}
    cur = dict(status.get("install") or {})
    if cur.get("stage") in ("Installing", "Finishing", "Installed", "Stopped"):
        return None
    if not cur and (status.get("clusterInstalled") is True or status.get("installStarted") is True):
        # a cluster from before installations were recorded: its record is the watch's to write (from the hub)
        return None
    out = {"stage": "Installing", "run": _int(cur.get("run"), 1) or 1, "startedAt": fmt_time(now),
           "endedAt": None, "lastFailure": None, "attempt": None, "observedAt": fmt_time(now)}
    return out


def install_cleaning(status, now):
    """status.install when a stopped installation is removed for a new one: the next run."""
    cur = dict((status or {}).get("install") or {})
    # startedAt: the new run starts with the removal (its time bound counts from here; Installing sets it again)
    return {"stage": "CleaningUp", "run": (_int(cur.get("run"), 1) or 1) + 1, "startedAt": fmt_time(now), "endedAt": None,
            "attempt": None, "lastFailure": None, "stoppedGeneration": None, "stoppedRequest": None,
            "stoppedSpec": None, "stoppedSpecVersion": None, "observedAt": fmt_time(now)}


def install_installed(status, now, cd=None):
    """status.install once the cluster is installed and connected. endedAt: as the watch recorded it, else the hub's
    install time (ClusterDeployment status.installedTimestamp); with neither it is left out (never written empty, so
    an end already stored stays). A cluster installed before installations were recorded and with no install time on
    the hub has no known end."""
    cur = dict((status or {}).get("install") or {})
    out = {"stage": "Installed", "lastFailure": None, "stoppedGeneration": None, "stoppedRequest": None,
           "stoppedSpec": None, "stoppedSpecVersion": None, "observedAt": fmt_time(now)}
    hub_time = parse_time(((cd or {}).get("status") or {}).get("installedTimestamp")) if isinstance(cd, dict) else None
    ended = cur.get("endedAt") or (fmt_time(hub_time) if hub_time else None)
    if ended:
        out["endedAt"] = ended
    # run is left as it stands (a merge keeps it), so a status read before a removal in the same job is good enough
    return out


RUNNING_STAGES = ("Preparing", "CleaningUp", "Installing", "Finishing")


def install_check_time(status, now):
    """{observedAt: now} for a run that works on an installation recorded as running (the check time the operator's
    in-flight rule reads); None otherwise (an installed or stopped record, or none)."""
    stage = ((status or {}).get("install") or {}).get("stage")
    return {"observedAt": fmt_time(now)} if stage in RUNNING_STAGES else None


def install_stopped_seen(obj):
    """status.install of a stopped installation after a run that starts nothing: the generation is recorded as seen
    (the operator waits again), and the digest of the installer's settings is renewed under this code's version (a
    digest recorded by another version of the product is replaced, never compared)."""
    obj = obj or {}
    meta = obj.get("metadata") or {}
    return {"stoppedGeneration": _int(meta.get("generation"), 0), "stoppedSpec": spec_digest(obj.get("spec")),
            "stoppedSpecVersion": DIGEST_VERSION}


def install_resumed(status, stage="Installing"):
    """status.install when a run finds that the hub's build of a recorded stop runs or is installed: the hub wins. The
    stage is the hub's, the stop's bookkeeping and its last failure go (the running installation's own attempt and
    failures are the watch's to write), the run is not counted again."""
    return {"stage": stage, "endedAt": None, "attempt": None, "lastFailure": None, "stoppedGeneration": None,
            "stoppedRequest": None, "stoppedSpec": None, "stoppedSpecVersion": None}


HUB_STATES = ("unreadable", "none", "deleting", "installed", "stopped", "running")


def recorded_installed(status):
    """The object records an installed cluster: status.clusterInstalled true, or stage Finishing or Installed."""
    status = status or {}
    stage = (status.get("install") or {}).get("stage")
    return _bool(status.get("clusterInstalled")) or stage in ("Finishing", "Installed")


def build_record_missing(status, cd, hub_read_ok):
    """True when the object records an installed cluster and the hub, read without error, holds no
    ClusterDeployment of its build: no release is made and no installation started for it (a new installation under
    its name could take the addresses and names of a cluster that may still run); only its deletion leads to a new
    one."""
    return bool(hub_read_ok) and cd is None and recorded_installed(status)


def removal_overdue(status, now, limit_minutes):
    """True when a removal recorded as CleaningUp started limit_minutes ago or more (status.install.startedAt)."""
    install = (status or {}).get("install") or {}
    if install.get("stage") != "CleaningUp" or not _int(limit_minutes, 0):
        return False
    started = _minutes_since(install.get("startedAt"), now)
    return started is not None and started >= float(limit_minutes)


def release_young(release_status, last_deployed, now, pending_minutes):
    """True when the hub's release of a build without a ClusterDeployment is pending (an older job is still making it)
    and younger than pending_minutes: it is left alone (the installation is starting). An unreadable time is old."""
    if not str(release_status or "").startswith("pending"):
        return False
    started = _minutes_since(last_deployed, now)
    # a time that cannot be read counts as old: the release is repaired, never waited for without end
    return started is not None and started < float(pending_minutes)


def hub_state(cd, hub_read_ok):
    """What the hub holds of a build: unreadable, none (no ClusterDeployment), deleting, installed, stopped, running."""
    if not hub_read_ok:
        return "unreadable"
    if not isinstance(cd, dict):
        return "none"
    if _being_removed(cd):
        return "deleting"
    if hub_installed(cd):
        return "installed"
    if hub_stopped(cd):
        return "stopped"
    return "running"


# ---- the guarded removal of a stopped build ----------------------------------------------------------------------
def asked_since_stop(obj, event=None):
    """Whether someone asked for a new installation since the recorded stop: (asked, why). Counts:
    - a Retry request other than the one handled at the stop (status.lastHandledReconcileRequest vs
      status.install.stoppedRequest), or a Retry event published after the stop (event: {reason, publishedAt}; the
      object's status.delivery when not given);
    - a change of the installer's part of the spec (spec.type and the provider blocks: its digest vs
      status.install.stoppedSpec; a stop recorded without the digest compares the generation).
    A change of anything else (access groups, viewers, deletion protection) does not count."""
    status = (obj or {}).get("status") or {}
    install = status.get("install") or {}
    handled = str(status.get("lastHandledReconcileRequest") or "")
    if "stoppedRequest" in install and handled != str(install.get("stoppedRequest") or ""):
        return True, "a Retry request since the stop"
    ev = event or {}
    if not ev:
        delivery = status.get("delivery") or {}
        ev = {"reason": delivery.get("reason"), "publishedAt": delivery.get("publishedAt")}
    # a Retry event counts when it was published after the stop was written (observedAt of the stop's write; Hive's
    # stop time for a stop recorded without it)
    written = [t for t in (parse_time(install.get("endedAt")), parse_time(install.get("observedAt"))) if t]
    ended = max(written) if written else None
    published = parse_time(ev.get("publishedAt"))
    if str(ev.get("reason") or "").endswith("ReconcileRequested") and ended and published and published > ended:
        return True, "a Retry request since the stop"
    if install.get("stoppedSpec"):
        if str(install.get("stoppedSpecVersion") or "") != DIGEST_VERSION:
            # recorded by another version of the product: only a Retry asks (the next run renews the record)
            return False, "nobody asked for a new installation since the stop (no Retry; the recorded settings are " \
                          "of another version and are renewed)"
        if spec_digest((obj or {}).get("spec")) != str(install.get("stoppedSpec")):
            return True, "a change of the cluster's installation settings since the stop"
        return False, "nobody asked for a new installation since the stop (no Retry, no change of the installation " \
                      "settings)"
    generation = _int(((obj or {}).get("metadata") or {}).get("generation"), 0)
    stopped_gen = _int(install.get("stoppedGeneration"))
    if stopped_gen is not None and generation > stopped_gen:
        return True, "a change of the cluster's spec since the stop"
    return False, "nobody asked for a new installation since the stop"


def may_remove_build(obj, cd, hub_read_ok, release_failed=False, event=None, recheck=False, release_exists=None,
                     release_young=False):
    """Whether the build of a stopped installation may be removed before a new one: (allowed, why).

    All of these must hold, read fresh in the same run:
    1. the hub's build is not installed;
    2. the hub's build has stopped (ProvisionStopped True) or is already being removed, or no ClusterDeployment is
       left (its release failed or is gone; a release of the build without a ClusterDeployment that did not fail is
       kept: stage Stopped never has one);
    3. the object records no installed cluster (clusterInstalled not true, stage not Finishing or Installed);
    4. someone asked since the stop (asked_since_stop), or the object records the removal as under way (stage
       CleaningUp: an earlier job decided and recorded it, and this run continues it).
    The hub not readable, or the object not readable, means no removal (unknown is treated as installed).
    recheck: the fresh read right before the first delete (conditions 1 to 3 again, the stage Stopped or CleaningUp).
    release_exists: whether the hub holds a release of the build (None: not known, as when there is none).
    """
    if not hub_read_ok:
        return False, "the hub could not be read; nothing is removed (an unknown build counts as installed)"
    if not isinstance(obj, dict):
        return False, "the cluster object could not be read; nothing is removed"
    if isinstance(cd, dict) and hub_installed(cd):
        return False, "the hub's build is installed; it is never removed by a run"
    status = obj.get("status") or {}
    install = status.get("install") or {}
    stage = install.get("stage")
    if status.get("clusterInstalled") is True or str(status.get("clusterInstalled")).lower() == "true":
        return False, "the cluster object records an installed cluster; nothing is removed"
    if stage in ("Finishing", "Installed"):
        return False, "the cluster object records an installed cluster (stage %s); nothing is removed" % stage
    if stage not in ("Stopped", "CleaningUp"):
        return False, "the cluster object records no stopped installation; nothing is removed"
    if cd is None:
        # a release of the build without a ClusterDeployment (failed, deployed, or pending for longer than the
        # installation allows) is no live build; a younger pending one is an older job's release being made
        if _bool(release_young):
            return False, "the hub's release of the build is still being made; the installation is starting"
    elif _being_removed(cd):
        # Hive is deprovisioning it (an earlier job's delete): nothing more is removed now; the run after it is gone
        # continues the removal
        return False, "the hub's build is being removed; the next run continues the removal once it is gone"
    elif not hub_stopped(cd):
        return False, "the hub's build has not stopped (Hive may still be trying); nothing is removed"
    if stage == "CleaningUp":
        return True, "the removal of the stopped build recorded on the cluster goes on"
    if recheck:
        return True, "the stopped build is removed for a new installation"
    asked, why = asked_since_stop(obj, event)
    if not asked:
        return False, why + "; nothing is removed"
    return True, "the stopped build is removed for a new installation (%s)" % why
