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


def shc_install_installed(status):
    return I.install_installed(_status(status), _now())


def shc_install_asked(obj):
    """{asked, why} for a cluster whose installation stopped: whether someone asked for a new one since the stop."""
    asked, why = I.asked_since_stop(obj if isinstance(obj, dict) else {})
    return {"asked": asked, "why": why}


def shc_install_overdue(status, deadline_minutes):
    """Whether the recorded installation runs past the deadline (a run then makes one observation and ends)."""
    return I.overdue(_status(status), _now(), deadline_minutes)


def shc_install_stopped_seen(obj):
    return I.install_stopped_seen(obj if isinstance(obj, dict) else {})


def shc_install_resumed(_unused=None):
    return I.install_resumed({})


def shc_install_spec_digest(spec):
    return I.spec_digest(spec)


class FilterModule(object):
    def filters(self):
        return {"shc_install_preparing": shc_install_preparing, "shc_install_starting": shc_install_starting,
                "shc_install_cleaning": shc_install_cleaning, "shc_install_installed": shc_install_installed,
                "shc_install_asked": shc_install_asked, "shc_install_overdue": shc_install_overdue,
                "shc_install_stopped_seen": shc_install_stopped_seen,
                "shc_install_spec_digest": shc_install_spec_digest, "shc_install_resumed": shc_install_resumed}
