"""Deterministic working-time allocation, independent of Flask and the database.

The caller supplies the actual working dates (including transferred Saturdays),
contracts, dated group assignments and approved absences. Minutes are integers;
a five-day full-time week is 32 hours for teachers, 26 teaching hours for trainee
teachers, and 40 hours for other staff. Breaks are unpaid. This module implements
these configured rules, rather than making a legal-compliance determination.

Early openers and afternoon substitutes are chosen together, so a nursery
assistant needed until closing cannot also be the 06:00 opener. Search is bounded
and unresolved rules are always returned as issues. No overtime is invented.
"""
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


TEACHER = "teacher"
NURSERY = "nursery_assistant"
ASSISTANT = "teaching_assistant"
ROLES = {TEACHER, NURSERY, ASSISTANT, "secretary", "employee_under_the_labour_code"}
EARLY_SHIFTS = {TEACHER: "early_teacher", NURSERY: "early_nursery"}
SEARCH_LIMIT = 10000


def _date(value):
    return value if isinstance(value, date) else date.fromisoformat(value)


def _during(item, day):
    return (not item.get("start_date") or str(item["start_date"]) <= day) and (
        not item.get("end_date") or str(item["end_date"]) >= day
    )


def _site(item):
    return item.get("site_id") or 0


def _key(value):
    return str(value)


def _issue(code, message, *, day=None, group_id=None, contract_id=None, severity="error", params=None):
    result = {"code": code, "message": message, "severity": severity}
    if params:
        result["params"] = params
    for key, value in (("day", day), ("group_id", group_id), ("contract_id", contract_id)):
        if value is not None:
            result[key] = value
    return result


def _deduplicate(issues):
    seen, result = set(), []
    for issue in issues:
        key = repr(sorted(issue.items()))
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result


def _daily_minutes(worker, day=None):
    if day is not None and day in worker.get("_day_targets", {}):
        return worker["_day_targets"][day]
    try:
        hours = Decimal(str(worker.get("weekly_hours")))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not hours.is_finite() or hours <= 0 or hours > 40:
        return None
    ratio = hours / Decimal(40)
    base = Decimal(384 if worker.get("role") == TEACHER else 480)
    work = int((base * ratio).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    teaching = int((Decimal(312 if worker.get("trainee") else 384) * ratio).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )) if worker.get("role") == TEACHER else 0
    return work, teaching


def _prepare(payload):
    """Distribute fractional minutes evenly without changing weekly totals."""
    prepared = dict(payload)
    prepared["workers"] = []
    for original in payload.get("workers", []):
        worker = dict(original)
        worker["_day_targets"] = {}
        if _daily_minutes(worker) is not None:
            weeks = defaultdict(list)
            for day in sorted(set(str(day) for day in payload.get("days", []))):
                if _during(worker, day) and not _absent(payload, worker, day):
                    monday = _date(day) - timedelta(days=_date(day).weekday())
                    weeks[monday].append(day)
            ratio = Decimal(str(worker["weekly_hours"])) / Decimal(40)
            daily_work = Decimal(384 if worker.get("role") == TEACHER else 480) * ratio
            daily_teaching = Decimal(312 if worker.get("trainee") else 384) * ratio if worker.get("role") == TEACHER else Decimal(0)
            for days in weeks.values():
                previous = [0, 0]
                for index, day in enumerate(days, 1):
                    totals = [int((amount * index).quantize(Decimal(1), rounding=ROUND_HALF_UP)) for amount in (daily_work, daily_teaching)]
                    worker["_day_targets"][day] = tuple(total - old for total, old in zip(totals, previous))
                    previous = totals
        prepared["workers"].append(worker)
    return prepared


def _assignment(worker, day):
    assignments = [item for item in worker.get("assignments", []) if _during(item, day)]
    return assignments[0] if len(assignments) == 1 else None


