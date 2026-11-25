"""Account-specific catalogue eligibility. Does not submit or schedule requests.

canJoin describes the current action; ableJoinFlag is the explicit eligibility
signal retained by the original detail adapter. A negative canJoin before opening
does not establish ineligibility. Missing/ambiguous evidence stays unknown.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone, timedelta

BEIJING = timezone(timedelta(hours=8))
PERSONAL_KEYS = ("canJoin", "ableJoinFlag", "unableJoinReason", "signUpId",
                 "injoin", "cancel", "joinCanCancel")
BOARD_KEYS = ("can_join", "can_cancel", "joined_by_me", "signup_id", "eligibility",
              "eligibility_reason", "eligibility_checked_at", "registration_phase",
              "board_status", "sign_start", "sign_end", "act_start", "act_end")


def flag(value):
    if isinstance(value, bool):
        return value
    if value in (1, "1"):
        return True
    if value in (0, "0"):
        return False
    if isinstance(value, str):
        value = value.strip().lower()
        if value == "true":
            return True
        if value == "false":
            return False
    return None


def epoch(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(value)
        if n > 1e12:
            n /= 1000
        if 0 < n < 253402300800:
            return n
        return None
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        s = str(value).strip().replace(".", "-").replace("/", "-")
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=BEIJING)).timestamp()
    except (ValueError, OverflowError):
        return None


def field(raw, *names):
    lower = {str(k).lower(): v for k, v in raw.items()}
    return next((lower[n.lower()] for n in names
                 if lower.get(n.lower()) not in (None, "")), None)


def window(value):
    dates = re.findall(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?",
                       str(value or ""))
    return [epoch(d) for d in dates[:2]] + [None] * max(0, 2 - len(dates))


def stamp_detail(raw, uid, now=None):
    keys = {str(k).lower() for k in raw}
    return {**{k: None for k in PERSONAL_KEYS if k.lower() not in keys}, **raw, "_eligibility_uid": str(uid),
            "_eligibility_checked_at": time.time() if now is None else now}


def decorate(act, uid, ttl=300, now=None):
    """Decorate normalized data; old/shared cache permission fields are never trusted."""
    now = time.time() if now is None else now
    raw = act.get("raw") or {}
    start, end = window(field(raw, "joindate"))
    act["sign_start"] = epoch(field(raw, "joinstartdate", "joinStartTime", "signupStartTime",
                                    "signStartTime", "applyStartTime", "enrollStartTime",
                                    "registrationStartTime")) or start or act.get("sign_start")
    act["sign_end"] = epoch(field(raw, "joinenddate", "signupEndTime", "signEndTime",
                                  "applyEndTime", "enrollEndTime", "registrationEndTime")) or end or act.get("sign_end")
    astart, aend = window(field(raw, "startdate"))
    act["act_start"] = epoch(field(raw, "startdateDate", "activityStartTime", "startTime", "beginTime")) or astart or act.get("act_start")
    act["act_end"] = epoch(field(raw, "enddate", "activityEndTime", "endTime", "finishTime")) or aend or act.get("act_end")
    checked = epoch(raw.get("_eligibility_checked_at"))
    trusted = (str(raw.get("_eligibility_uid", "")) == str(uid) and checked is not None
               and 0 <= now - checked <= ttl)
    can = flag(field(raw, "canJoin")) if trusted else None
    able = flag(field(raw, "ableJoinFlag")) if trusted else None
    reason = str(field(raw, "unableJoinReason") or "").strip() if trusted else ""
    sid = str(field(raw, "signUpId") or "0") if trusted else "0"
    mine = sid.lower() not in ("", "0", "none", "null", "undefined")
    act.update(can_join=can, can_cancel=flag(field(raw, "cancel", "joinCanCancel")) if trusted else None,
               joined_by_me=mine, signup_id=sid if mine else "",
               eligibility_checked_at=checked if trusted else None)
    phase = registration_phase(act, now)
    # A time-only rejection never proves that personal conditions fail.
    time_only = bool(re.fullmatch(r"(?:该活动|本活动|活动|当前)?(?:报名)?(?:尚未开始|还未开始|还没开始|未开始|未开放|尚未开放)(?:报名)?[！!。.]?", reason))
    if reason and not time_only and can is not True:
        eligible, why = "ineligible", reason
    elif able is True or can is True:
        eligible, why = "eligible", "符合报名条件" if phase == "soon" else "当前可报名"
    elif phase == "open" and can is False:
        eligible, why = "ineligible", "平台当前不允许此账号报名"
    else:
        eligible, why = "unknown", "报名尚未开放，平台未明确返回个人资格" if phase == "soon" and trusted else "资格待确认，请刷新或查看详情"
    act.update(eligibility=eligible, eligibility_reason=why)
    update_status(act, now)
    return act


def registration_phase(act, now):
    start, end = act.get("sign_start"), act.get("sign_end")
    if end and now >= end:
        return "closed"
    if act.get("state") == "ended":
        return "closed"
    if start and now < start:
        return "soon"
    if (start and now >= start and end and now < end) or act.get("state") in ("open", "full"):
        return "open"
    if act.get("state") == "soon":
        return "soon"
    return "closed" if act.get("state") == "running" else "unknown"


def update_status(act, now=None):
    now = time.time() if now is None else now
    phase = registration_phase(act, now)
    act["registration_phase"] = phase
    if act.get("joined_by_me"):
        status = "joined"
    elif phase == "closed":
        status = "closed"
    elif act.get("quota") == 0 or act.get("state") == "full":
        status = "full"
    elif act.get("eligibility") == "ineligible":
        status = "ineligible"
    elif act.get("eligibility") != "eligible":
        status = "unknown"
    elif phase == "soon":
        status = "soon"
    elif phase == "open" and act.get("can_join") is True:
        status = "open"
    else:
        status = "unknown"
    act["board_status"] = status
    return status


def sort_key(act):
    phase = act.get("registration_phase")
    start = act.get("sign_start")
    # Upcoming first (nearest opening), already open second, unavailable last.
    group = 0 if phase == "soon" else 1 if phase == "open" else 2
    if not start:
        group = 3
    return group, start or float("inf"), str(act.get("aid", ""))
