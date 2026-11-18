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
import re

STAGES = ("Preparing", "CleaningUp", "Installing", "Finishing", "Installed", "Stopped")
# stages in which the hub may hold a running or finished installation
LIVE_STAGES = ("CleaningUp", "Installing", "Finishing", "Installed")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# the end of a log is where the installer writes why it failed; nothing beyond this many characters is scanned
LOG_SCAN_LIMIT = 65536
# a value the tenant typed (flavor, instance or machine type, image) is shown only when it has this form
TENANT_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
HIVE_CODE = re.compile(r"^[A-Z][A-Za-z0-9]{2,63}$")

# causes -> condition reasons (the catalogue's)
QUOTA, SETTING, CREDENTIALS, NOT_STARTED, OTHER = "quota", "setting", "credentials", "not-started", "other"
CAUSE_REASONS = {QUOTA: "QuotaExceeded", SETTING: "InstallSettingRefused", CREDENTIALS: "CredentialsInvalid",
                 NOT_STARTED: "InstallNotStarted", OTHER: "InstallFailed"}
OVERDUE_REASON = "InstallOverdue"

# ---- install-time fields ------------------------------------------------------------------------------------------
# Spec fields the installer reads once (install-config and the build's values) and the platform does not apply to an
# installed cluster, per provider block. The CRD's transition rule refuses a change of any of them while a cluster is
# installing or installed; a test keeps this list, the role's reads and the rule in step.
INSTALL_TIME_FIELDS = {
    "openstack": ("environment", "controlPlaneCount", "workerCount", "controlPlaneFlavor", "workerFlavor",
                  "ocpImageSet", "glanceImageOverride", "trustBundle", "pullSecretVaultPath",
                  "pullSecretVaultProperty", "sshKeyVaultPath"),
    "aws": ("environment", "region", "publish", "clusterType", "controlPlaneCount", "workerCount",
            "controllerFlavor", "workerFlavor"),
    "gcp": ("environment", "region", "publish", "controlPlaneCount", "workerCount", "controllerFlavor",
            "workerFlavor"),
}
# the spec fields outside the provider blocks that are install-time as well
INSTALL_TIME_TOP_FIELDS = ("type",)


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
    if value and want and value == want and TENANT_VALUE.match(value):
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
    if not HIVE_CODE.match(code):
        code = ""
    own = install_log if isinstance(install_log, str) else ""
    if len(own) > LOG_SCAN_LIMIT:
        own = own[-LOG_SCAN_LIMIT:]
    extra = second if isinstance(second, str) else ""
    if len(extra) > LOG_SCAN_LIMIT:
        extra = extra[-LOG_SCAN_LIMIT:]
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
    # 4. could not start, or ended without a log
    if pod_not_started:
        return out(NOT_STARTED, "the installation could not be started on the platform")
    if log_read and not own.strip():
        return out(NOT_STARTED, "the attempt ended before the installer wrote a log")
    # 5. the installer could not reach the cluster's API (on a hybrid network most likely a network cause)
    if network_path and (code in API_UNREACHABLE_CODES or (log and API_UNREACHABLE_TEXT.search(log))):
        return out(OTHER, "the installer could not reach the cluster's API; a cluster on a network needs the "
                          "platform's connection to the network and a site that advertises floating IPs")
    # 6. anything else: Hive's specific code, never log text
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