def _preferred(worker, day):
    assignment = _assignment(worker, day) or {}
    # 1970-01-05 was a Monday. The phase is persistent rather than month-relative.
    week = (_date(day) - date(1970, 1, 5)).days // 7
    phase = int(assignment.get("shift_phase") or 0) % 2
    return "morning" if (week + phase) % 2 == 0 else "afternoon"


def _absent(payload, worker, day):
    return [absence for absence in payload.get("absences", [])
            if absence["user_id"] == worker["user_id"] and _during(absence, day)]


def _intervals(entry):
    start, end = entry.get("start_minute"), entry.get("end_minute")
    if not isinstance(start, int) or not isinstance(end, int) or end <= start:
        return []
    pause, length = entry.get("break_start"), entry.get("break_minutes", 0)
    if length and isinstance(pause, int):
        return [(start, pause), (pause + length, end)]
    return [(start, end)]


def _coverage(intervals, start, end):
    cursor = start
    for left, right in sorted(intervals):
        if right <= cursor:
            continue
        if left > cursor:
            return False
        cursor = max(cursor, right)
        if cursor >= end:
            return True
    return cursor >= end


def _overlap(first, second):
    return sum(max(0, min(b, d) - max(a, c))
               for a, b in _intervals(first) for c, d in _intervals(second))


def _entry(worker, day, shift=None, group_id=None, note=""):
    assignment = _assignment(worker, day)
    if group_id is None and assignment:
        group_id = assignment.get("group_id")
    result = {"day": day, "contract_id": worker["contract_id"], "user_id": worker["user_id"],
              "group_id": group_id, "start_minute": None, "end_minute": None,
              "break_start": None, "break_minutes": 0, "work_minutes": 0,
              "teaching_minutes": 0, "shift": shift or "unassigned", "note": note,
              "note_parts": [{"message": note, "params": {}}] if note else []}
    if shift in (None, "absence", "unassigned") or _daily_minutes(worker) is None:
        return result
    work, teaching = _daily_minutes(worker, day)
    pause = 20 if work > 360 else 0
    if shift == "afternoon":
        end = 1050
        start = end - work - pause
    else:
        start = {"early_teacher": 420, "early_nursery": 360}.get(shift, 480)
        end = start + work + pause
    # The morning teacher remains in the group for the entire 08:00–12:00 window.
    break_start = (max(720, start + 240) if worker.get("role") == TEACHER and shift != "afternoon"
                   else start + min(120 if worker.get("role") == TEACHER else 240, work)) if pause else None
    result.update(start_minute=start, end_minute=end, break_start=break_start,
                  break_minutes=pause, work_minutes=work, teaching_minutes=teaching)
    return result


def _day_context(payload, day):
    workers = [worker for worker in payload.get("workers", []) if _during(worker, day)]
    counts = Counter(worker["user_id"] for worker in workers)
    counts.update(worker["user_id"] for worker in payload.get("external_contracts", []) if _during(worker, day))
    issues, usable = [], []
    for worker in workers:
        cid = worker["contract_id"]
        if counts[worker["user_id"]] > 1:
            issues.append(_issue("overlapping_contracts", "The employee has overlapping active contracts; resolve them before scheduling.", day=day, contract_id=cid))
            continue
        if worker.get("role") not in ROLES:
            issues.append(_issue("unknown_role", "No working-time rule is configured for this job role.", day=day, contract_id=cid))
            continue
        if _daily_minutes(worker) is None:
            issues.append(_issue("invalid_weekly_hours", "Contract weekly hours must be a positive number no greater than 40.", day=day, contract_id=cid))
            continue
        assignments = [item for item in worker.get("assignments", []) if _during(item, day)]
        if len(assignments) > 1:
            issues.append(_issue("overlapping_assignments", "The employee has overlapping group assignments on this date.", day=day, contract_id=cid))
            continue
        assignment = _assignment(worker, day)
        if worker["role"] in (TEACHER, NURSERY) and not assignment:
            issues.append(_issue("missing_group", "Teachers and nursery assistants must have a group assignment.", day=day, contract_id=cid))
            continue
        if assignment:
            group = next((g for g in payload.get("groups", []) if g["id"] == assignment.get("group_id") and _during(g, day)), None)
            if group is None or _site(group) != _site(worker):
                issues.append(_issue("invalid_group", "The assigned group is not active at the employee’s workplace.", day=day, contract_id=cid))
                continue
        usable.append(worker)
    return workers, usable, issues


