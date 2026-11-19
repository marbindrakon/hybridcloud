# -*- coding: utf-8 -*-
# status.install blocks of an OpenShift cluster's installation (module_utils shc_install): the role writes them at
# the steps it owns (the first check, the start of an installation, the removal of a stopped one, the end). Each
# filter takes the object's status as read and returns the block to write, or an empty dict when nothing changes.
from __future__ import absolute_import, division, print_function

__metaclass__ = type

import datetime

from ansible_collections.hybridsovereign.shc.plugins.module_utils import shc_install as I


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _status(value):
    return value if isinstance(value, dict) else {}


def shc_install_preparing(status):
    return I.install_preparing(_status(status), _now()) or {}


def shc_install_starting(status):
    return I.install_starting(_status(status), _now()) or {}


def shc_install_cleaning(status):
    return I.install_cleaning(_status(status), _now())


def shc_install_installed(status, cd=None):
    """status.install once installed; cd: the hub's ClusterDeployment (its install time when the watch recorded none)."""
    return I.install_installed(_status(status), _now(), cd if isinstance(cd, dict) else None)


def shc_install_asked(obj, margin_minutes=1):
    """{asked, why} for a cluster whose installation stopped: whether someone asked for a new one since the stop."""
    asked, why = I.asked_since_stop(obj if isinstance(obj, dict) else {}, None, margin_minutes)
    return {"asked": asked, "why": why}


def shc_install_overdue(status, deadline_minutes):
    """Whether the recorded installation runs past the deadline (a run then makes one observation and ends)."""
    return I.overdue(_status(status), _now(), deadline_minutes)


def shc_install_stopped_seen(obj):
    return I.install_stopped_seen(obj if isinstance(obj, dict) else {})


def shc_install_release_young(info, pending_minutes):
    """Whether a release (helm status's info: status, last_deployed) is pending and younger than pending_minutes."""
    info = info if isinstance(info, dict) else {}
    return I.release_young(info.get("status"), info.get("last_deployed"), _now(), pending_minutes)


def shc_install_namespace_wait(status, limit_minutes):
    """{since, over} of the wait for a namespace an earlier object of the same name still holds."""
    return I.namespace_wait(_status(status), _now(), limit_minutes)


def shc_install_resumed(_unused=None):
    return I.install_resumed({})


def shc_install_spec_digest(spec):
    return I.spec_digest(spec)


def shc_failure_message(failed_task, failed_result=None, refused=False, what="provisioning"):
    """status.message of a failed job from ansible_failed_task and ansible_failed_result (the rescue's)."""
    task = failed_task if isinstance(failed_task, dict) else {}
    result = failed_result if isinstance(failed_result, dict) else {}
    msg = result.get("msg")
    return I.failure_message(task.get("action") or "", msg if isinstance(msg, str) else "", task.get("name") or "",
                             refused, what)


def shc_step_failed(step, what="provisioning"):
    """The general sentence for a failed step (the cloud and site call wrappers fail with it)."""
    return I.step_failed(step, what)


class FilterModule(object):
    def filters(self):
        return {"shc_install_preparing": shc_install_preparing, "shc_install_starting": shc_install_starting,
                "shc_install_cleaning": shc_install_cleaning, "shc_install_installed": shc_install_installed,
                "shc_install_asked": shc_install_asked, "shc_install_overdue": shc_install_overdue,
                "shc_install_stopped_seen": shc_install_stopped_seen,
                "shc_install_spec_digest": shc_install_spec_digest, "shc_install_resumed": shc_install_resumed,
                "shc_install_release_young": shc_install_release_young,
                "shc_install_namespace_wait": shc_install_namespace_wait,
                "shc_failure_message": shc_failure_message, "shc_step_failed": shc_step_failed}
