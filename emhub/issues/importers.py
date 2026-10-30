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
One-time import of EMhub logbook entries as historical issues.

EMhub logbooks are Projects with status 'special:logbook' whose entries hold a
list of dated actions, each with a status (critical, high, medium, low,
solved). Each entry becomes an issue flagged 'imported':

  - the instrument is the logbook's resource (or one given with --map)
  - urgency comes from the highest unsolved action status
  - the issue is closed if the last action is 'solved'
  - actions become staff-only history notes, with their original dates
  - no notifications are sent; timing metrics ignore imported issues

Re-running is safe: entries already imported (same legacy id) are skipped.
The logbooks in EMhub are not changed.
"""

import datetime as dt

from .authz import ADMIN, require
from .models import Issue, as_utc, utcnow

LEGACY_SOURCE = 'emhub-logbook'
STATUS_SEVERITY = {'critical': 'stop', 'high': 'blocked', 'medium': 'degraded',
                   'low': 'question'}
SEVERITY_RANK = {'question': 0, 'degraded': 1, 'blocked': 2, 'stop': 3}


def _parse_action_date(value, tz):
    try:
        d = dt.datetime.strptime(str(value), '%Y/%m/%d')
    except (TypeError, ValueError):
        return None
    return dt.datetime.combine(d.date(), dt.time(9), tz).astimezone(dt.timezone.utc)


def import_logbooks(svc, logbook_ids, instrument_map=None, dry_run=True, user=None):
    """ Import entries of the given logbooks. instrument_map maps a logbook id
    to an EMhub resource id, for logbooks without one (or to override it).
    Returns a report: per logbook counts and skipped entries with reasons. """
    if user is not None:
        require(svc.roles(user), ADMIN)
    dm, store = svc.app.dm, svc.store
    tz = dm.timezone
    instrument_map = instrument_map or {}

    already = {i.legacy_id for i in store.issues(legacy_source=LEGACY_SOURCE)}
    logbooks = {p.id: p for p in dm.get_projects(condition='status="special:logbook"')}
    report = {'dry_run': dry_run, 'logbooks': [], 'imported': 0, 'skipped': 0}

    for lb_id in logbook_ids:
        lb = logbooks.get(lb_id)
        entry = {'logbook_id': lb_id, 'title': lb.title if lb else None,
                 'entries': 0, 'imported': 0, 'already': 0, 'skipped': []}
        report['logbooks'].append(entry)
        if lb is None:
            entry['skipped'].append({'entry': None, 'reason': 'Not a logbook'})
            continue

        resource_id = instrument_map.get(lb_id) or lb.extra.get('resource_id')
        inst = svc.get_instrument(resource_id) if resource_id else None
        for e in sorted(lb.entries, key=lambda e: e.id):
            entry['entries'] += 1
            if str(e.id) in already:
                entry['already'] += 1
                continue
            if inst is None:
                entry['skipped'].append({'entry': e.id, 'reason':
                                         'Logbook has no instrument; use --map'})
                continue

            data = (e.extra or {}).get('data', {}) or {}
            actions = [a for a in data.get('actions', []) if isinstance(a, dict)]
            statuses = [str(a.get('status', '')).lower() for a in actions]
            severities = [STATUS_SEVERITY[st] for st in statuses if st in STATUS_SEVERITY]
            severity = max(severities, key=SEVERITY_RANK.get) if severities else 'degraded'
            solved = bool(statuses) and statuses[-1] == 'solved'

            reported = as_utc(e.date or e.creation_date) or utcnow()
            action_dates = [_parse_action_date(a.get('date'), tz) for a in actions]
            closed_at = (action_dates[-1] or reported) if solved else None
            creator = e.creation_user
            context = {}
            if data.get('category'):
                context['logbook_category'] = str(data['category'])[:128]
            if data.get('tags'):
                tags = data['tags']
                context['tags'] = (', '.join(map(str, tags)) if isinstance(tags, list)
                                   else str(tags))[:128]

            issue = Issue(
                instrument_id=inst.id, instrument_name=inst.name,
                problem_area='other', title=(e.title or f'Logbook entry {e.id}')[:200],
                description=(e.description or '')[:10000], severity=severity,
                status='closed' if solved else 'acknowledged',
                reporter_id=creator.id if creator else 0,
                reporter_name=creator.name if creator else 'Unknown',
                reporter_email=(creator.email if creator else '') or '',
                reported_at=reported, resolved_at=closed_at, closed_at=closed_at,
                # No made-up resolution data: how it was fixed is in the notes
                context=context, imported=True, resolution={},
                legacy_source=LEGACY_SOURCE, legacy_id=str(e.id))
            store.new_issue(issue, creator)
            issue.add_event(
                at=reported, actor_id=creator.id if creator else None,
                actor_name=creator.name if creator else 'EMhub', kind='imported',
                internal=True,
                data={'source': LEGACY_SOURCE, 'logbook_id': lb_id, 'entry_id': e.id,
                      'images': sum(1 for a in actions if a.get('image_file'))
                      + len(data.get('images') or [])})
            for a, when in zip(actions, action_dates):
                text = f"[{a.get('status', '')}] {a.get('action', '') or ''}".strip()
                issue.add_event(
                    at=when or reported, actor_name=str(a.get('user', '') or '')[:256],
                    kind='comment', internal=True, data={'text': text[:5000]})
            # Saved without notifications: imported history never emails anyone
            store.save_issue(issue, creator, commit=False)
            entry['imported'] += 1

        report['imported'] += entry['imported']
        report['skipped'] += len(entry['skipped'])

    if dry_run:
        store.rollback()
    else:
        store.commit()
    return report