def _merges(payload, day):
    groups = {group["id"]: group for group in payload.get("groups", []) if _during(group, day)}
    mapping, issues = {}, []
    for merge in payload.get("merges", []):
        if str(merge.get("day")) != day:
            continue
        source, target = merge.get("source_group_id"), merge.get("target_group_id")
        if source not in groups or target not in groups or source == target or _site(groups[source]) != _site(groups[target]):
            issues.append(_issue("invalid_merge", "A merge requires two different active groups at the same workplace.", day=day, group_id=source))
        elif source in mapping:
            issues.append(_issue("duplicate_merge", "A group can merge into only one other group per day.", day=day, group_id=source))
        else:
            mapping[source] = target
    # Only direct merges are accepted: the printed target must itself be open.
    for source, target in list(mapping.items()):
        if target in mapping:
            issues.append(_issue("chained_merge", "The destination of a group merge cannot itself be merged into another group.", day=day, group_id=source))
    invalid = {issue.get("group_id") for issue in issues}
    return {source: target for source, target in mapping.items() if source not in invalid}, issues


def _teacher_rows(workers, day, early_id):
    """Choose the best feasible AM/PM orientation separately for each group."""
    if not workers:
        return [], 0
    if len(workers) == 1:
        worker = workers[0]
        shift = "early_teacher" if worker["contract_id"] == early_id else "morning"
        return [_entry(worker, day, shift)], int(_preferred(worker, day) != "morning")
    candidates = []
    forced = [worker for worker in workers if worker["contract_id"] == early_id]
    for lead in forced or workers:
        rows, deviations = [], 0
        for worker in workers:
            shift = "morning" if worker == lead else "afternoon"
            deviations += int(_preferred(worker, day) != shift)
            rows.append(_entry(worker, day, "early_teacher" if worker["contract_id"] == early_id else shift))
        teacher_intervals = [interval for row in rows for interval in _intervals(row)]
        violations = int(not _coverage(teacher_intervals, 480, 720))
        violations += int(max(_overlap(a, b) for index, a in enumerate(rows) for b in rows[index + 1:]) < 120)
        candidates.append((violations, deviations, _key(lead["contract_id"]), rows))
    violations, deviations, _, rows = min(candidates, key=lambda item: item[:3])
    return rows, deviations + violations * 1000


def _matching(needs, candidates, excluded, budget):
    """Explore complete matchings; short-hour candidates may still leave a gap."""
    needs = sorted(needs, key=lambda gid: (len([w for w in candidates[gid] if w["contract_id"] != excluded]), _key(gid)))
    result, matches = {}, []

    def visit(index, used):
        if index == len(needs):
            matches.append(dict(result))
            return
        gid = needs[index]
        for worker in candidates[gid]:
            if budget[0] >= SEARCH_LIMIT:
                return
            budget[0] += 1
            cid = worker["contract_id"]
            if cid == excluded or cid in used:
                continue
            result[gid] = worker
            visit(index + 1, used | {cid})
            result.pop(gid, None)

    visit(0, set())
    return matches


def _rotation_key(worker, role, counts, last, day):
    cid = worker["contract_id"]
    # Count is primary; consecutive days are avoided whenever equally fair.
    return counts[(role, cid)], int(last.get(role) == cid), _key(cid)


