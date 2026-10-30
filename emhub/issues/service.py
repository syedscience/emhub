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
import uuid

import flask
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from werkzeug.utils import secure_filename

from .authz import (user_issue_roles, require, Forbidden,
                    REPORTER, STAFF, MANAGER, ADMIN)
from . import insights as mi_insights
from .models import (Instrument, Issue, IssueSymptom, IssueEvent,
                     OutboxMessage, AffectedUser, Attachment,
                     DowntimeInterval, Notice, AuditEvent, as_utc, utcnow)

HERE = os.path.dirname(os.path.abspath(__file__))
KNOWLEDGE_FILE = os.path.join(HERE, 'knowledge', 'problem_areas.json')

# Map EMhub resource tags to an instrument type, first match wins.
# Problem areas can be restricted to some types (only_types / not_types).
TYPE_TAGS = [
    ('fib', {'fibsem', 'fib', 'aquilos', 'hydra', 'arctis', 'crossbeam'}),
    ('tem', {'krios', 'arctica', 'talos', 'glacios', 'tem', 'tundra'}),
    ('light', {'confocal', 'airyscan', 'lsm', 'light'}),
    ('prep', {'vitrobot', 'prep', 'plunge', 'leica', 'negativestain',
              'coater', 'plasma', 'hpf', 'preparation', 'solarus', 'primo',
              'micropatterning', 'freezer'}),
]

# action: (allowed current statuses, new status, required role)
TRANSITIONS = {
    'acknowledge': ({'new', 'reopened'}, 'acknowledged', STAFF),
    'investigate': ({'new', 'acknowledged', 'reopened', 'waiting_vendor'},
                    'investigating', STAFF),
    'wait_vendor': ({'new', 'acknowledged', 'investigating', 'reopened'},
                    'waiting_vendor', STAFF),
    'resolve': (set(Issue.OPEN_STATUSES), 'resolved', STAFF),
    'close': ({'resolved'}, 'closed', STAFF),
    'reopen': ({'resolved', 'closed'}, 'reopened', 'participant'),
    'duplicate': (set(Issue.OPEN_STATUSES), 'duplicate', STAFF),
}

MAX_FILES_PER_ISSUE = 20
EMAIL_RE = re.compile(r'^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$')

# Allowed attachments: extension -> (content type, magic bytes or None for text)
FILE_TYPES = {
    'png': ('image/png', [b'\x89PNG\r\n\x1a\n']),
    'jpg': ('image/jpeg', [b'\xff\xd8\xff']),
    'jpeg': ('image/jpeg', [b'\xff\xd8\xff']),
    'gif': ('image/gif', [b'GIF87a', b'GIF89a']),
    'pdf': ('application/pdf', [b'%PDF-']),
    'txt': ('text/plain', None),
    'log': ('text/plain', None),
    'csv': ('text/csv', None),
    'json': ('application/json', None),
}


def sniff_content_type(ext, data):
    """ Return the content type if the file content matches its extension. """
    if ext not in FILE_TYPES:
        return None
    content_type, magics = FILE_TYPES[ext]
    if magics is None:  # text files: must be valid UTF-8 without NUL bytes
        head = data[:65536]
        if b'\x00' in head:
            return None
        try:
            head.decode('utf-8')
        except UnicodeDecodeError as e:
            if e.start < len(head) - 4:  # allow a character cut at the end
                return None
        return content_type
    return content_type if any(data.startswith(m) for m in magics) else None


LIMITS = {'title': 200, 'description': 10000, 'tried': 5000, 'contact': 256}
CONTEXT_FIELDS = ['detector', 'software', 'session_type', 'session_status',
                  'onset', 'booking_id', 'session_id']


# Default content of the 'config:issues' form (emh-issues init-config)
DEFAULT_CONFIG = {
    # Extra EMhub roles mapped to issue roles (see authz.py)
    'roles': {'staff': [], 'manager': [], 'admin': []},
    'notifications': {
        'email': [],            # facility addresses notified of new issues
        'webhooks': [],         # names of server-side webhooks (ISSUES_WEBHOOKS)
        'receipt_to_reporter': True,
        'notify_reporter': True,  # email reporters about progress and staff replies
        'escalation': {'email': [], 'webhooks': []},  # when not acknowledged in time
        'routes': [],           # [{"instruments": [...], "min_severity": "blocked",
                                #   "email": [...], "webhooks": [...]}]
        'ack_minutes': {},      # per severity, defaults from problem_areas.json
        'summary_email': [],    # weekly summary (Monday morning) recipients
    },
}


class ValidationError(Exception):
    def __init__(self, errors):
        self.errors = errors
        super().__init__('; '.join(f'{k}: {v}' for k, v in errors.items()))


class NotFound(Exception):
    pass


