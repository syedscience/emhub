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
Business logic of the Microscope Issues module.

All reads and writes go through IssuesService, which checks the issue roles
of the current EMhub user. Pages (content.py) and the JSON API (api.py) are
thin layers on top of it.
"""

import datetime as dt
import json
import os
import re

import flask
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from .authz import (user_issue_roles, require, Forbidden,
                    REPORTER, STAFF, ADMIN)
from .models import (Instrument, Issue, IssueSymptom, IssueEvent,
                     OutboxMessage, as_utc, utcnow)

HERE = os.path.dirname(os.path.abspath(__file__))
KNOWLEDGE_FILE = os.path.join(HERE, 'knowledge', 'problem_areas.json')

# Map EMhub resource tags to an instrument type, first match wins.
# Problem areas can be restricted to some types (only_types / not_types).
TYPE_TAGS = [
    ('fib', {'fibsem', 'fib', 'aquilos', 'hydra', 'arctis', 'crossbeam'}),
    ('tem', {'krios', 'arctica', 'talos', 'glacios', 'tem', 'tundra'}),
    ('light', {'confocal', 'airyscan', 'lsm', 'light'}),
    ('prep', {'vitrobot', 'prep', 'plunge', 'leica', 'negativestain',
              'coater', 'plasma', 'hpf'}),
]

LIMITS = {'title': 200, 'description': 10000, 'tried': 5000, 'contact': 256}
CONTEXT_FIELDS = ['detector', 'software', 'session_type', 'session_status',
                  'onset', 'booking_id', 'session_id']


# Default content of the 'config:issues' form (emh-issues init-config)
DEFAULT_CONFIG = {
    # Extra EMhub roles mapped to issue roles (see authz.py)
    'roles': {'staff': [], 'manager': [], 'admin': []},
    'notifications': {
        'email': [],            # facility addresses notified of new issues
        'webhooks': [],         # [{"name": "...", "url": "..."}] e.g. Teams Workflows
        'receipt_to_reporter': True,
    },
}


class ValidationError(Exception):
    def __init__(self, errors):
        self.errors = errors
        super().__init__('; '.join(f'{k}: {v}' for k, v in errors.items()))


class NotFound(Exception):
    pass


def instrument_type_for(tags, name=''):
    words = set(re.split(r'[\s,]+', f'{tags} {name}'.lower()))
    for itype, keys in TYPE_TAGS:
        if words & keys:
            return itype
    if 'microscope' in words:
        return 'tem'
    return 'other'


def iso(value):
    value = as_utc(value)
    return value.isoformat() if value else None


class IssuesService:
    def __init__(self, app, db):
        self.app = app
        self.db = db
        self._knowledge = None

    # ------------------------- Configuration ---------------------------------
    @property
    def session(self):
        return self.db.session

    def config(self):
        """ Module settings from the 'config:issues' form (empty if missing),
        read once per request. """
        if flask.has_app_context() and 'mi_config' in flask.g:
            return flask.g.mi_config
        dm = self.app.dm
        config = {}
        if dm.get_form_by(name='config:issues') is not None:
            config = dm.get_config('issues', default={}) or {}
        if flask.has_app_context():
            flask.g.mi_config = config
        return config

    def roles(self, user=None):
        user = user if user is not None else self.app.user
        return user_issue_roles(user, self.config())

    def knowledge(self):
        if self._knowledge is None:
            with open(KNOWLEDGE_FILE) as f:
                self._knowledge = json.load(f)
        kb = dict(self._knowledge)
        # Facilities can override the problem areas in config:issues
        if areas := self.config().get('problem_areas'):
            kb['problem_areas'] = areas
        return kb

    def severities(self):
        return {s['id']: s for s in self.knowledge()['severities']}

    def problem_areas(self, instrument=None):
        areas = self.knowledge()['problem_areas']
        if instrument is None:
            return areas
        itype = instrument.instrument_type
        return [a for a in areas
                if itype not in a.get('not_types', [])
                and (not a.get('only_types') or itype in a['only_types'])]

    # ------------------------- Instruments -----------------------------------
    def sync_instruments(self):
        """ Mirror EMhub resources into the issues database.
        Resources are never deleted here; renames keep issue history.
        """
        s = self.session
        existing = {i.resource_id: i for i in s.scalars(select(Instrument))}
        now = utcnow()
        for r in self.app.dm.get_resources():
            inst = existing.get(r.id)
            if inst is None:
                inst = Instrument(resource_id=r.id, enabled=True)
                s.add(inst)
            inst.name = r.name
            inst.tags = r.tags or ''
            inst.instrument_type = instrument_type_for(r.tags, r.name)
            inst.emhub_status = r.status or 'active'
            inst.synced_at = now
        s.commit()

    def instruments(self, reportable_only=True, sync=True):
        if sync:
            self.sync_instruments()
        q = select(Instrument).order_by(Instrument.name)
        items = list(self.session.scalars(q))
        if reportable_only:
            items = [i for i in items if i.is_reportable]
        return items

    def get_instrument(self, instrument_id=None, resource_id=None):
        s = self.session
        if instrument_id is not None:
            return s.get(Instrument, int(instrument_id))
        return s.scalars(select(Instrument).where(
            Instrument.resource_id == int(resource_id))).first()

    def set_instrument_enabled(self, instrument_id, enabled, user=None):
        require(self.roles(user), ADMIN)
        inst = self.get_instrument(instrument_id=instrument_id)
        if inst is None:
            raise NotFound(f'Instrument {instrument_id} not found')
        inst.enabled = bool(enabled)
        self.session.commit()
        return inst

    # ------------------------- Issues ----------------------------------------
    def _clean_text(self, payload, key, errors, required=False):
        value = payload.get(key) or ''
        if not isinstance(value, str):
            errors[key] = 'Must be text.'
            return ''
        value = value.strip()
        if required and not value:
            errors[key] = 'This field is required.'
        elif len(value) > LIMITS[key]:
            errors[key] = f'Must be at most {LIMITS[key]} characters.'
        return value

    def _parse_occurred_at(self, value, errors):
        if not value:
            return None
        try:
            when = dt.datetime.fromisoformat(str(value))
        except ValueError:
            errors['occurred_at'] = 'Invalid date/time.'
            return None
        if when.tzinfo is None:  # browser datetime-local: facility time
            when = when.replace(tzinfo=self.app.dm.timezone)
        when = when.astimezone(dt.timezone.utc)
        if when > utcnow() + dt.timedelta(minutes=5):
            errors['occurred_at'] = 'Cannot be in the future.'
        return when

    def create_issue(self, payload, user=None):
        """ Validate and store a new issue reported by the current user.
        The issue, its 'created' event and the queued notifications are
        committed in one transaction. Retries with the same
        idempotency_key return the issue created the first time.
        """
        user = user if user is not None else self.app.user
        roles = self.roles(user)
        require(roles, REPORTER)
        s = self.session

        key = payload.get('idempotency_key')
        if key is not None:
            key = str(key)[:64]
            if previous := self._issue_by_key(key):
                return previous

        errors = {}
        inst = None
        try:
            inst = self.get_instrument(instrument_id=payload.get('instrument_id'))
        except (TypeError, ValueError):
            pass
        if inst is None or not inst.is_reportable:
            errors['instrument_id'] = 'Select an instrument that accepts reports.'

        areas = {a['id']: a for a in self.problem_areas(inst)} if inst else {}
        area = areas.get(payload.get('problem_area'))
        if inst and area is None:
            errors['problem_area'] = 'Select a problem area for this instrument.'

        severities = self.severities()
        severity = payload.get('severity')
        if severity not in severities:
            errors['severity'] = 'Select how urgent this is.'

        symptom_ids = payload.get('symptoms') or []
        if not isinstance(symptom_ids, list):
            symptom_ids = []
        symptom_ids = [sid for sid in symptom_ids if isinstance(sid, str)]
        symptoms = []
        if area:
            known = {sy['id']: sy for sy in area.get('symptoms', [])}
            for sid in dict.fromkeys(symptom_ids):
                if sid not in known:
                    errors['symptoms'] = f"Unknown symptom '{sid}'."
                    break
                symptoms.append(known[sid])

        title = self._clean_text(payload, 'title', errors, required=True)
        description = self._clean_text(payload, 'description', errors)
        tried = self._clean_text(payload, 'tried', errors)
        contact = self._clean_text(payload, 'contact', errors)
        occurred_at = self._parse_occurred_at(payload.get('occurred_at'), errors)

        context = {}
        raw_context = payload.get('context') or {}
        if isinstance(raw_context, dict):
            for k in CONTEXT_FIELDS:
                v = raw_context.get(k)
                if v not in (None, ''):
                    context[k] = str(v)[:128]

        if errors:
            raise ValidationError(errors)

        # Some problems are always urgent: raise the severity to the minimum
        # required by the area or any selected symptom.
        rank = {k: v['rank'] for k, v in severities.items()}
        minimums = [area.get('minSeverity')] + [sy.get('minSeverity') for sy in symptoms]
        bumped_from = None
        for m in minimums:
            if m in rank and rank[m] > rank[severity]:
                bumped_from = bumped_from or severity
                severity = m

        now = utcnow()
        issue = Issue(instrument_id=inst.id, instrument_name=inst.name,
                      problem_area=area['id'], title=title,
                      description=description, tried=tried,
                      severity=severity, status='new',
                      reporter_id=user.id, reporter_name=user.name,
                      reporter_email=user.email or '', contact=contact,
                      occurred_at=occurred_at, reported_at=now,
                      context=context, resolution={}, idempotency_key=key)
        issue.symptoms = [IssueSymptom(symptom_id=sy['id'], label=sy['label'])
                          for sy in symptoms]
        s.add(issue)
        try:
            s.flush()  # assign issue.id
            issue.ticket = f'MI-{now.year}-{issue.id:05d}'
            event = IssueEvent(issue_id=issue.id, at=now, actor_id=user.id,
                               actor_name=user.name, kind='created',
                               data={'severity': severity,
                                     'bumped_from': bumped_from})
            s.add(event)
            s.flush()
            self._enqueue_notifications(issue, event)
            s.commit()
        except IntegrityError:
            s.rollback()
            # A concurrent request with the same key won the race
            if key and (previous := self._issue_by_key(key)):
                return previous
            raise
        return issue

    def _issue_by_key(self, key):
        return self.session.scalars(
            select(Issue).where(Issue.idempotency_key == key)).first()

    def _enqueue_notifications(self, issue, event):
        """ Queue one outbox message per destination. Delivery is done
        later by the worker, so a mail/Teams outage never loses a report. """
        notif = self.config().get('notifications', {})
        targets = [('email', addr) for addr in notif.get('email', [])]
        targets += [('webhook', w['name']) for w in notif.get('webhooks', [])
                    if isinstance(w, dict) and w.get('name')]
        if notif.get('receipt_to_reporter', True) and issue.reporter_email:
            targets.append(('receipt', issue.reporter_email))

        for channel, dest in dict.fromkeys(targets):
            self.session.add(OutboxMessage(
                issue_id=issue.id, event_id=event.id, channel=channel,
                destination=dest,
                dedupe_key=f'{issue.id}:{event.id}:{channel}:{dest}'[:128]))

    def get_issue(self, issue_id, user=None):
        user = user if user is not None else self.app.user
        roles = self.roles(user)
        require(roles, REPORTER)
        try:
            issue = self.session.get(Issue, int(issue_id))
        except (TypeError, ValueError):
            issue = None
        if issue is None:
            raise NotFound(f'Issue {issue_id} not found')
        return issue

    def list_issues(self, user=None, status=None, instrument_id=None,
                    mine=False, limit=500):
        """ Return issues, newest first. status can be 'open', 'closed',
        a single status or None for all. """
        user = user if user is not None else self.app.user
        require(self.roles(user), REPORTER)
        q = select(Issue).order_by(Issue.reported_at.desc(), Issue.id.desc())
        if status == 'open':
            q = q.where(Issue.status.in_(Issue.OPEN_STATUSES))
        elif status == 'closed':
            q = q.where(Issue.status.not_in(Issue.OPEN_STATUSES))
        elif status:
            q = q.where(Issue.status == status)
        if instrument_id:
            q = q.where(Issue.instrument_id == int(instrument_id))
        if mine:
            q = q.where(Issue.reporter_id == user.id)
        return list(self.session.scalars(q.limit(limit)))

    def counts(self):
        """ Open issues per instrument id (for badges). """
        q = (select(Issue.instrument_id, func.count())
             .where(Issue.status.in_(Issue.OPEN_STATUSES))
             .group_by(Issue.instrument_id))
        return dict(self.session.execute(q).all())

    # ------------------------- Serialization ---------------------------------
    def can_see_private(self, issue, user=None, roles=None):
        """ Reporter contact details and internal notes are only visible
        to staff and to the reporter of the issue. """
        user = user if user is not None else self.app.user
        roles = roles if roles is not None else self.roles(user)
        return STAFF in roles or issue.reporter_id == user.id

    def issue_json(self, issue, user=None, detail=False):
        user = user if user is not None else self.app.user
        roles = self.roles(user)
        private = self.can_see_private(issue, user, roles)
        d = {
            'id': issue.id, 'ticket': issue.ticket,
            'instrument_id': issue.instrument_id,
            'instrument_name': issue.instrument_name,
            'problem_area': issue.problem_area,
            'title': issue.title, 'severity': issue.severity,
            'status': issue.status, 'instrument_state': issue.instrument_state,
            'reported_at': iso(issue.reported_at),
            'occurred_at': iso(issue.occurred_at),
            'acknowledged_at': iso(issue.acknowledged_at),
            'resolved_at': iso(issue.resolved_at),
            'closed_at': iso(issue.closed_at),
            'assignee_name': issue.assignee_name,
            'symptoms': [{'id': sy.symptom_id, 'label': sy.label}
                         for sy in issue.symptoms],
            'version': issue.version,
            'is_mine': issue.reporter_id == user.id,
        }
        if private:
            d.update(reporter_name=issue.reporter_name)
        if detail:
            d.update(description=issue.description, tried=issue.tried,
                     context=issue.context)
            if private:
                d.update(reporter_email=issue.reporter_email,
                         contact=issue.contact)
            d['events'] = [{'at': iso(e.at), 'kind': e.kind,
                            'actor_name': e.actor_name if private else '',
                            'data': e.data}
                           for e in issue.events
                           if not e.internal or STAFF in roles]
        return d


__all__ = ['IssuesService', 'ValidationError', 'NotFound', 'Forbidden',
           'instrument_type_for']