def build_schedule(payload):
    """Return ``{entries, issues}`` for the actual ISO working dates in payload."""
    payload = _prepare(payload)
    days = sorted(set(str(day) for day in payload.get("days", [])))
    groups = {group["id"]: group for group in payload.get("groups", [])}
    all_entries, issues = [], []
    counts, last = Counter(), {}
    for historic in sorted(payload.get("history", []), key=lambda row: str(row["day"])):
        if days and str(historic["day"]) >= days[0]:
            continue
        worker = next((w for w in payload.get("workers", []) if w["contract_id"] == historic["contract_id"]), None)
        for role, shift in EARLY_SHIFTS.items():
            start = 420 if role == TEACHER else 360
            if historic.get("shift") == shift or (worker and worker["role"] == role and historic.get("start_minute") == start):
                counts[(role, historic["contract_id"])] += 1
                last[(_site(worker or {}), role)] = historic["contract_id"]
    for day in days:
        active_groups = {gid: group for gid, group in groups.items() if _during(group, day)}
        workers, usable, day_issues = _day_context(payload, day)
        issues.extend(day_issues)
        mapping, merge_issues = _merges(payload, day)
        issues.extend(merge_issues)
        rows = {}
        for worker in workers:
            absences = _absent(payload, worker, day)
            if absences:
                rows[worker["contract_id"]] = _entry(worker, day, "absence", note="; ".join(dict.fromkeys(a.get("label") or "Absence" for a in absences)))
            elif worker not in usable:
                rows[worker["contract_id"]] = _entry(worker, day, "unassigned", note="Scheduling information is missing or inconsistent.")
        present = [worker for worker in usable if not _absent(payload, worker, day)]
        for site in sorted({_site(g) for g in active_groups.values()} | {_site(w) for w in present}, key=_key):
            local = [worker for worker in present if _site(worker) == site]
            local_groups = {gid: group for gid, group in active_groups.items() if _site(group) == site}
            teachers = [worker for worker in local if worker["role"] == TEACHER]
            nurses = [worker for worker in local if worker["role"] == NURSERY]
            group_teachers = {gid: [worker for worker in teachers if (_assignment(worker, day) or {}).get("group_id") == gid]
                              for gid in local_groups if gid not in mapping}
            needs = [gid for gid, members in group_teachers.items() if len(members) == 1]
            candidates = {gid: sorted([worker for worker in local if worker["role"] == ASSISTANT or (
                worker["role"] == NURSERY and (_assignment(worker, day) or {}).get("group_id") == gid)],
                key=lambda worker: (worker["role"] != NURSERY, _key(worker["contract_id"]))) for gid in needs}
            local_last = {role: last.get((site, role)) for role in EARLY_SHIFTS}
            early_teachers = sorted(teachers, key=lambda w: _rotation_key(w, TEACHER, counts, local_last, day)) or [None]
            early_nurses = sorted(nurses, key=lambda w: _rotation_key(w, NURSERY, counts, local_last, day)) or [None]
            best_solution, budget = None, [0]
            for early_teacher in early_teachers:
                if budget[0] >= SEARCH_LIMIT:
                    break
                teacher_rows, deviation = [], 0
                for gid, members in group_teachers.items():
                    result, score = _teacher_rows(members, day, early_teacher["contract_id"] if early_teacher else None)
                    teacher_rows.extend(result)
                    deviation += score
                # Staff of a manually merged source join the named destination.
                for worker in teachers:
                    gid = (_assignment(worker, day) or {}).get("group_id")
                    if gid in mapping:
                        shift = "early_teacher" if worker == early_teacher else _preferred(worker, day)
                        teacher_rows.append(_entry(worker, day, shift, group_id=mapping[gid]))
                for early_nurse in early_nurses:
                    if budget[0] >= SEARCH_LIMIT:
                        break
                    budget[0] += 1
                    matchings = _matching(needs, candidates, early_nurse["contract_id"] if early_nurse else None, budget)
                    for matched in matchings:
                        trial = {row["contract_id"]: row for row in teacher_rows}
                        for gid, worker in matched.items():
                            trial[worker["contract_id"]] = _entry(worker, day, "afternoon", group_id=gid,
                                note="Afternoon cover in this group.")
                        for worker in local:
                            if worker["contract_id"] in trial:
                                continue
                            shift = "early_nursery" if worker == early_nurse else _preferred(worker, day)
                            gid = (_assignment(worker, day) or {}).get("group_id")
                            trial[worker["contract_id"]] = _entry(worker, day, shift, group_id=mapping.get(gid, gid))
                        # Local rule violations outrank preferences and rotation.
                        trial_errors = _validate_day(payload, day, list(trial.values()) + list(rows.values()), restrict_site=site,
                                                    include_missing=False)
                        error_count = sum(issue["severity"] == "error" for issue in trial_errors)
                        teacher_fair = _rotation_key(early_teacher, TEACHER, counts, local_last, day)[:2] if early_teacher else (0, 0)
                        nurse_fair = _rotation_key(early_nurse, NURSERY, counts, local_last, day)[:2] if early_nurse else (0, 0)
                        score = (error_count, sum((teacher_fair[1], nurse_fair[1])), deviation, sum((teacher_fair[0], nurse_fair[0])),
                                 _key(early_teacher["contract_id"] if early_teacher else ""), _key(early_nurse["contract_id"] if early_nurse else ""))
                        if best_solution is None or score < best_solution[0]:
                            best_solution = (score, trial, early_teacher, early_nurse)
            if budget[0] >= SEARCH_LIMIT:
                issues.append(_issue("search_limit", "The scheduling search reached its limit; unresolved conflicts require a manual decision.", day=day, severity="warning"))
            if best_solution is not None:
                _, trial, chosen_teacher, chosen_nurse = best_solution
                rows.update(trial)
                for role, chosen in ((TEACHER, chosen_teacher), (NURSERY, chosen_nurse)):
                    if chosen:
                        if last.get((site, role)) == chosen["contract_id"]:
                            issues.append(_issue("early_rotation_repeated", "The early start could not be assigned to a different eligible employee on this day.", day=day, contract_id=chosen["contract_id"], severity="warning"))
                        counts[(role, chosen["contract_id"])] += 1
                        last[(site, role)] = chosen["contract_id"]
            else:
                # Preserve normal hours while explicitly reporting the unresolved opening/closing conflict.
                for gid, members in group_teachers.items():
                    result, _ = _teacher_rows(members, day, None)
                    rows.update((row["contract_id"], row) for row in result)
                for worker in local:
                    rows.setdefault(worker["contract_id"], _entry(worker, day, _preferred(worker, day)))
                issues.append(_issue("allocation_conflict", "Early opening and afternoon cover cannot both be allocated; an HR or CEO decision is required.", day=day))
        for row in rows.values():
            worker = next(worker for worker in workers if worker["contract_id"] == row["contract_id"])
            original_group = (_assignment(worker, day) or {}).get("group_id")
            for source, target in mapping.items():
                if original_group in (source, target) or row.get("group_id") in (source, target):
                    text = "Merged group: %(source)s → %(target)s." % {"source": groups[source]["name"], "target": groups[target]["name"]}
                    row["note"] = " ".join(part for part in (row.get("note"), text) if part)
                    row["note_parts"].append({"message": "Merged group: %(source)s → %(target)s.",
                        "params": {"source": groups[source]["name"], "target": groups[target]["name"]}})
            if row["shift"] in ("morning", "afternoon", "early_teacher") and worker["role"] == TEACHER:
                actual = "morning" if row["shift"] == "early_teacher" else row["shift"]
                if actual != _preferred(worker, day):
                    issues.append(_issue("shift_preference_changed", "Coverage or the rotation of early starts requires a different shift on this day.", day=day, contract_id=worker["contract_id"], severity="warning"))
        all_entries.extend(sorted(rows.values(), key=lambda row: _key(row["contract_id"])))
    issues.extend(validate_schedule(payload, all_entries))
    return {"entries": all_entries, "issues": _deduplicate(issues)}