class Conflict(Exception):
    """ The issue changed since the user loaded it (optimistic locking). """
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
        self._audit(user if user is not None else self.app.user, 'instrument_enabled',
                    {'instrument': inst.name, 'enabled': inst.enabled})
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
            issue.events.append(event)
            s.flush()
            self._notify(issue, event, user)
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

    def _find_issue(self, ref):
        """ Find an issue by id or ticket number (for duplicate links). """
        ref = str(ref or '').strip()
        if ref.isdigit():
            return self.session.get(Issue, int(ref))
        return self.session.scalars(
            select(Issue).where(Issue.ticket == ref.upper())).first()

    def list_issues(self, user=None, status=None, instrument_id=None,
                    mine=False, assigned=None, limit=500):
        """ Return issues, newest first. status can be 'open', 'closed',
        a single status or None for all. assigned can be 'me' or 'none'. """
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
        if assigned == 'me':
            q = q.where(Issue.assignee_id == user.id)
        elif assigned == 'none':
            q = q.where(Issue.assignee_id.is_(None))
        return list(self.session.scalars(q.limit(limit)))

    def counts(self):
        """ Open issues per instrument id (for badges). """
        q = (select(Issue.instrument_id, func.count())
             .where(Issue.status.in_(Issue.OPEN_STATUSES))
             .group_by(Issue.instrument_id))
        return dict(self.session.execute(q).all())

    def duplicates(self, instrument_id, problem_area=None, user=None):
        """ Open issues on the same instrument, same problem area first,
        so reporters can join an existing issue instead of a new one. """
        require(self.roles(user), REPORTER)
        q = (select(Issue)
             .where(Issue.instrument_id == int(instrument_id),
                    Issue.status.in_(Issue.OPEN_STATUSES))
             .order_by(Issue.reported_at.desc()).limit(20))
        items = list(self.session.scalars(q))
        items.sort(key=lambda i: i.problem_area != problem_area)
        return items

    # ------------------------- Workflow --------------------------------------
    def is_participant(self, issue, user=None, roles=None):
        """ Staff, the reporter and users who joined the issue. """
        user = user if user is not None else self.app.user
        roles = roles if roles is not None else self.roles(user)
        return (STAFF in roles or issue.reporter_id == user.id
                or any(a.user_id == user.id for a in issue.affected))

    def _check_version(self, issue, payload):
        version = payload.get('version')
        if version is None or str(version) != str(issue.version):
            raise Conflict()

    def _add_event(self, issue, user, kind, data=None, internal=False, at=None):
        roles = self.roles(user) if user is not None else set()
        data = dict(data or {})
        if STAFF in roles:
            data['by_staff'] = True
        event = IssueEvent(issue_id=issue.id, at=at or utcnow(),
                           actor_id=getattr(user, 'id', None),
                           actor_name=getattr(user, 'name', None) or 'EMhub',
                           kind=kind, data=data, internal=internal)
        issue.events.append(event)  # keep the loaded history up to date
        self.session.flush()
        if not internal:
            self._notify(issue, event, user)
        return event

    def transition(self, issue_id, action, payload=None, user=None):
        """ Move an issue through the workflow:
        new -> acknowledged -> investigating -> waiting_vendor -> resolved
        -> closed, plus reopen and duplicate. """
        user = user if user is not None else self.app.user
        roles = self.roles(user)
        payload = payload or {}
        issue = self.get_issue(issue_id, user)

        if action not in TRANSITIONS:
            raise ValidationError({'action': f"Unknown action '{action}'."})
        allowed_from, new_status, role = TRANSITIONS[action]
        if role == 'participant':
            if not self.is_participant(issue, user, roles):
                raise Forbidden('Only staff or people affected by this issue can do this.')
        else:
            require(roles, role)
        if issue.status not in allowed_from:
            raise ValidationError({'action': f"Cannot {action.replace('_', ' ')} "
                                             f"an issue that is {issue.status}."})
        self._check_version(issue, payload)

        now = utcnow()
        old_status = issue.status
        data = {'from': old_status, 'to': new_status}
        errors = {}

        if action == 'resolve':
            resolution = self._clean_resolution(payload, errors)
            restored_at = self._parse_time(payload.get('restored_at'), 'restored_at', errors) or now
            if errors:
                raise ValidationError(errors)
            issue.resolution = resolution
            issue.resolved_at = now
            if issue.instrument_state in ('degraded', 'unavailable'):
                self._set_instrument_state(issue, 'operational', restored_at)
            data['resolution'] = resolution
        elif action == 'reopen':
            reason = str(payload.get('reason') or '').strip()
            if not reason:
                raise ValidationError({'reason': 'Explain why the issue is reopened.'})
            data['reason'] = reason[:2000]
            data['previous_resolution'] = issue.resolution
            issue.resolution = {}
            issue.resolved_at = issue.closed_at = None
        elif action == 'close':
            issue.closed_at = now
        elif action == 'duplicate':
            target = self._find_issue(payload.get('duplicate_of'))
            if target is None or target.id == issue.id or target.status == 'duplicate':
                raise ValidationError({'duplicate_of': 'Enter the ticket of another, '
                                                      'non-duplicate issue.'})
            issue.duplicate_of_id = target.id
            issue.closed_at = now
            data['duplicate_of'] = target.ticket
            self._add_affected(target, issue.reporter_id, issue.reporter_name,
                               issue.reporter_email)
            self._add_event(target, user, 'duplicate_merged',
                            {'ticket': issue.ticket, 'reporter': issue.reporter_name},
                            internal=True)
        elif action == 'wait_vendor':
            if vendor_ref := str(payload.get('vendor_ref') or '').strip()[:128]:
                issue.resolution = dict(issue.resolution or {}, vendor_ref=vendor_ref)
                data['vendor_ref'] = vendor_ref

        # The first staff action counts as the acknowledgement
        if STAFF in roles and issue.acknowledged_at is None and action != 'reopen':
            issue.acknowledged_at = now
        if action == 'acknowledge' and issue.assignee_id is None:
            issue.assignee_id, issue.assignee_name = user.id, user.name

        issue.status = new_status
        self._add_event(issue, user, action if action != 'wait_vendor' else 'waiting_vendor', data)
        self.session.commit()
        return issue

    def _clean_resolution(self, payload, errors):
        kb = self.knowledge()
        res = {}
        rtype, cause = payload.get('resolution_type'), payload.get('root_cause')
        if rtype not in kb['resolution_types']:
            errors['resolution_type'] = 'Select how the issue was resolved.'
        if cause not in kb['root_causes']:
            errors['root_cause'] = 'Select the root cause (or Unknown).'
        notes = str(payload.get('resolution_notes') or '').strip()
        if len(notes) > 5000:
            errors['resolution_notes'] = 'Must be at most 5000 characters.'
        time_lost = payload.get('time_lost_min')
        if time_lost not in (None, ''):
            try:
                time_lost = int(time_lost)
                if not 0 <= time_lost <= 100000:
                    raise ValueError
            except (TypeError, ValueError):
                errors['time_lost_min'] = 'Enter minutes as a positive number.'
                time_lost = None
        else:
            time_lost = None
        res.update(resolution_type=rtype, root_cause=cause,
                   resolution_notes=notes, time_lost_min=time_lost,
                   data_lost=bool(payload.get('data_lost')))
        if vendor_ref := str(payload.get('vendor_ref') or '').strip()[:128]:
            res['vendor_ref'] = vendor_ref
        return res

    def _parse_time(self, value, field, errors):
        sub = {}
        when = self._parse_occurred_at(value, sub)
        if sub:
            errors[field] = sub['occurred_at']
        return when

    def update_issue(self, issue_id, payload, user=None):
        """ Staff changes: severity, assignee and instrument state. """
        user = user if user is not None else self.app.user
        roles = self.roles(user)
        require(roles, STAFF)
        issue = self.get_issue(issue_id, user)
        self._check_version(issue, payload)
        changes, errors = {}, {}
        now = utcnow()

        if 'severity' in payload and payload['severity'] != issue.severity:
            if payload['severity'] not in self.severities():
                errors['severity'] = 'Unknown severity.'
            else:
                changes['severity'] = [issue.severity, payload['severity']]
                issue.severity = payload['severity']

        if 'assignee_id' in payload:
            aid = payload['assignee_id']
            if aid in (None, '', 0):
                if issue.assignee_id is not None:
                    changes['assignee'] = [issue.assignee_name, None]
                    issue.assignee_id = issue.assignee_name = None
            else:
                staff = {u['id']: u for u in self.staff_users()}
                try:
                    aid = int(aid)
                except (TypeError, ValueError):
                    aid = None
                if aid not in staff:
                    errors['assignee_id'] = 'Select a staff member.'
                elif aid != issue.assignee_id:
                    changes['assignee'] = [issue.assignee_name, staff[aid]['name']]
                    issue.assignee_id, issue.assignee_name = aid, staff[aid]['name']

        if 'instrument_state' in payload and payload['instrument_state'] != issue.instrument_state:
            state = payload['instrument_state']
            if state not in Issue.INSTRUMENT_STATES:
                errors['instrument_state'] = 'Unknown instrument state.'
            else:
                at = self._parse_time(payload.get('state_since'), 'state_since', errors) or now
                if not errors:
                    changes['instrument_state'] = [issue.instrument_state, state]
                    self._set_instrument_state(issue, state, at)

        if errors:
            self.session.rollback()
            raise ValidationError(errors)
        if changes:
            if issue.acknowledged_at is None:
                issue.acknowledged_at = now
            self._add_event(issue, user, 'updated', {'changes': changes})
            self.session.commit()
        return issue

    def _set_instrument_state(self, issue, state, at):
        """ Track downtime intervals as the instrument state changes. """
        for d in issue.downtimes:
            if d.end is None:
                d.end = max(as_utc(at), as_utc(d.start))
        if state in ('degraded', 'unavailable'):
            issue.downtimes.append(DowntimeInterval(
                instrument_id=issue.instrument_id, state=state, start=at))
        issue.instrument_state = state

    def add_comment(self, issue_id, text, internal=False, user=None):
        user = user if user is not None else self.app.user
        roles = self.roles(user)
        issue = self.get_issue(issue_id, user)
        if internal:
            require(roles, STAFF)
        elif not self.is_participant(issue, user, roles):
            raise Forbidden('Join the issue ("I am also affected") to comment on it.')
        text = str(text or '').strip()
        if not text:
            raise ValidationError({'text': 'Write a comment.'})
        if len(text) > 5000:
            raise ValidationError({'text': 'Must be at most 5000 characters.'})
        if STAFF in roles and not internal and issue.acknowledged_at is None:
            issue.acknowledged_at = utcnow()
        event = self._add_event(issue, user, 'comment', {'text': text}, internal=bool(internal))
        self.session.commit()
        return event

    def _add_affected(self, issue, user_id, name, email):
        if user_id == issue.reporter_id or any(a.user_id == user_id for a in issue.affected):
            return False
        issue.affected.append(AffectedUser(user_id=user_id, user_name=name,
                                           user_email=email or ''))
        return True

    def join_issue(self, issue_id, user=None):
        """ 'I am also affected': follow an open issue instead of reporting
        a duplicate. """
        user = user if user is not None else self.app.user
        require(self.roles(user), REPORTER)
        issue = self.get_issue(issue_id, user)
        if not issue.is_open:
            raise ValidationError({'issue': 'This issue is no longer open.'})
        if self._add_affected(issue, user.id, user.name, user.email):
            self._add_event(issue, user, 'affected', internal=True)
            self.session.commit()
        return issue

    def staff_users(self):
        """ Active EMhub users with the staff issue role (assignees). """
        config = self.config()
        return sorted(({'id': u.id, 'name': u.name, 'email': u.email}
                       for u in self.app.dm.get_users()
                       if STAFF in user_issue_roles(_AsUser(u), config)),
                      key=lambda u: u['name'])

    # ------------------------- Attachments -----------------------------------
    def files_folder(self):
        return self.app.config.get('ISSUES_FILES') or os.path.join(
            self.app.instance_path, 'issue_files')

    def add_attachment(self, issue_id, filename, stream, internal=False, user=None):
        user = user if user is not None else self.app.user
        roles = self.roles(user)
        issue = self.get_issue(issue_id, user)
        if internal:
            require(roles, STAFF)
        elif not self.is_participant(issue, user, roles):
            raise Forbidden('Only staff or people affected by this issue can add files.')
        if len(issue.attachments) >= MAX_FILES_PER_ISSUE:
            raise ValidationError({'file': f'At most {MAX_FILES_PER_ISSUE} files per issue.'})

        max_bytes = int(self.app.config.get('ISSUES_MAX_FILE_MB', 20)) * 1024 * 1024
        data = stream.read(max_bytes + 1)
        if not data:
            raise ValidationError({'file': 'The file is empty.'})
        if len(data) > max_bytes:
            raise ValidationError({'file': f'Files must be smaller than {max_bytes // 1048576} MB.'})

        safe = secure_filename(filename or '') or 'file'
        ext = safe.rsplit('.', 1)[-1].lower() if '.' in safe else ''
        content_type = sniff_content_type(ext, data)
        if content_type is None:
            raise ValidationError({'file': 'Allowed files: images (PNG, JPEG, GIF), PDF '
                                           'and text logs (TXT, LOG, CSV, JSON).'})

        stored = uuid.uuid4().hex
        folder = os.path.join(self.files_folder(), str(issue.id))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, stored)
        with open(path, 'wb') as f:
            f.write(data)
        try:
            att = Attachment(filename=safe[:256], stored_name=stored,
                             content_type=content_type, size=len(data),
                             internal=bool(internal), uploaded_by_id=user.id,
                             uploaded_by_name=user.name)
            issue.attachments.append(att)
            self._add_event(issue, user, 'attachment', {'filename': att.filename},
                            internal=True)
            self.session.commit()
        except Exception:
            self.session.rollback()
            os.remove(path)
            raise
        return att

    def get_attachment(self, attachment_id, user=None):
        """ Return (attachment, path) if the user may download it. """
        user = user if user is not None else self.app.user
        roles = self.roles(user)
        require(roles, REPORTER)
        att = self.session.get(Attachment, int(attachment_id))
        if att is None:
            raise NotFound('File not found')
        issue = self.session.get(Issue, att.issue_id)
        if att.internal and STAFF not in roles:
            raise NotFound('File not found')
        if not self.is_participant(issue, user, roles):
            raise Forbidden('Only staff or people affected by this issue can see its files.')
        return att, os.path.join(self.files_folder(), str(issue.id), att.stored_name)

    # ------------------------- Notices ---------------------------------------
    def notices(self, instrument_id=None):
        """ Active, unexpired notices (for one instrument plus global ones). """
        now = utcnow()
        q = select(Notice).where(Notice.active.is_(True)).order_by(Notice.created_at.desc())
        items = [n for n in self.session.scalars(q)
                 if n.expires_at is None or as_utc(n.expires_at) > now]
        if instrument_id:
            items = [n for n in items if n.instrument_id in (None, int(instrument_id))]
        return items

    def create_notice(self, payload, user=None):
        user = user if user is not None else self.app.user
        require(self.roles(user), STAFF)
        errors = {}
        message = str(payload.get('message') or '').strip()
        if not message or len(message) > 1000:
            errors['message'] = 'Write a message (at most 1000 characters).'
        level = payload.get('level') or 'warning'
        if level not in Notice.LEVELS:
            errors['level'] = 'Unknown level.'
        inst_id = payload.get('instrument_id') or None
        if inst_id is not None:
            try:
                if self.get_instrument(instrument_id=inst_id) is None:
                    raise ValueError
                inst_id = int(inst_id)
            except (TypeError, ValueError):
                errors['instrument_id'] = 'Unknown instrument.'
        expires = None
        if payload.get('expires_at'):
            try:
                expires = dt.datetime.fromisoformat(str(payload['expires_at']))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=self.app.dm.timezone)
                expires = expires.astimezone(dt.timezone.utc)
            except ValueError:
                errors['expires_at'] = 'Invalid date/time.'
        if errors:
            raise ValidationError(errors)
        notice = Notice(instrument_id=inst_id, message=message, level=level,
                        created_by_name=user.name, expires_at=expires)
        self.session.add(notice)
        self._audit(user, 'notice_created', {'message': message, 'instrument_id': inst_id})
        self.session.commit()
        return notice

    def deactivate_notice(self, notice_id, user=None):
        user = user if user is not None else self.app.user
        require(self.roles(user), STAFF)
        notice = self.session.get(Notice, int(notice_id))
        if notice is None:
            raise NotFound('Notice not found')
        notice.active = False
        self._audit(user, 'notice_removed', {'id': notice.id})
        self.session.commit()
        return notice

    def notice_json(self, n):
        return {'id': n.id, 'message': n.message, 'level': n.level,
                'instrument_id': n.instrument_id,
                'instrument_name': n.instrument.name if n.instrument else None,
                'created_by_name': n.created_by_name,
                'created_at': iso(n.created_at), 'expires_at': iso(n.expires_at)}

    # ------------------------- Settings and notifications --------------------
    def _audit(self, user, action, data):
        self.session.add(AuditEvent(actor_id=getattr(user, 'id', None),
                                    actor_name=getattr(user, 'name', '') or '',
                                    action=action, data=data))

    def webhook_names(self):
        """ Webhook endpoints are secrets: their URLs are only defined on the
        server (ISSUES_WEBHOOKS in config.py or ISSUES_WEBHOOK_<NAME> env). """
        names = set((self.app.config.get('ISSUES_WEBHOOKS') or {}).keys())
        names.update(k[len('ISSUES_WEBHOOK_'):].lower().replace('_', '-')
                     for k in os.environ if k.startswith('ISSUES_WEBHOOK_'))
        return sorted(names)

    def webhook_url(self, name):
        urls = self.app.config.get('ISSUES_WEBHOOKS') or {}
        url = urls.get(name) or os.environ.get(
            'ISSUES_WEBHOOK_' + name.upper().replace('-', '_'))
        if not url or not url.startswith('https://'):
            return None
        return url

    def notification_settings(self):
        notif = dict(DEFAULT_CONFIG['notifications'])
        notif.update(self.config().get('notifications') or {})
        ack = {s['id']: s['ack_minutes'] for s in self.knowledge()['severities']}
        ack.update(notif.get('ack_minutes') or {})
        notif['ack_minutes'] = ack
        return notif

    def save_settings(self, payload, user=None):
        """ Validate and store the notification settings in config:issues. """
        user = user if user is not None else self.app.user
        require(self.roles(user), ADMIN)
        errors = {}
        hooks = set(self.webhook_names())
        severities = self.severities()

        def emails(key, value):
            value = value or []
            if isinstance(value, str):
                value = re.split(r'[\s,;]+', value)
            out = [e.strip() for e in value if str(e).strip()]
            bad = [e for e in out if not EMAIL_RE.match(e)]
            if bad:
                errors[key] = f"Invalid email: {', '.join(bad)}"
            return out

        def webhooks(key, value):
            out = [w for w in (value or []) if w]
            bad = [w for w in out if w not in hooks]
            if bad:
                errors[key] = f"Unknown webhook: {', '.join(bad)}"
            return out

        notif = {
            'email': emails('email', payload.get('email')),
            'webhooks': webhooks('webhooks', payload.get('webhooks')),
            'receipt_to_reporter': bool(payload.get('receipt_to_reporter', True)),
            'notify_reporter': bool(payload.get('notify_reporter', True)),
            'escalation': {
                'email': emails('escalation_email', (payload.get('escalation') or {}).get('email')),
                'webhooks': webhooks('escalation_webhooks',
                                     (payload.get('escalation') or {}).get('webhooks')),
            },
            'routes': [],
            'ack_minutes': {},
            'summary_email': emails('summary_email', payload.get('summary_email')),
        }
        for sid, minutes in (payload.get('ack_minutes') or {}).items():
            try:
                if sid not in severities or not 0 < int(minutes) <= 10080:
                    raise ValueError
                notif['ack_minutes'][sid] = int(minutes)
            except (TypeError, ValueError):
                errors['ack_minutes'] = 'Minutes must be between 1 and 10080.'
        names = {i.name for i in self.instruments(reportable_only=False, sync=False)}
        for i, route in enumerate(payload.get('routes') or []):
            key = f'routes.{i}'
            insts = [n for n in (route.get('instruments') or []) if n]
            if any(n not in names for n in insts):
                errors[key] = 'Unknown instrument in route.'
            if route.get('min_severity', 'question') not in severities:
                errors[key] = 'Unknown severity in route.'
            notif['routes'].append({
                'instruments': insts,
                'min_severity': route.get('min_severity', 'question'),
                'email': emails(key, route.get('email')),
                'webhooks': webhooks(key, route.get('webhooks')),
            })
        if errors:
            raise ValidationError(errors)

        dm = self.app.dm
        config = dict(self.config() or DEFAULT_CONFIG)
        config['notifications'] = notif
        form = dm.get_form_by(name='config:issues')
        if form is None:
            dm.create_form(name='config:issues', definition=config)
        else:
            dm.update_form(id=form.id, definition=config)
        if flask.has_app_context():
            flask.g.pop('mi_config', None)
        self._audit(user, 'settings_saved', {'notifications': notif})
        self.session.commit()
        return notif

    def send_test(self, channel, destination, user=None):
        user = user if user is not None else self.app.user
        require(self.roles(user), ADMIN)
        destination = str(destination or '').strip()
        if channel == 'email' and not EMAIL_RE.match(destination):
            raise ValidationError({'destination': 'Enter a valid email address.'})
        if channel == 'webhook' and destination not in self.webhook_names():
            raise ValidationError({'destination': 'Unknown webhook.'})
        if channel not in ('email', 'webhook'):
            raise ValidationError({'channel': 'Unknown channel.'})
        msg = OutboxMessage(kind='test', channel=channel, destination=destination,
                            dedupe_key=f'test:{uuid.uuid4().hex}')
        self.session.add(msg)
        self._audit(user, 'test_sent', {'channel': channel, 'destination': destination})
        self.session.commit()
        return msg

    def outbox_summary(self, user=None, check_access=True):
        if check_access:
            require(self.roles(user), ADMIN)
        s = self.session
        counts = dict(s.execute(select(OutboxMessage.status, func.count())
                                .group_by(OutboxMessage.status)).all())
        recent = list(s.scalars(select(OutboxMessage)
                                .where(OutboxMessage.status != 'accepted')
                                .order_by(OutboxMessage.id.desc()).limit(50)))
        oldest = s.scalars(select(func.min(OutboxMessage.created_at))
                           .where(OutboxMessage.status.in_(['queued', 'retrying']))).first()
        return {'counts': counts, 'messages': recent, 'oldest_pending': as_utc(oldest)}

    def retry_message(self, message_id, user=None):
        user = user if user is not None else self.app.user
        require(self.roles(user), ADMIN)
        msg = self.session.get(OutboxMessage, int(message_id))
        if msg is None:
            raise NotFound('Message not found')
        if msg.status not in ('failed', 'skipped'):
            raise ValidationError({'status': 'Only failed messages can be retried.'})
        msg.status, msg.attempts, msg.next_attempt_at = 'queued', 0, utcnow()
        self._audit(user, 'message_retried', {'id': msg.id})
        self.session.commit()
        return msg

    def _notify(self, issue, event, actor=None):
        """ Queue one outbox message per recipient in the same transaction as
        the event. The worker delivers them, so a mail or Teams outage never
        blocks or loses a report. """
        notif = self.notification_settings()
        severities = self.severities()
        rank = severities.get(issue.severity, {}).get('rank', 0)
        targets = []
        kind = event.kind
        people = [issue.reporter_email] + [a.user_email for a in issue.affected]
        actor_email = getattr(actor, 'email', None)

        if kind == 'created':
            targets += [('email', e) for e in notif['email']]
            targets += [('webhook', w) for w in notif['webhooks']]
            for route in notif.get('routes') or []:
                if route.get('instruments') and issue.instrument_name not in route['instruments']:
                    continue
                if rank < severities.get(route.get('min_severity'), {}).get('rank', 0):
                    continue
                targets += [('email', e) for e in route.get('email', [])]
                targets += [('webhook', w) for w in route.get('webhooks', [])]
            if notif.get('receipt_to_reporter', True) and issue.reporter_email:
                targets.append(('receipt', issue.reporter_email))
        elif kind == 'escalated':
            esc = notif.get('escalation') or {}
            targets += [('email', e) for e in esc.get('email', [])]
            targets += [('webhook', w) for w in esc.get('webhooks', [])]
        elif kind in ('acknowledge', 'resolve', 'reopen', 'duplicate'):
            if notif.get('notify_reporter', True):
                targets += [('email', e) for e in people if e]
            if kind in ('resolve', 'reopen'):
                targets += [('webhook', w) for w in notif['webhooks']]
        elif kind == 'comment':
            if event.data.get('by_staff'):
                if notif.get('notify_reporter', True):
                    targets += [('email', e) for e in people if e]
            else:
                staff_emails = []
                if issue.assignee_id:
                    staff_emails = [u['email'] for u in self.staff_users()
                                    if u['id'] == issue.assignee_id]
                targets += [('email', e) for e in (staff_emails or notif['email'])]

        for channel, dest in dict.fromkeys(targets):
            if channel == 'email' and dest == actor_email:
                continue  # nobody needs an email about their own action
            self.session.add(OutboxMessage(
                issue_id=issue.id, event_id=event.id, kind=kind, channel=channel,
                destination=dest,
                dedupe_key=f'{issue.id}:{event.id}:{channel}:{dest}'[:128]))

    def escalate_overdue(self, now=None):
        """ Re-alert the escalation contacts about issues that nobody
        acknowledged in time. Runs in the worker, so it keeps working after
        the reporter closes the browser. Each issue is escalated once per
        report or reopen. """
        now = now or utcnow()
        notif = self.notification_settings()
        ack = notif['ack_minutes']
        esc = notif.get('escalation') or {}
        contacts = len(esc.get('email', [])) + len(esc.get('webhooks', []))
        count = 0
        q = select(Issue).where(Issue.status.in_(['new', 'reopened']))
        for issue in self.session.scalars(q):
            start, escalated = None, False
            for e in issue.events:
                if e.kind in ('created', 'reopen'):
                    start, escalated = as_utc(e.at), False
                elif e.kind == 'escalated':
                    escalated = True
            if start is None or escalated:
                continue
            deadline = start + dt.timedelta(minutes=ack.get(issue.severity, 30))
            if now >= deadline:
                self._add_event(issue, None, 'escalated',
                                {'after_minutes': ack.get(issue.severity, 30),
                                 'contacts': contacts}, at=now)
                count += 1
        self.session.commit()
        return count

    # ------------------------- Insights --------------------------------------
    def parse_period(self, start=None, end=None, days=90):
        """ Period [start, end) from 'YYYY-MM-DD' strings in facility time.
        Defaults to the last 'days' days, ending tomorrow (today included). """
        tz = self.app.dm.timezone
        errors = {}

        def _date(value, field):
            try:
                d = dt.date.fromisoformat(str(value))
            except ValueError:
                errors[field] = 'Use YYYY-MM-DD.'
                return None
            return dt.datetime.combine(d, dt.time(), tz).astimezone(dt.timezone.utc)

        today = dt.datetime.now(tz).date()
        end_dt = _date(end, 'end') + dt.timedelta(days=1) if end else \
            dt.datetime.combine(today + dt.timedelta(days=1), dt.time(), tz).astimezone(dt.timezone.utc)
        start_dt = _date(start, 'start') if start else end_dt - dt.timedelta(days=days)
        if errors:
            raise ValidationError(errors)
        if start_dt >= end_dt:
            raise ValidationError({'start': 'The start must be before the end.'})
        if end_dt - start_dt > dt.timedelta(days=3700):
            raise ValidationError({'start': 'The period can be at most 10 years.'})
        return start_dt, end_dt

    def _period_issues(self, start, end, instrument_id=None, include_imported=True):
        q = (select(Issue)
             .options(selectinload(Issue.events), selectinload(Issue.downtimes),
                      selectinload(Issue.symptoms))
             .where(Issue.reported_at >= start, Issue.reported_at < end)
             .order_by(Issue.reported_at))
        if instrument_id:
            q = q.where(Issue.instrument_id == int(instrument_id))
        if not include_imported:
            q = q.where(Issue.imported.is_(False))
        return list(self.session.scalars(q))

    def insights(self, start, end, instrument_id=None, include_imported=True,
                 user=None, now=None, check_access=True):
        """ Compute the insights for the period (managers only; the worker
        calls it with check_access=False for the weekly summary). """
        if check_access:
            user = user if user is not None else self.app.user
            require(self.roles(user), MANAGER)
        now = now or utcnow()
        issues = self._period_issues(start, end, instrument_id, include_imported)

        q = (select(Issue).options(selectinload(Issue.events))
             .where(Issue.status.in_(Issue.OPEN_STATUSES)))
        if instrument_id:
            q = q.where(Issue.instrument_id == int(instrument_id))
        if not include_imported:
            q = q.where(Issue.imported.is_(False))
        open_issues = list(self.session.scalars(q))

        instruments = self.instruments(reportable_only=False, sync=False)
        reported = {i.instrument_id for i in issues} | {i.instrument_id for i in open_issues}
        shown = {i.id: i.name for i in instruments
                 if (i.is_reportable or i.id in reported)
                 and (not instrument_id or i.id == int(instrument_id))}
        by_resource = mi_insights.booked_hours_by_resource(
            self.app.dm.get_bookings(), start, end)
        booked = {i.id: by_resource.get(i.resource_id, 0.0) for i in instruments if i.id in shown}

        return mi_insights.compute(
            issues, open_issues, shown, start, end, now, self.app.dm.timezone,
            booked_hours=booked,
            recurrence_days=int(self.config().get('recurrence_days', 30)),
            severities=list(self.severities()),
            areas={a['id']: a['label'] for a in self.problem_areas()})

    EXPORT_COLUMNS = ['ticket', 'reported_at', 'occurred_at', 'acknowledged_at',
                      'resolved_at', 'closed_at', 'instrument', 'problem_area',
                      'symptoms', 'severity', 'status', 'instrument_state', 'title',
                      'assignee', 'affected_users', 'response_min', 'restoration_h',
                      'downtime_h', 'resolution_type', 'root_cause', 'resolution_notes',
                      'time_lost_min', 'data_lost', 'vendor_ref', 'duplicate_of',
                      'imported', 'reporter', 'reporter_email']

    def export_rows(self, start, end, instrument_id=None, include_imported=True, user=None):
        """ Rows for the CSV export (managers). Reporter identity is only
        included for staff, as on the pages. """
        user = user if user is not None else self.app.user
        roles = self.roles(user)
        require(roles, MANAGER)
        areas = {a['id']: a['label'] for a in self.problem_areas()}
        tickets = dict(self.session.execute(select(Issue.id, Issue.ticket)).all())
        tz = self.app.dm.timezone
        now = utcnow()

        def _t(value):
            value = as_utc(value)
            return value.astimezone(tz).strftime('%Y-%m-%d %H:%M') if value else ''

        rows = []
        for i in self._period_issues(start, end, instrument_id, include_imported):
            res = i.resolution or {}
            down = mi_insights.hours(mi_insights.merge_intervals(
                [(as_utc(d.start), as_utc(d.end) if d.end else now)
                 for d in i.downtimes if d.state == 'unavailable']))
            response = restoration = ''
            if i.acknowledged_at and not i.imported:
                response = round((as_utc(i.acknowledged_at) - as_utc(i.reported_at)).total_seconds() / 60, 1)
            if i.resolved_at and not i.imported:
                ends = [as_utc(d.end) for d in i.downtimes if d.end]
                restored = max(ends) if ends else as_utc(i.resolved_at)
                restoration = round(max(0, (restored - as_utc(i.occurred_at or i.reported_at))
                                        .total_seconds() / 3600), 2)
            row = {
                'ticket': i.ticket, 'reported_at': _t(i.reported_at),
                'occurred_at': _t(i.occurred_at), 'acknowledged_at': _t(i.acknowledged_at),
                'resolved_at': _t(i.resolved_at), 'closed_at': _t(i.closed_at),
                'instrument': i.instrument_name,
                'problem_area': areas.get(i.problem_area, i.problem_area),
                'symptoms': '; '.join(s.label for s in i.symptoms),
                'severity': i.severity, 'status': i.status,
                'instrument_state': i.instrument_state, 'title': i.title,
                'assignee': i.assignee_name or '',
                'affected_users': len(i.affected),
                'response_min': response, 'restoration_h': restoration,
                'downtime_h': round(down, 2) if down else '',
                'resolution_type': res.get('resolution_type', ''),
                'root_cause': res.get('root_cause', ''),
                'resolution_notes': res.get('resolution_notes', ''),
                'time_lost_min': res.get('time_lost_min') if res.get('time_lost_min') is not None else '',
                'data_lost': 'yes' if res.get('data_lost') else '',
                'vendor_ref': res.get('vendor_ref', ''),
                'duplicate_of': tickets.get(i.duplicate_of_id, ''),
                'imported': 'yes' if i.imported else '',
                'reporter': i.reporter_name if STAFF in roles else '',
                'reporter_email': i.reporter_email if STAFF in roles else '',
            }
            rows.append(row)
        return rows

    def enqueue_weekly_summary(self, now=None):
        """ Queue the weekly summary for the previous Monday-Sunday once
        Monday 07:00 facility time has passed. Safe to call repeatedly: the
        dedupe key makes it once per week and recipient. """
        recipients = self.notification_settings().get('summary_email') or []
        if not recipients:
            return 0
        tz = self.app.dm.timezone
        now_local = (now or utcnow()).astimezone(tz)
        monday = now_local.date() - dt.timedelta(days=now_local.weekday())
        if now_local < dt.datetime.combine(monday, dt.time(7), tz):
            monday -= dt.timedelta(days=7)
        start = dt.datetime.combine(monday - dt.timedelta(days=7), dt.time(), tz)
        week = start.strftime('%G-W%V')
        count = 0
        for dest in recipients:
            key = f'summary:{week}:{dest}'[:128]
            exists = self.session.scalars(select(OutboxMessage.id).where(
                OutboxMessage.dedupe_key == key)).first()
            if exists:
                continue
            self.session.add(OutboxMessage(
                kind='summary', channel='email', destination=dest, dedupe_key=key,
                payload={'start': start.date().isoformat(),
                         'end': (start.date() + dt.timedelta(days=6)).isoformat(),
                         'week': week}))
            try:
                self.session.commit()
                count += 1
            except IntegrityError:  # another worker queued it first
                self.session.rollback()
        return count

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
        participant = self.is_participant(issue, user, roles)
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
            'assignee_id': issue.assignee_id,
            'assignee_name': issue.assignee_name,
            'symptoms': [{'id': sy.symptom_id, 'label': sy.label}
                         for sy in issue.symptoms],
            'affected_count': len(issue.affected),
            'version': issue.version,
            'is_mine': issue.reporter_id == user.id,
        }
        if private:
            d.update(reporter_name=issue.reporter_name)
        if detail:
            dup = self.session.get(Issue, issue.duplicate_of_id) if issue.duplicate_of_id else None
            d.update(description=issue.description, tried=issue.tried,
                     context=issue.context, resolution=issue.resolution,
                     duplicate_of=dup.ticket if dup else None,
                     duplicate_of_id=dup.id if dup else None,
                     is_participant=participant,
                     is_affected=any(a.user_id == user.id for a in issue.affected),
                     is_staff=STAFF in roles)
            if private:
                d.update(reporter_email=issue.reporter_email,
                         contact=issue.contact,
                         affected=[a.user_name for a in issue.affected])
            if STAFF in roles:
                d['downtimes'] = [{'state': x.state, 'start': iso(x.start),
                                   'end': iso(x.end)} for x in issue.downtimes]
            d['attachments'] = [] if not participant else [
                {'id': a.id, 'filename': a.filename, 'size': a.size,
                 'content_type': a.content_type, 'internal': a.internal,
                 'uploaded_by_name': a.uploaded_by_name,
                 'uploaded_at': iso(a.uploaded_at)}
                for a in issue.attachments if not a.internal or STAFF in roles]
            d['events'] = [{'at': iso(e.at), 'kind': e.kind, 'internal': e.internal,
                            'actor_name': e.actor_name
                            if private or e.data.get('by_staff') or e.actor_id is None else '',
                            'data': {k: v for k, v in e.data.items() if k != 'by_staff'}}
                           for e in issue.events
                           if not e.internal or STAFF in roles]
        return d


class _AsUser:
    """ Present an EMhub user row as an authenticated user for authz. """
    is_authenticated = True

    def __init__(self, user):
        self._u = user

    def __getattr__(self, name):
        return getattr(self._u, name)


__all__ = ['IssuesService', 'ValidationError', 'NotFound', 'Forbidden',
           'Conflict', 'instrument_type_for']
