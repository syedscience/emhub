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
thin layers on top of it; storage is in store.py (EMhub entries).
"""

import datetime as dt
import json
import os
import re
import uuid

import flask
from werkzeug.utils import secure_filename

from .authz import user_issue_roles, require, Forbidden, REPORTER, MANAGER, ADMIN
from . import insights as mi_insights
from .models import (Instrument, Issue, IssueSymptom, OutboxMessage,
                     AffectedUser, Attachment, DowntimeInterval, Notice,
                     AuditEvent, as_utc, utcnow)
from .store import Conflict

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

# action: (allowed current statuses, new status); managers only
TRANSITIONS = {
    'acknowledge': ({'new', 'reopened'}, 'acknowledged'),
    'investigate': ({'new', 'acknowledged', 'reopened', 'waiting_vendor'},
                    'investigating'),
    'wait_vendor': ({'new', 'acknowledged', 'investigating', 'reopened'},
                    'waiting_vendor'),
    'resolve': (set(Issue.OPEN_STATUSES), 'resolved'),
    'close': ({'resolved'}, 'closed'),
    'reopen': ({'resolved', 'closed'}, 'reopened'),
    'duplicate': (set(Issue.OPEN_STATUSES), 'duplicate'),
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
    # EMhub resource ids that do not accept reports
    'disabled_instruments': [],
    'notifications': {
        'email': [],            # facility addresses notified of new issues
        'webhooks': [],         # names of server-side webhooks (ISSUES_WEBHOOKS)
        'receipt_to_reporter': True,
        'notify_reporter': True,  # email reporters about progress and replies
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
    def __init__(self, app, store):
        self.app = app
        self.store = store
        self._knowledge = None

    # ------------------------- Configuration ---------------------------------
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

    def _save_config(self, config):
        dm = self.app.dm
        form = dm.get_form_by(name='config:issues')
        if form is None:
            dm.create_form(name='config:issues', definition=config)
        else:
            dm.update_form(id=form.id, definition=config)
        if flask.has_app_context():
            flask.g.pop('mi_config', None)

    def roles(self, user=None):
        user = user if user is not None else self.app.user
        return user_issue_roles(user)

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
    def _instrument(self, resource):
        disabled = set(self.config().get('disabled_instruments') or [])
        return Instrument(resource, instrument_type_for(resource.tags, resource.name),
                          enabled=resource.id not in disabled)

    def instruments(self, reportable_only=True):
        """ EMhub resources as instruments, by name. """
        items = sorted((self._instrument(r) for r in self.app.dm.get_resources()),
                       key=lambda i: i.name)
        if reportable_only:
            items = [i for i in items if i.is_reportable]
        return items

    def get_instrument(self, instrument_id):
        try:
            resource = self.app.dm.get_resource_by(id=int(instrument_id))
        except (TypeError, ValueError):
            return None
        return self._instrument(resource) if resource else None

    def set_instrument_enabled(self, instrument_id, enabled, user=None):
        user = user if user is not None else self.app.user
        require(self.roles(user), ADMIN)
        inst = self.get_instrument(instrument_id)
        if inst is None:
            raise NotFound(f'Instrument {instrument_id} not found')
        config = dict(self.config() or DEFAULT_CONFIG)
        disabled = set(config.get('disabled_instruments') or [])
        if enabled:
            disabled.discard(inst.id)
        else:
            disabled.add(inst.id)
        config['disabled_instruments'] = sorted(disabled)
        self._save_config(config)
        self._audit(user, 'instrument_enabled', {'instrument': inst.name,
                                                 'enabled': bool(enabled)})
        self.store.commit()
        return self.get_instrument(inst.id)

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

        key = payload.get('idempotency_key')
        if key is not None:
            key = str(key)[:64]
            previous = self.store.find_issue(idempotency_key=key)
            if previous is not None and previous.reporter_id == user.id:
                return previous

        errors = {}
        inst = self.get_instrument(payload.get('instrument_id'))
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
                      context=context, idempotency_key=key,
                      symptoms=[IssueSymptom(symptom_id=sy['id'], label=sy['label'])
                                for sy in symptoms])
        self.store.new_issue(issue, user)
        self._add_event(issue, user, 'created',
                        {'severity': severity, 'bumped_from': bumped_from}, at=now)
        return self._save(issue, user)

    def _save(self, issue, user=None, commit=True):
        """ Save the issue with the notifications queued by _add_event. """
        messages = issue.__dict__.pop('_outbox', [])
        try:
            return self.store.save_issue(issue, user, messages, commit=commit)
        except Exception:
            self.store.rollback()
            raise

    def _issue(self, issue_id):
        issue = self.store.get_issue(issue_id)
        if issue is None:
            raise NotFound(f'Issue {issue_id} not found')
        return issue

    def get_issue(self, issue_id, user=None):
        """ Issues and their history are for managers only. """
        require(self.roles(user), MANAGER)
        return self._issue(issue_id)

    def _find_issue(self, ref):
        """ Find an issue by id or ticket number (for duplicate links). """
        ref = str(ref or '').strip()
        if ref.isdigit():
            return self.store.get_issue(int(ref))
        return self.store.find_issue(ticket=ref.upper())

    def list_issues(self, user=None, status=None, instrument_id=None,
                    mine=False, assigned=None, limit=500):
        """ Return issues, newest first. status can be 'open', 'closed',
        a single status or None for all. assigned can be 'me' or 'none'. """
        user = user if user is not None else self.app.user
        require(self.roles(user), MANAGER)
        if status == 'open':
            items = self.store.issues(status=Issue.OPEN_STATUSES)
        elif status == 'closed':
            items = [i for i in self.store.issues() if not i.is_open]
        elif status:
            items = self.store.issues(status=status)
        else:
            items = self.store.issues()
        if instrument_id:
            items = [i for i in items if i.instrument_id == int(instrument_id)]
        if mine:
            items = [i for i in items if i.reporter_id == user.id]
        if assigned == 'me':
            items = [i for i in items if i.assignee_id == user.id]
        elif assigned == 'none':
            items = [i for i in items if i.assignee_id is None]
        items.sort(key=lambda i: (as_utc(i.reported_at), i.id), reverse=True)
        return items[:limit]

    def counts(self):
        """ Open issues per instrument id (for badges). """
        counts = {}
        for i in self.store.issues(status=Issue.OPEN_STATUSES):
            counts[i.instrument_id] = counts.get(i.instrument_id, 0) + 1
        return counts

    # ------------------------- Workflow --------------------------------------
    def _check_version(self, issue, payload):
        version = payload.get('version')
        if version is None or str(version) != str(issue.version):
            raise Conflict()

    def _add_event(self, issue, user, kind, data=None, internal=False, at=None):
        """ Add an event to the issue history and queue its notifications
        (saved by _save, in the same transaction). """
        roles = self.roles(user) if user is not None else set()
        data = dict(data or {})
        if MANAGER in roles:
            data['by_manager'] = True
        event = issue.add_event(at=at or utcnow(), actor_id=getattr(user, 'id', None),
                                actor_name=getattr(user, 'name', None) or 'EMhub',
                                kind=kind, data=data, internal=internal)
        if not internal:
            issue.__dict__.setdefault('_outbox', []).extend(
                self._notify(issue, event, user))
        return event

    def transition(self, issue_id, action, payload=None, user=None):
        """ Move an issue through the workflow:
        new -> acknowledged -> investigating -> waiting_vendor -> resolved
        -> closed, plus reopen and duplicate. """
        user = user if user is not None else self.app.user
        payload = payload or {}
        issue = self.get_issue(issue_id, user)

        if action not in TRANSITIONS:
            raise ValidationError({'action': f"Unknown action '{action}'."})
        allowed_from, new_status = TRANSITIONS[action]
        if issue.status not in allowed_from:
            raise ValidationError({'action': f"Cannot {action.replace('_', ' ')} "
                                             f"an issue that is {issue.status}."})
        self._check_version(issue, payload)

        now = utcnow()
        old_status = issue.status
        data = {'from': old_status, 'to': new_status}
        errors = {}
        target = None

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
            # The reporter of the duplicate follows the original issue
            self._add_affected(target, issue.reporter_id, issue.reporter_name,
                               issue.reporter_email)
            self._add_event(target, user, 'duplicate_merged',
                            {'ticket': issue.ticket, 'reporter': issue.reporter_name},
                            internal=True)
        elif action == 'wait_vendor':
            if vendor_ref := str(payload.get('vendor_ref') or '').strip()[:128]:
                issue.resolution = dict(issue.resolution or {}, vendor_ref=vendor_ref)
                data['vendor_ref'] = vendor_ref

        # The first manager action counts as the acknowledgement
        if issue.acknowledged_at is None and action != 'reopen':
            issue.acknowledged_at = now
        if action == 'acknowledge' and issue.assignee_id is None:
            issue.assignee_id, issue.assignee_name = user.id, user.name

        issue.status = new_status
        self._add_event(issue, user, action if action != 'wait_vendor' else 'waiting_vendor', data)
        if target is not None:
            self._save(target, user, commit=False)
        return self._save(issue, user)

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
        """ Manager changes: severity, assignee and instrument state. """
        user = user if user is not None else self.app.user
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
                managers = {u['id']: u for u in self.manager_users()}
                try:
                    aid = int(aid)
                except (TypeError, ValueError):
                    aid = None
                if aid not in managers:
                    errors['assignee_id'] = 'Select an EMhub manager.'
                elif aid != issue.assignee_id:
                    changes['assignee'] = [issue.assignee_name, managers[aid]['name']]
                    issue.assignee_id, issue.assignee_name = aid, managers[aid]['name']

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
            raise ValidationError(errors)
        if changes:
            if issue.acknowledged_at is None:
                issue.acknowledged_at = now
            self._add_event(issue, user, 'updated', {'changes': changes})
            self._save(issue, user)
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
        """ A reply (emailed to the reporter) or an internal note. """
        user = user if user is not None else self.app.user
        issue = self.get_issue(issue_id, user)
        text = str(text or '').strip()
        if not text:
            raise ValidationError({'text': 'Write a comment.'})
        if len(text) > 5000:
            raise ValidationError({'text': 'Must be at most 5000 characters.'})
        if not internal and issue.acknowledged_at is None:
            issue.acknowledged_at = utcnow()
        event = self._add_event(issue, user, 'comment', {'text': text}, internal=bool(internal))
        self._save(issue, user)
        return event

    def _add_affected(self, issue, user_id, name, email):
        if user_id == issue.reporter_id or any(a.user_id == user_id for a in issue.affected):
            return False
        issue.affected.append(AffectedUser(user_id=user_id, user_name=name,
                                           user_email=email or ''))
        return True

    def manager_users(self):
        """ Active EMhub managers (possible assignees). """
        return sorted(({'id': u.id, 'name': u.name, 'email': u.email}
                       for u in self.app.dm.get_users()
                       if u.is_active and u.is_manager),
                      key=lambda u: u['name'])

    # ------------------------- Attachments -----------------------------------
    def files_folder(self):
        return self.app.config.get('ISSUES_FILES') or os.path.join(
            self.app.instance_path, 'issue_files')

    def add_attachment(self, issue_id, filename, stream, internal=False, user=None):
        """ Managers add files to any issue; reporters only to their own
        reports (e.g. screenshots sent with the report). """
        user = user if user is not None else self.app.user
        roles = self.roles(user)
        issue = self._issue(issue_id)
        if MANAGER not in roles:
            if issue.reporter_id != user.id:
                raise Forbidden('You can only add files to your own reports.')
            if internal:
                raise Forbidden('Only managers can add internal files.')
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

        att = Attachment(id=uuid.uuid4().hex, filename=safe[:256],
                         content_type=content_type, size=len(data),
                         internal=bool(internal), uploaded_by_id=user.id,
                         uploaded_by_name=user.name)
        folder = os.path.join(self.files_folder(), str(issue.id))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, att.id)
        with open(path, 'wb') as f:
            f.write(data)
        try:
            issue.attachments.append(att)
            self._add_event(issue, user, 'attachment', {'filename': att.filename},
                            internal=True)
            self._save(issue, user)
        except Exception:
            os.remove(path)
            raise
        return att

    def get_attachment(self, issue_id, attachment_id, user=None):
        """ Return (attachment, path); managers only. """
        issue = self.get_issue(issue_id, user)
        att = next((a for a in issue.attachments if a.id == str(attachment_id)), None)
        if att is None:
            raise NotFound('File not found')
        return att, os.path.join(self.files_folder(), str(issue.id), att.id)

    # ------------------------- Notices ---------------------------------------
    def notices(self, instrument_id=None):
        """ Active, unexpired notices (for one instrument plus global ones),
        shown to everyone on the report form. """
        now = utcnow()
        items = [n for n in self.store.notices(active=True)
                 if n.expires_at is None or as_utc(n.expires_at) > now]
        if instrument_id:
            items = [n for n in items if n.instrument_id in (None, int(instrument_id))]
        items.sort(key=lambda n: as_utc(n.created_at), reverse=True)
        return items

    def create_notice(self, payload, user=None):
        user = user if user is not None else self.app.user
        require(self.roles(user), MANAGER)
        errors = {}
        message = str(payload.get('message') or '').strip()
        if not message or len(message) > 1000:
            errors['message'] = 'Write a message (at most 1000 characters).'
        level = payload.get('level') or 'warning'
        if level not in Notice.LEVELS:
            errors['level'] = 'Unknown level.'
        inst = None
        if payload.get('instrument_id'):
            inst = self.get_instrument(payload['instrument_id'])
            if inst is None:
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
        notice = Notice(instrument_id=inst.id if inst else None,
                        instrument_name=inst.name if inst else None,
                        message=message, level=level,
                        created_by_name=user.name, expires_at=expires)
        self.store.add_notice(notice, user)
        self._audit(user, 'notice_created', {'message': message,
                                             'instrument_id': notice.instrument_id})
        self.store.commit()
        return notice

    def deactivate_notice(self, notice_id, user=None):
        user = user if user is not None else self.app.user
        require(self.roles(user), MANAGER)
        notice = next((n for n in self.store.notices() if str(n.id) == str(notice_id)), None)
        if notice is None:
            raise NotFound('Notice not found')
        notice.active = False
        self.store.save_notice(notice)
        self._audit(user, 'notice_removed', {'id': notice.id})
        self.store.commit()
        return notice

    def notice_json(self, n):
        return {'id': n.id, 'message': n.message, 'level': n.level,
                'instrument_id': n.instrument_id,
                'instrument_name': n.instrument_name,
                'created_by_name': n.created_by_name,
                'created_at': iso(n.created_at), 'expires_at': iso(n.expires_at)}

    # ------------------------- Settings and notifications --------------------
    def _audit(self, user, action, data):
        self.store.add_audit(AuditEvent(actor_id=getattr(user, 'id', None),
                                        actor_name=getattr(user, 'name', '') or '',
                                        action=action, data=data), user)

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
        names = {i.name for i in self.instruments(reportable_only=False)}
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

        config = dict(self.config() or DEFAULT_CONFIG)
        config['notifications'] = notif
        self._save_config(config)
        self._audit(user, 'settings_saved', {'notifications': notif})
        self.store.commit()
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
        msg = self.store.add_message(OutboxMessage(
            kind='test', channel=channel, destination=destination,
            dedupe_key=f'test:{uuid.uuid4().hex}'))
        self._audit(user, 'test_sent', {'channel': channel, 'destination': destination})
        self.store.commit()
        return msg

    def outbox_summary(self, user=None, check_access=True):
        if check_access:
            require(self.roles(user), ADMIN)
        messages = self.store.messages()
        counts = {}
        for m in messages:
            counts[m.status] = counts.get(m.status, 0) + 1
        recent = [m for m in reversed(messages) if m.status != 'accepted'][:50]
        pending = [as_utc(m.created_at) for m in messages if m.status in ('queued', 'retrying')]
        return {'counts': counts, 'messages': recent,
                'oldest_pending': min(pending) if pending else None}

    def retry_message(self, message_id, user=None):
        user = user if user is not None else self.app.user
        require(self.roles(user), ADMIN)
        try:
            msg = self.store.get_message(int(message_id))
        except (TypeError, ValueError):
            msg = None
        if msg is None:
            raise NotFound('Message not found')
        if msg.status not in ('failed', 'skipped'):
            raise ValidationError({'status': 'Only failed messages can be retried.'})
        msg.status, msg.attempts, msg.next_attempt_at = 'queued', 0, utcnow()
        self.store.save_message(msg)
        self._audit(user, 'message_retried', {'id': msg.id})
        self.store.commit()
        return msg

    def _notify(self, issue, event, actor=None):
        """ Return one outbox message per recipient, saved in the same
        transaction as the event. The worker delivers them, so a mail or
        Teams outage never blocks or loses a report.

        Channels: 'email' (facility, routes, escalation), 'reporter' (the
        reporter and reporters of duplicates; these emails carry no link,
        reporters cannot open the issue pages) and 'webhook' (Teams). """
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
                targets.append(('reporter', issue.reporter_email))
        elif kind == 'escalated':
            esc = notif.get('escalation') or {}
            targets += [('email', e) for e in esc.get('email', [])]
            targets += [('webhook', w) for w in esc.get('webhooks', [])]
        elif kind in ('acknowledge', 'resolve', 'reopen', 'duplicate', 'comment'):
            if notif.get('notify_reporter', True):
                targets += [('reporter', e) for e in people if e]
            if kind in ('resolve', 'reopen'):
                targets += [('webhook', w) for w in notif['webhooks']]

        messages = []
        for channel, dest in dict.fromkeys(targets):
            # Nobody needs an email about their own action (the receipt of
            # a report is the exception)
            if channel != 'webhook' and dest == actor_email and kind != 'created':
                continue
            messages.append(OutboxMessage(
                issue_id=issue.id, event_id=event.id, kind=kind, channel=channel,
                destination=dest,
                dedupe_key=f'{issue.id}:{event.id}:{channel}:{dest}'[:128]))
        return messages

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
        for issue in self.store.issues(status=['new', 'reopened']):
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
                try:
                    self._save(issue)
                except Conflict:  # changed meanwhile, check again next time
                    continue
                count += 1
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

    def _select(self, issues, instrument_id=None, include_imported=True):
        return [i for i in issues
                if (not instrument_id or i.instrument_id == int(instrument_id))
                and (include_imported or not i.imported)]

    def _period_issues(self, start, end, instrument_id=None, include_imported=True):
        issues = [i for i in self.store.issues()
                  if start <= as_utc(i.reported_at) < end]
        issues.sort(key=lambda i: as_utc(i.reported_at))
        return self._select(issues, instrument_id, include_imported)

    def insights(self, start, end, instrument_id=None, include_imported=True,
                 user=None, now=None, check_access=True):
        """ Compute the insights for the period (managers only; the worker
        calls it with check_access=False for the weekly summary). """
        if check_access:
            require(self.roles(user), MANAGER)
        now = now or utcnow()
        issues = self._period_issues(start, end, instrument_id, include_imported)
        open_issues = self._select(self.store.issues(status=Issue.OPEN_STATUSES),
                                   instrument_id, include_imported)

        reported = {i.instrument_id for i in issues} | {i.instrument_id for i in open_issues}
        shown = {i.id: i.name for i in self.instruments(reportable_only=False)
                 if (i.is_reportable or i.id in reported)
                 and (not instrument_id or i.id == int(instrument_id))}
        by_resource = mi_insights.booked_hours_by_resource(
            self.app.dm.get_bookings(), start, end)
        booked = {iid: by_resource.get(iid, 0.0) for iid in shown}

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
        """ Rows for the CSV export (managers only). """
        require(self.roles(user), MANAGER)
        areas = {a['id']: a['label'] for a in self.problem_areas()}
        tickets = {i.id: i.ticket for i in self.store.issues()}
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
            rows.append({
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
                'reporter': i.reporter_name,
                'reporter_email': i.reporter_email,
            })
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
            if self.store.message_exists(key):
                continue
            self.store.add_message(OutboxMessage(
                kind='summary', channel='email', destination=dest, dedupe_key=key,
                payload={'start': start.date().isoformat(),
                         'end': (start.date() + dt.timedelta(days=6)).isoformat(),
                         'week': week}))
            count += 1
        self.store.commit()
        return count

    # ------------------------- Serialization ---------------------------------
    def receipt_json(self, issue):
        """ What a reporter gets back after submitting a report. """
        return {'id': issue.id, 'ticket': issue.ticket, 'title': issue.title,
                'instrument_name': issue.instrument_name,
                'severity': issue.severity, 'status': issue.status}

    def issue_json(self, issue, user=None, detail=False):
        """ Full issue, for managers (the only ones who can open issues). """
        user = user if user is not None else self.app.user
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
            'reporter_name': issue.reporter_name,
            'symptoms': [{'id': sy.symptom_id, 'label': sy.label}
                         for sy in issue.symptoms],
            'affected_count': len(issue.affected),
            'imported': issue.imported,
            'version': issue.version,
            'is_mine': issue.reporter_id == user.id,
        }
        if detail:
            dup = self.store.get_issue(issue.duplicate_of_id) if issue.duplicate_of_id else None
            d.update(description=issue.description, tried=issue.tried,
                     context=issue.context, resolution=issue.resolution,
                     duplicate_of=dup.ticket if dup else None,
                     duplicate_of_id=dup.id if dup else None,
                     reporter_email=issue.reporter_email, contact=issue.contact,
                     affected=[a.user_name for a in issue.affected],
                     downtimes=[{'state': x.state, 'start': iso(x.start),
                                 'end': iso(x.end)} for x in issue.downtimes],
                     attachments=[{'id': a.id, 'filename': a.filename, 'size': a.size,
                                   'content_type': a.content_type, 'internal': a.internal,
                                   'uploaded_by_name': a.uploaded_by_name,
                                   'uploaded_at': iso(a.uploaded_at)}
                                  for a in issue.attachments],
                     events=[{'at': iso(e.at), 'kind': e.kind, 'internal': e.internal,
                              'actor_name': e.actor_name,
                              'data': {k: v for k, v in e.data.items() if k != 'by_manager'}}
                             for e in issue.events])
        return d


__all__ = ['IssuesService', 'ValidationError', 'NotFound', 'Forbidden',
           'Conflict', 'instrument_type_for']