def _validate_day(payload, day, entries, *, restrict_site=None, include_missing=True):
    workers, usable, issues = _day_context(payload, day)
    if restrict_site is not None:
        workers = [w for w in workers if _site(w) == restrict_site]
        usable = [w for w in usable if _site(w) == restrict_site]
        ids = {w["contract_id"] for w in workers}
        issues = [i for i in issues if i.get("contract_id") in ids]
    worker_map = {worker["contract_id"]: worker for worker in workers}
    entries = [entry for entry in entries if entry["day"] == day and entry["contract_id"] in worker_map]
    by_contract = defaultdict(list)
    valid_rows = []
    for row in entries:
        by_contract[row["contract_id"]].append(row)
        worker = worker_map[row["contract_id"]]
        cid = worker["contract_id"]
        numeric = [row.get(key) for key in ("work_minutes", "teaching_minutes", "break_minutes")]
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in numeric):
            issues.append(_issue("invalid_minutes", "Working, teaching and break times must be non-negative whole minutes.", day=day, contract_id=cid))
            continue
        work, teaching, pause = numeric
        if work > 480:
            issues.append(_issue("daily_limit", "Daily working time cannot exceed 8 hours, excluding the break.", day=day, contract_id=cid))
        if teaching > work or (worker["role"] != TEACHER and teaching):
            issues.append(_issue("invalid_teaching_minutes", "Teaching time cannot exceed working time and is available only for teachers.", day=day, contract_id=cid))
        if _absent(payload, worker, day):
            if work or teaching or pause or row.get("start_minute") is not None or row.get("end_minute") is not None:
                issues.append(_issue("absence_work", "No working time can be assigned on a day of absence.", day=day, contract_id=cid))
            continue
        if not work:
            if teaching or pause or row.get("start_minute") is not None or row.get("end_minute") is not None:
                issues.append(_issue("zero_work_times", "A zero-hours entry cannot contain start, end or break times.", day=day, contract_id=cid))
            continue
        start, end, break_start = row.get("start_minute"), row.get("end_minute"), row.get("break_start")
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= 1440 or end - start != work + pause:
            issues.append(_issue("invalid_interval", "Start, end, break and working times do not agree.", day=day, contract_id=cid))
            continue
        expected_pause = 20 if work > 360 else 0
        if pause != expected_pause or (pause and (not isinstance(break_start, int) or break_start < start or break_start + pause > end)) or (not pause and break_start is not None):
            issues.append(_issue("invalid_break", "Working time over six hours requires a 20-minute break; otherwise there is no break.", day=day, contract_id=cid))
            continue
        anchored = start == 480 or end == 1050 or (worker["role"] == TEACHER and start == 420) or (worker["role"] == NURSERY and start == 360)
        if not anchored:
            issues.append(_issue("invalid_shift_anchor", "Work must start at 08:00 or end at 17:30, except for the designated 07:00 teacher and 06:00 nursery assistant.", day=day, contract_id=cid))
        assigned_group = next((g for g in payload.get("groups", []) if g["id"] == row.get("group_id") and _during(g, day)), None)
        if row.get("group_id") is not None and (assigned_group is None or _site(assigned_group) != _site(worker)):
            issues.append(_issue("invalid_entry_group", "The schedule entry’s group is not active at the employee’s workplace.", day=day, contract_id=cid))
            continue
        assignment = _assignment(worker, day)
        if worker["role"] in (TEACHER, NURSERY) and assignment and row.get("group_id") != assignment.get("group_id"):
            mapping, _ = _merges(payload, day)
            if mapping.get(assignment.get("group_id")) != row.get("group_id"):
                issues.append(_issue("unauthorised_group", "The employee can work in another group only through a recorded group merge.", day=day, contract_id=cid))
                continue
        if worker["role"] == TEACHER:
            actual_shift = "morning" if start <= 480 else "afternoon"
            if actual_shift != _preferred(worker, day):
                issues.append(_issue("shift_preference_changed", "Coverage or the rotation of early starts requires a different shift on this day.", day=day, contract_id=cid, severity="warning"))
        valid_rows.append(row)
    for worker in workers:
        cid = worker["contract_id"]
        if include_missing and not by_contract[cid]:
            issues.append(_issue("missing_entry", "The employee’s daily schedule entry is missing.", day=day, contract_id=cid))
        if len(by_contract[cid]) > 1:
            issues.append(_issue("duplicate_entry", "An employee can have only one schedule entry per day.", day=day, contract_id=cid))
    mapping, merge_issues = _merges(payload, day)
    issues.extend(merge_issues)
    groups = [group for group in payload.get("groups", []) if _during(group, day) and (restrict_site is None or _site(group) == restrict_site)]
    for group in groups:
        gid = group["id"]
        if gid in mapping:
            continue
        roster = [worker for worker in usable if worker["role"] == TEACHER and (_assignment(worker, day) or {}).get("group_id") == gid]
        present = [worker for worker in roster if not _absent(payload, worker, day)]
        group_rows = [row for row in valid_rows if row.get("group_id") == gid]
        teacher_rows = [row for row in group_rows if worker_map[row["contract_id"]]["role"] == TEACHER]
        if not roster:
            issues.append(_issue("no_group_teacher", "No active teacher is assigned to this group.", day=day, group_id=gid))
        elif not present:
            issues.append(_issue("manual_merge_required", "All teachers in this group are absent; HR or CEO must record a group merge.", day=day, group_id=gid))
        if not _coverage([interval for row in teacher_rows for interval in _intervals(row)], 480, 720):
            issues.append(_issue("morning_teacher_coverage", "A teacher must be continuously present in the group between 08:00 and 12:00.", day=day, group_id=gid))
        present_ids = {worker["contract_id"] for worker in present}
        own_teacher_rows = [row for row in teacher_rows if row["contract_id"] in present_ids]
        if len(present) >= 2 and (len(own_teacher_rows) < 2 or max((_overlap(a, b) for index, a in enumerate(own_teacher_rows) for b in own_teacher_rows[index + 1:]), default=0) < 120):
            issues.append(_issue("teacher_overlap", "Teachers in the group require at least two hours of overlapping working time, excluding breaks.", day=day, group_id=gid))
        closing_rows = [row for row in group_rows if row.get("end_minute") == 1050 and worker_map[row["contract_id"]]["role"] in (TEACHER, NURSERY, ASSISTANT)]
        if not closing_rows:
            issues.append(_issue("afternoon_coverage", "A teacher, the group’s nursery assistant or a workplace teaching assistant must cover the afternoon until 17:30.", day=day, group_id=gid))
        care_rows = [row for row in group_rows if worker_map[row["contract_id"]]["role"] in (TEACHER, NURSERY, ASSISTANT)]
        if not _coverage([interval for row in care_rows for interval in _intervals(row)], 480, 1050):
            issues.append(_issue("group_coverage_gap", "The group has an uncovered period between 08:00 and 17:30; revise the afternoon cover or record a group merge.", day=day, group_id=gid))
        if len(present) == 1 and own_teacher_rows and own_teacher_rows[0]["start_minute"] > 480:
            issues.append(_issue("single_teacher_morning", "The group’s only available teacher must work in the morning.", day=day, group_id=gid))
    for site in {_site(group) for group in groups} | {_site(worker) for worker in workers}:
        local_rows = [row for row in valid_rows if _site(worker_map[row["contract_id"]]) == site]
        for role, start, code, message in ((TEACHER, 420, "missing_early_teacher", "Exactly one teacher at the workplace must start at 07:00."),
                                          (NURSERY, 360, "missing_early_nursery", "Exactly one nursery assistant at the workplace must start at 06:00.")):
            count = sum(worker_map[row["contract_id"]]["role"] == role and row["start_minute"] == start for row in local_rows)
            if count != 1:
                issues.append(_issue(code, message, day=day))
    return issues


