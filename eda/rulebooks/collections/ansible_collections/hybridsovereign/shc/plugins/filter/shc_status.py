# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

__metaclass__ = type

from ansible_collections.hybridsovereign.shc.plugins.module_utils.shc_status import (
    build_summary, derive_phase, merge_conditions)


def shc_merge_conditions(old, new):
    """Merge a list of conditions into another by type (see the cr_status module)."""
    return merge_conditions(old or [], new or [])


def shc_phase(obj):
    """Derived status.phase of an object."""
    return derive_phase(obj or {})


def shc_summary(obj):
    """Tenant-safe summary of an object."""
    obj = obj or {}
    conds = (obj.get("status") or {}).get("conditions") or []
    return build_summary(obj, conds, derive_phase(obj, conds))


class FilterModule(object):
    def filters(self):
        return {"shc_merge_conditions": shc_merge_conditions, "shc_phase": shc_phase, "shc_summary": shc_summary}
