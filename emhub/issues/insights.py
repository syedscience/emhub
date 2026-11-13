# **************************************************************************
# *
# * Authors:     Syed Asfarul Haque (syedasfarul.haque@stjude.org) [1]
# *
# * [1] St. Jude Children's Research Hospital, Memphis, TN
# *
# * This program is free software; you can redistribute it and/or modify
# * it under the terms of the GNU General Public License as published by
# * the Free Software Foundation; either version 3 of the License, or
# * (at your option) any later version.
# *
# * This program is distributed in the hope that it will be useful,
# * but WITHOUT ANY WARRANTY; without even the implied warranty of
# * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# * GNU General Public License for more details.
# *
# * You should have received a copy of the GNU General Public License
# * along with this program; if not, write to the Free Software
# * Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA
# * 02111-1307  USA
# *
# **************************************************************************
"""
Insights for the Microscope Issues module.

Pure functions over issue rows, so the definitions are easy to test with
known fixtures. Definitions (also shown on the Insights page):

  incidents     Issues reported in the period, excluding duplicates.
  response      First staff response (acknowledged_at) minus server
                submission time (reported_at). Median and 90th percentile.
  restoration   Instrument restored (end of the last downtime interval, or
                resolution time) minus impact start (occurred_at, or
                reported_at when unknown). Separate from ticket closure.
  downtime      Union of 'unavailable' intervals per instrument, clipped to
                the period; overlapping issues are not double counted.
  availability  1 - unavailable hours / calendar hours in the period.
  issue rate    Incidents per 100 booked hours (EMhub bookings). Booked
                hours are not operating hours.
  vendor wait   Time issues spent in 'waiting for vendor'.
  recurrence    Same instrument and problem area reported more than once
                within the recurrence window (imported history and 'other'
                excluded).

Imported history (e.g. old logbooks) is counted in totals but excluded from
timing metrics, because its timestamps are not reliable.
"""

import datetime as dt
import math
from collections import Counter, defaultdict

from .models import as_utc

OPEN_STATUSES = ['new', 'acknowledged', 'investigating', 'waiting_vendor', 'reopened']
AGE_BUCKETS = [('< 1 day', 1), ('1-7 days', 7), ('7-30 days', 30), ('> 30 days', None)]