def observe(obj, job, cd, provision, now, settings, obj_error=False, hub_error=False, provisions=None):
    """One observation of an installation. Returns a dict:

    end        the job must end now
    outcome    gone | replaced | deleting | superseded | installed | stopped | overdue | window | None (go on)
    write      a status write is due (status.install and conditions changed, or the check time is due)
    action     the status write's action (reconciling while it runs, failed on a stop, installing for "still
               installing")
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
    settings   {windowMinutes, deadlineMinutes, writeEveryMinutes, provider, tenantValues}
    obj_error  the object could not be read (unknown: go on, write nothing)
    hub_error  the hub could not be read (unknown: go on, write nothing; never a stop)
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
    if hub_error:
        res["log"] = "the hub could not be read; checking again"
        if window_reached:
            res.update(end=True, outcome="window", log="the watch window ended while the hub could not be read")
        return res

    nowt = fmt_time(now)
    install["observedAt"] = nowt
    conditions = []
    generation = _int(meta.get("generation"), 0)

    if cd is None:
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
        prov_attempt = _int((provision or {}).get("spec", {}).get("attempt")) if isinstance(provision, dict) else None
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
        prev = cur.get("lastFailure") or {}
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
                install["lastFailure"] = {"attempt": failed_attempt, "reason": cause["reason"],
                                          "message": _failure_message(failed_attempt, limit, cause["text"]),
                                          "time": nowt}
        elif cd_failed_true and not prev:
            # an earlier attempt failed and no provision of it is left: Hive's code and its condition message
            failed_attempt = max(attempt - 1, 1) if provision is not None else max(restarts, 1)
            cause = classify(provider, str(cd_failed.get("reason") or ""), None, settings.get("tenantValues"),
                             log_read=False, network_path=net, second=str(cd_failed.get("message") or ""))
            install["lastFailure"] = {"attempt": failed_attempt, "reason": cause["reason"],
                                      "message": _failure_message(failed_attempt, limit, cause["text"]),
                                      "time": nowt}
        if hub_installed(cd):
            install["stage"] = "Finishing"
            install.pop("lastFailure", None)
            conditions.append(_ready("InstallFinishing", "The cluster is installed; the platform is connecting it."))
            if str((_find(status, "Stalled") or {}).get("status", "")) == "True":
                conditions.append(_stalled("False"))
            res.update(end=True, outcome="installed", write=True, log="the hub's build is installed")
        elif hub_stopped(cd):
            install["stage"] = "Stopped"
            install.setdefault("endedAt", nowt)
            lf = install.get("lastFailure")
            if not lf:
                cause = classify(provider, str((cd_failed or {}).get("reason") or ""), None,
                                 settings.get("tenantValues"), pod_not_started=not_started, log_read=False,
                                 network_path=net)
                lf = {"attempt": attempt, "reason": cause["reason"],
                      "message": _failure_message(attempt, limit, cause["text"]), "time": nowt}
                install["lastFailure"] = lf
            message = lf["message"] + (" The installer stopped after %d attempts." % limit if limit else
                                       " The installer stopped.")
            install["stoppedGeneration"] = generation
            install["stoppedRequest"] = str(status.get("lastHandledReconcileRequest") or "")
            conditions.append(_ready(lf["reason"], message))
            conditions.append(_stalled("True", lf["reason"], message))
            res.update(end=True, outcome="stopped", write=True, action="failed", status="failed",
                       log="the hub's build stopped: %s" % lf["reason"])
        else:
            install["stage"] = "Installing"
            lf = install.get("lastFailure")
            if not_started and not lf:
                conditions.append(_ready("InstallNotStarted",
                                         "%s could not be started on the platform yet; the platform keeps checking."
                                         % _attempt_text(attempt, limit)))
            elif lf and limit and _int(lf.get("attempt"), 0) >= limit:
                # every attempt failed and Hive has not stopped yet: no new attempt comes
                conditions.append(_ready("InstallRetrying", lf["message"] + " No attempts are left; the installation "
                                                                          "stops shortly."))
            elif lf:
                conditions.append(_ready("InstallRetrying", lf["message"] + " The installer is trying again."))
            else:
                conditions.append(_ready("Installing", "Installing, %s." % _attempt_text(attempt, limit).lower()))
            res["log"] = "installing, attempt %d" % attempt

    # -- the platform's own deadline for an installation that neither finished nor stopped
    if not res["end"]:
        started = _minutes_since(install.get("startedAt"), now)
        if started is not None and started >= deadline:
            message = "The installation did not finish within %d minutes." % int(deadline)
            install["stoppedGeneration"] = generation
            install["stoppedRequest"] = str(status.get("lastHandledReconcileRequest") or "")
            conditions = [_ready(OVERDUE_REASON, message), _stalled("True", OVERDUE_REASON, message)]
            res.update(end=True, outcome="overdue", write=True, action="failed", status="failed",
                       log="the installation passed the deadline")
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


def _find(status, ctype):
    for c in (status or {}).get("conditions") or []:
        if isinstance(c, dict) and c.get("type") == ctype:
            return c
    return None


def same_object(obj, uid, event_id):
    """Whether a job may go on with its object: (ok, why). The check right before the job installs the release."""
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
    """status.install right before the release is made: a new installation (Installing, startedAt now) unless one is
    already running or done (then None: no change)."""
    status = status or {}
    cur = dict(status.get("install") or {})
    if cur.get("stage") in ("Installing", "Finishing", "Installed", "Stopped"):
        # running, done, or stopped (a new installation starts only after the removal recorded CleaningUp)
        return None
    if not cur and status.get("clusterInstalled") is True:
        return None
    out = {"stage": "Installing", "run": _int(cur.get("run"), 1) or 1, "startedAt": fmt_time(now),
           "endedAt": None, "lastFailure": None, "attempt": None, "observedAt": fmt_time(now)}
    return out


def install_cleaning(status, now):
    """status.install when a stopped installation is removed for a new one: the next run."""
    cur = dict((status or {}).get("install") or {})
    return {"stage": "CleaningUp", "run": (_int(cur.get("run"), 1) or 1) + 1, "startedAt": None, "endedAt": None,
            "attempt": None, "lastFailure": None, "stoppedGeneration": None, "stoppedRequest": None,
            "observedAt": fmt_time(now)}


def install_installed(status, now):
    """status.install once the cluster is installed and connected."""
    cur = dict((status or {}).get("install") or {})
    # run is left as it stands (a merge keeps it), so a status read before a removal in the same job is good enough
    return {"stage": "Installed", "endedAt": cur.get("endedAt") or fmt_time(now),
            "lastFailure": None, "stoppedGeneration": None, "stoppedRequest": None, "observedAt": fmt_time(now)}


# ---- the guarded removal of a stopped build ----------------------------------------------------------------------
def may_remove_build(obj, cd, hub_read_ok, release_failed=False, event=None, recheck=False):
    """Whether the build of a stopped installation may be removed before a new one: (allowed, why).

    All of these must hold, read fresh in the same run:
    1. the hub's build is not installed;
    2. the hub's build has stopped (ProvisionStopped True), or a failed release left no ClusterDeployment at all;
    3. the object records no installed cluster (clusterInstalled not true, stage not Finishing or Installed);
    4. someone asked since the stop: a newer generation, or a Retry request other than the one handled at the stop
       (event: {reason, publishedAt} of the job's request, a Retry request published after the stop also counts).
    The hub not readable, or the object not readable, means no removal (unknown is treated as installed).
    recheck: the same run decided before and recorded stage CleaningUp; this is the fresh read right before the
    first delete (conditions 1 to 3 again; 4 was established by that decision).
    """
    if not hub_read_ok:
        return False, "the hub could not be read; nothing is removed (an unknown build counts as installed)"
    if not isinstance(obj, dict):
        return False, "the cluster object could not be read; nothing is removed"
    if isinstance(cd, dict) and hub_installed(cd):
        return False, "the hub's build is installed; it is never removed by a run"
    status = obj.get("status") or {}
    install = status.get("install") or {}
    if status.get("clusterInstalled") is True or str(status.get("clusterInstalled")).lower() == "true":
        return False, "the cluster object records an installed cluster; nothing is removed"
    if install.get("stage") in ("Finishing", "Installed"):
        return False, "the cluster object records an installed cluster (stage %s); nothing is removed" % install["stage"]
    if cd is None:
        if not release_failed:
            return False, "there is no stopped build on the hub"
    elif not hub_stopped(cd):
        return False, "the hub's build has not stopped (Hive may still be trying); nothing is removed"
    if recheck:
        if install.get("stage") not in ("Stopped", "CleaningUp"):
            return False, "the cluster object records no stopped installation; nothing is removed"
        return True, "the stopped build is removed for a new installation"
    if install.get("stage") != "Stopped":
        return False, "the cluster object records no stopped installation; nothing is removed"
    generation = _int((obj.get("metadata") or {}).get("generation"), 0)
    stopped_gen = _int(install.get("stoppedGeneration"))
    handled = str(status.get("lastHandledReconcileRequest") or "")
    asked = False
    if stopped_gen is not None and generation > stopped_gen:
        asked = True
    if "stoppedRequest" in install and handled != str(install.get("stoppedRequest") or ""):
        asked = True
    ev = event or {}
    if not ev:
        delivery = status.get("delivery") or {}
        ev = {"reason": delivery.get("reason"), "publishedAt": delivery.get("publishedAt")}
    ended = parse_time(install.get("endedAt"))
    published = parse_time(ev.get("publishedAt"))
    if str(ev.get("reason") or "").endswith("ReconcileRequested") and ended and published and published > ended:
        asked = True
    if not asked:
        return False, "nobody asked for a new installation since the stop; nothing is removed"
    return True, "the stopped build is removed for a new installation"


# ---- the in-flight check of the operator ----------------------------------------------------------------------
def observation_fresh(status, now, stale_minutes):
    """For an acknowledged event of a cluster: True/False when status.install.observedAt (written by this event's
    job, i.e. not older than delivery.jobStartedAt) says whether the job still watches; None when the job has not
    observed yet (the kind's job time applies)."""
    status = status or {}
    install = status.get("install") or {}
    observed = parse_time(install.get("observedAt"))
    started = parse_time((status.get("delivery") or {}).get("jobStartedAt"))
    if observed is None or started is None or observed < started:
        return None
    return (now - observed).total_seconds() / 60.0 < float(stale_minutes)