def validate_schedule(payload, entries, *, restrict_to_entry_days=False, validation_days=None):
    """Validate stored/manual rows too; optionally restrict checks to shown dates.

    With ``restrict_to_entry_days=True``, a month-boundary week is checked only
    against its supplied dates, not against working days outside that month.
    Prefer explicit ``validation_days`` for persisted months, including empty
    schedules: a missing entry must never remove a date from validation.
    """
    payload = _prepare(payload)
    days = sorted(set(str(day) for day in payload.get("days", [])))
    if validation_days is not None:
        selected = {str(day) for day in validation_days}
        days = [day for day in days if day in selected]
    elif restrict_to_entry_days and entries:
        days = [day for day in days if any(row.get("day") == day for row in entries)]
    issues = []
    allowed_days = set(days)
    worker_map = {worker["contract_id"]: worker for worker in payload.get("workers", [])}
    for row in entries:
        if row.get("day") not in set(str(day) for day in payload.get("days", [])) or row.get("contract_id") not in worker_map:
            issues.append(_issue("unknown_entry", "The schedule entry does not belong to a supplied working day or contract.", day=row.get("day"), contract_id=row.get("contract_id")))
        elif not _during(worker_map[row["contract_id"]], row["day"]):
            issues.append(_issue("inactive_contract_entry", "The schedule entry falls outside the contract’s active dates.", day=row["day"], contract_id=row["contract_id"]))
        elif row.get("user_id") != worker_map[row["contract_id"]]["user_id"]:
            issues.append(_issue("wrong_employee", "The schedule entry’s employee and contract do not match.", day=row.get("day"), contract_id=row["contract_id"]))
    for day in days:
        issues.extend(_validate_day(payload, day, entries))
    expected, actual = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    for day in days:
        monday = _date(day) - timedelta(days=_date(day).weekday())
        for worker in payload.get("workers", []):
            target = _daily_minutes(worker, day)
            if _during(worker, day) and target and not _absent(payload, worker, day):
                key = (worker["contract_id"], monday.isoformat())
                expected[key][0] += target[0]
                expected[key][1] += target[1]
    for row in entries:
        if row.get("day") not in allowed_days or row.get("contract_id") not in worker_map:
            continue
        monday = _date(row["day"]) - timedelta(days=_date(row["day"]).weekday())
        key = (row["contract_id"], monday.isoformat())
        for index, field in enumerate(("work_minutes", "teaching_minutes")):
            if isinstance(row.get(field), int):
                actual[key][index] += row[field]
    previous = {}
    rotation_rows = [row for row in payload.get("history", []) if not days or str(row["day"]) < days[0]] + list(entries)
    for row in sorted(rotation_rows, key=lambda item: (str(item["day"]), _key(item["contract_id"]))):
        worker = worker_map.get(row.get("contract_id"))
        if not worker:
            continue
        role = worker.get("role")
        is_early = (role in EARLY_SHIFTS and row.get("shift") == EARLY_SHIFTS[role]) or (
            role == TEACHER and row.get("start_minute") == 420) or (
            role == NURSERY and row.get("start_minute") == 360)
        if not is_early:
            continue
        key = (_site(worker), role)
        if row["day"] in allowed_days and previous.get(key) == row["contract_id"]:
            issues.append(_issue("early_rotation_repeated", "The early start could not be assigned to a different eligible employee on this day.", day=row["day"], contract_id=row["contract_id"], severity="warning"))
        previous[key] = row["contract_id"]
    for key in sorted(expected.keys() | actual.keys(), key=lambda item: (item[1], _key(item[0]))):
        cid, monday = key
        if expected[key][0] != actual[key][0]:
            issues.append(_issue("weekly_work_target", "Working time for the week starting %(week)s is %(actual)s minutes; the proportional target is %(expected)s minutes.", day=monday, contract_id=cid, params={"week": monday, "actual": actual[key][0], "expected": expected[key][0]}))
        if expected[key][1] != actual[key][1]:
            issues.append(_issue("weekly_teaching_target", "Teaching time for the week starting %(week)s is %(actual)s minutes; the proportional target is %(expected)s minutes.", day=monday, contract_id=cid, params={"week": monday, "actual": actual[key][1], "expected": expected[key][1]}))
    return _deduplicate(issues)