def percentile(values, q):
    """ Percentile with linear interpolation; None for no data. """
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    k = (len(values) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def summary(values):
    values = [v for v in values if v is not None]
    return {'n': len(values), 'median': percentile(values, 0.5),
            'p90': percentile(values, 0.9)}


def merge_intervals(intervals):
    """ Union of (start, end) intervals, returned sorted and non-overlapping. """
    merged = []
    for start, end in sorted(i for i in intervals if i[1] > i[0]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def hours(intervals):
    return sum((e - s).total_seconds() for s, e in intervals) / 3600


def clip(start, end, lo, hi):
    return max(start, lo), min(end, hi)


def week_start(when, tz):
    local = when.astimezone(tz)
    monday = local.date() - dt.timedelta(days=local.weekday())
    return monday.isoformat()


def vendor_wait_hours(issue, now):
    """ Hours spent in 'waiting_vendor', from the issue history. """
    total, since = 0.0, None
    for e in issue.events:
        to = (e.data or {}).get('to')
        if e.kind in ('wait_vendor', 'waiting_vendor') or to == 'waiting_vendor':
            since = since or as_utc(e.at)
        elif since is not None and (to or e.kind in ('resolve', 'duplicate', 'close')):
            total += (as_utc(e.at) - since).total_seconds() / 3600
            since = None
    if since is not None:
        total += (now - since).total_seconds() / 3600
    return total


NO_LAB = 'No lab on record'
TOP_LABS = 15


def issue_labs(issue, lab_of):
    """ The labs (PI names) hit by an issue: the reporter's lab and the labs
    of users who said "I have this too". A staff report counts for the user
    it affected, not for the staff member's own lab. """
    users = [] if issue.staff_report else [issue.reporter_id]
    users += [a.user_id for a in issue.affected]
    labs = {lab_of.get(u) or NO_LAB for u in users if u}
    if len(labs) > 1:
        labs.discard(NO_LAB)
    return labs


def compute(issues, open_issues, instruments, start, end, now, tz,
            booked_hours=None, recurrence_days=30, severities=None, areas=None,
            lab_of=None):
    """ Compute every insight for issues reported in [start, end).

    issues:       issues reported in the period (with events, downtimes, symptoms)
    open_issues:  all currently open issues (backlog is 'now', not the period)
    instruments:  {instrument_id: name} to report on
    booked_hours: {instrument_id: hours booked in EMhub in the period}
    lab_of:       {user_id: lab (PI) name}
    """
    severities = severities or {}
    areas = areas or {}
    booked_hours = booked_hours or {}
    end_or_now = min(end, now)
    period_hours = max(0.0, (end_or_now - start).total_seconds() / 3600)

    incidents = [i for i in issues if i.status != 'duplicate']
    duplicates = len(issues) - len(incidents)
    timed = [i for i in incidents if not i.imported]

    # ---- Response and restoration (timing metrics, non-imported only)
    response = {}
    for i in timed:
        if i.acknowledged_at:
            response[i.id] = (as_utc(i.acknowledged_at) - as_utc(i.reported_at)).total_seconds() / 60
    restoration = {}
    for i in timed:
        if i.resolved_at is None:
            continue
        ends = [as_utc(d.end) for d in i.downtimes if d.end is not None]
        restored = max(ends) if ends else as_utc(i.resolved_at)
        impact = as_utc(i.occurred_at or i.reported_at)
        restoration[i.id] = max(0.0, (restored - impact).total_seconds() / 3600)

    # ---- Downtime: union per instrument of 'unavailable' intervals
    downtime = defaultdict(list)
    for i in issues:
        for d in i.downtimes:
            if d.state != 'unavailable':
                continue
            s, e = clip(as_utc(d.start), as_utc(d.end) if d.end else now, start, end_or_now)
            if e > s:
                downtime[d.instrument_id].append((s, e))

    # ---- Per instrument table
    per_instrument = []
    by_inst = defaultdict(list)
    for i in incidents:
        by_inst[i.instrument_id].append(i)
    for iid, name in instruments.items():  # EMhub order
        items = by_inst.get(iid, [])
        down = hours(merge_intervals(downtime.get(iid, [])))
        booked = booked_hours.get(iid)
        per_instrument.append({
            'instrument_id': iid, 'name': name,
            'incidents': len(items),
            'open': sum(1 for i in open_issues if i.instrument_id == iid),
            'booked_hours': round(booked, 1) if booked is not None else None,
            'per_100_booked_hours': (round(100 * len(items) / booked, 2)
                                     if booked else None),
            'downtime_hours': round(down, 1),
            'availability': (round(1 - down / period_hours, 4)
                             if period_hours else None),
            'median_response_min': percentile([response[i.id] for i in items if i.id in response], 0.5),
            'median_restoration_h': percentile([restoration[i.id] for i in items
                                                if i.id in restoration], 0.5),
            'vendor_wait_hours': round(sum(vendor_wait_hours(i, now) for i in items), 1),
        })

    # ---- Trends
    weeks = Counter()
    weeks_by_severity = defaultdict(Counter)
    for i in incidents:
        w = week_start(as_utc(i.reported_at), tz)
        weeks[w] += 1
        weeks_by_severity[i.severity][w] += 1
    all_weeks = []
    if incidents or period_hours:
        w = dt.date.fromisoformat(week_start(start, tz))
        last = dt.date.fromisoformat(week_start(end_or_now - dt.timedelta(seconds=1), tz))
        while w <= last:
            all_weeks.append(w.isoformat())
            w += dt.timedelta(days=7)

    # ---- Labs: an incident counts once for every lab it hit
    labs = Counter()
    for i in incidents:
        labs.update(issue_labs(i, lab_of or {}))
    by_lab = labs.most_common()
    if len(by_lab) > TOP_LABS:
        rest = sum(n for _, n in by_lab[TOP_LABS - 1:])
        by_lab = by_lab[:TOP_LABS - 1] + [('Other labs', rest)]

    symptoms = Counter()
    for i in incidents:
        symptoms.update(s.label for s in i.symptoms)

    # ---- Backlog (all open issues, now)
    backlog_age = Counter()
    backlog_severity = Counter()
    backlog_owner = Counter()
    for i in open_issues:
        age_days = (now - as_utc(i.reported_at)).total_seconds() / 86400
        for label, limit in AGE_BUCKETS:
            if limit is None or age_days < limit:
                backlog_age[label] += 1
                break
        backlog_severity[i.severity] += 1
        backlog_owner[i.assignee_name or 'Unassigned'] += 1
    oldest_open = sorted(open_issues, key=lambda i: as_utc(i.reported_at))[:10]

    # ---- Recurrence: same instrument + area more than once within the window
    window = dt.timedelta(days=recurrence_days)
    groups = defaultdict(list)
    # Imported history and the catch-all area say nothing about repeats
    for i in sorted(incidents, key=lambda i: as_utc(i.reported_at)):
        if not i.imported and i.problem_area != 'other':
            groups[(i.instrument_name, i.problem_area)].append(i)
    recurrence = []
    for (inst, area), items in groups.items():
        repeats = sum(1 for a, b in zip(items, items[1:])
                      if as_utc(b.reported_at) - as_utc(a.reported_at) <= window)
        if repeats:
            causes = Counter((i.resolution or {}).get('root_cause') for i in items
                             if (i.resolution or {}).get('root_cause'))
            recurrence.append({'instrument': inst, 'problem_area': areas.get(area, area),
                               'issues': len(items), 'repeats': repeats,
                               'root_causes': dict(causes)})
    recurrence.sort(key=lambda r: (-r['repeats'], -r['issues']))

    # ---- Resolution knowledge
    resolved = [i for i in incidents if (i.resolution or {}).get('resolution_type')]
    resolution_types = Counter(i.resolution['resolution_type'] for i in resolved)
    root_causes = Counter(i.resolution.get('root_cause') or 'Unknown' for i in resolved)
    self_fixed = resolution_types.get('Fixed by my own checks', 0)

    # ---- Data coverage (missing data is reported, not hidden)
    coverage = {
        'incidents': len(incidents),
        'imported': sum(1 for i in incidents if i.imported),
        'without_response': sum(1 for i in timed if i.id not in response),
        'unresolved': sum(1 for i in incidents if i.resolved_at is None),
        'without_start_time': sum(1 for i in timed if i.occurred_at is None),
        'unknown_root_cause': root_causes.get('Unknown', 0),
    }

    return {
        'period': {'start': start, 'end': end, 'hours': round(period_hours, 1)},
        'totals': {
            'incidents': len(incidents), 'duplicates': duplicates,
            'open_now': len(open_issues),
            'response_min': summary(response.values()),
            'restoration_h': summary(restoration.values()),
            'downtime_hours': round(sum(r['downtime_hours'] for r in per_instrument), 1),
            'booked_hours': round(sum(v for v in booked_hours.values() if v), 1),
            'self_fixed_pct': round(100 * self_fixed / len(resolved), 1) if resolved else None,
            'escalated': sum(1 for i in incidents for e in i.events if e.kind == 'escalated'),
        },
        'trend': {'weeks': all_weeks,
                  'by_severity': {s: [weeks_by_severity[s].get(w, 0) for w in all_weeks]
                                  for s in severities or weeks_by_severity}},
        'by_area': Counter(areas.get(i.problem_area, i.problem_area) for i in incidents).most_common(),
        'by_severity': Counter(i.severity for i in incidents).most_common(),
        'top_symptoms': symptoms.most_common(10),
        'by_lab': by_lab,
        'per_instrument': per_instrument,
        'backlog': {'by_age': [(label, backlog_age.get(label, 0)) for label, _ in AGE_BUCKETS],
                    'by_severity': backlog_severity.most_common(),
                    'by_owner': backlog_owner.most_common(),
                    'waiting_vendor': sum(1 for i in open_issues if i.status == 'waiting_vendor'),
                    'oldest': oldest_open},
        'recurrence': recurrence[:15],
        'resolution_types': resolution_types.most_common(),
        'root_causes': root_causes.most_common(),
        'coverage': coverage,
    }


def booked_hours_by_resource(bookings, start, end):
    """ Hours booked per EMhub resource id in [start, end), clipped. Only
    real user bookings count: slots (which contain bookings), downtime,
    maintenance and repair are excluded. """
    total = defaultdict(float)
    for b in bookings:
        if b.type != 'booking':
            continue
        s, e = clip(as_utc(b.start), as_utc(b.end), start, end)
        if e > s:
            total[b.resource_id] += (e - s).total_seconds() / 3600
    return total
