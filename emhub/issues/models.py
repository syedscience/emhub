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
Records of the Microscope Issues module.

Everything is stored in the EMhub database, as entries of one hidden
project (status 'special:issues', see store.py): each issue is an entry and
its history, symptoms, files and downtime are kept inside it. Notices,
queued notifications and the audit trail are entries of their own type.

These classes are plain Python objects; IssueStore converts them to and
from the JSON kept in entry.extra['data']. All times are UTC.
"""

import datetime as dt


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def as_utc(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def _dump_time(value):
    value = as_utc(value)
    return value.isoformat() if value else None


def _load_time(value):
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return as_utc(value)
    return as_utc(dt.datetime.fromisoformat(value))


class Record:
    """ Object with a fixed set of fields, saved as a JSON dict.
    FIELDS maps a field name to its default; TIMES are datetime fields;
    CHILDREN maps a field to the Record class of its list items. """
    FIELDS = {}
    TIMES = ()
    CHILDREN = {}

    def __init__(self, **kwargs):
        for name, default in self.FIELDS.items():
            value = kwargs.pop(name, None)
            if value is None:
                value = default() if callable(default) else default
            setattr(self, name, value)
        if kwargs:
            raise TypeError(f'Unknown fields for {type(self).__name__}: {", ".join(kwargs)}')

    def to_dict(self):
        d = {}
        for name in self.FIELDS:
            value = getattr(self, name)
            if name in self.TIMES:
                value = _dump_time(value)
            elif name in self.CHILDREN:
                value = [c.to_dict() for c in value]
            d[name] = value
        return d

    @classmethod
    def from_dict(cls, d):
        kwargs = {}
        for name in cls.FIELDS:
            if name not in d:
                continue
            value = d[name]
            if name in cls.TIMES:
                value = _load_time(value)
            elif name in cls.CHILDREN:
                value = [cls.CHILDREN[name].from_dict(c) for c in value or []]
            kwargs[name] = value
        return cls(**kwargs)


class Instrument:
    """ An EMhub resource that issues can be reported against. Its id is the
    EMhub resource id; admins can switch reporting off for it. """

    def __init__(self, resource, instrument_type, enabled=True):
        self.id = self.resource_id = resource.id
        self.name = resource.name
        self.tags = resource.tags or ''
        self.instrument_type = instrument_type
        self.emhub_status = resource.status or 'active'
        self.enabled = enabled

    @property
    def is_reportable(self):
        return self.enabled and self.emhub_status == 'active'

    def json(self):
        return {'id': self.id, 'resource_id': self.resource_id,
                'name': self.name, 'instrument_type': self.instrument_type,
                'emhub_status': self.emhub_status, 'enabled': self.enabled}


class IssueSymptom(Record):
    FIELDS = {'symptom_id': '', 'label': ''}


class IssueEvent(Record):
    """ Append-only history of everything that happens to an issue.
    'internal' events are notes that are never emailed to the reporter. """
    FIELDS = {'id': 0, 'at': utcnow, 'actor_id': None, 'actor_name': '',
              'kind': '', 'data': dict, 'internal': False}
    TIMES = ('at',)


class AffectedUser(Record):
    """ Reporters of duplicates, who follow the original issue. """
    FIELDS = {'user_id': 0, 'user_name': '', 'user_email': '', 'joined_at': utcnow}
    TIMES = ('joined_at',)


class Attachment(Record):
    """ File attached to an issue. The file is stored on disk (ISSUES_FILES
    folder) under its random id; only metadata is kept here. """
    FIELDS = {'id': '', 'filename': '', 'content_type': '', 'size': 0,
              'internal': False, 'uploaded_by_id': 0, 'uploaded_by_name': '',
              'uploaded_at': utcnow}
    TIMES = ('uploaded_at',)


class DowntimeInterval(Record):
    """ Period when the instrument was degraded or unavailable because of an
    issue. 'end' is empty while it lasts. Kept separate from the ticket
    status so restoration time is measured independently of closure. """
    FIELDS = {'state': '', 'start': None, 'end': None, 'instrument_id': 0}
    TIMES = ('start', 'end')


class Issue(Record):
    STATUSES = ['new', 'acknowledged', 'investigating', 'waiting_vendor',
                'resolved', 'closed', 'reopened', 'duplicate']
    OPEN_STATUSES = ['new', 'acknowledged', 'investigating',
                     'waiting_vendor', 'reopened']
    INSTRUMENT_STATES = ['operational', 'degraded', 'unavailable', 'unknown']

    FIELDS = {
        'ticket': None,
        'instrument_id': 0,          # EMhub resource id
        'instrument_name': '',       # snapshot at report time
        'problem_area': '', 'title': '', 'description': '', 'tried': '',
        'severity': '', 'status': 'new', 'instrument_state': 'unknown',
        'reporter_id': 0, 'reporter_name': '', 'reporter_email': '', 'contact': '',
        'occurred_at': None,         # when the problem started (reporter)
        'reported_at': utcnow,       # server time
        'acknowledged_at': None, 'resolved_at': None, 'closed_at': None,
        'updated_at': utcnow,
        'assignee_id': None, 'assignee_name': None,
        # detector, software, session_type, session_status, onset,
        # booking_id, session_id
        'context': dict,
        # resolution_type, root_cause, resolution_notes, data_lost,
        # time_lost_min, vendor_ref
        'resolution': dict,
        'duplicate_of_id': None,
        'imported': False, 'legacy_source': None, 'legacy_id': None,
        'idempotency_key': None,
        'version': 1,
        'symptoms': list, 'events': list, 'affected': list,
        'attachments': list, 'downtimes': list,
    }
    TIMES = ('occurred_at', 'reported_at', 'acknowledged_at', 'resolved_at',
             'closed_at', 'updated_at')
    CHILDREN = {'symptoms': IssueSymptom, 'events': IssueEvent,
                'affected': AffectedUser, 'attachments': Attachment,
                'downtimes': DowntimeInterval}

    id = None  # the EMhub entry id, set by the store

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    def add_event(self, **kwargs):
        event = IssueEvent(id=max((e.id for e in self.events), default=0) + 1, **kwargs)
        self.events.append(event)
        return event


class OutboxMessage(Record):
    """ Notification queued together with the change that triggered it;
    delivered by the worker (emh-issues worker). """
    STATUSES = ['queued', 'sending', 'accepted', 'retrying', 'failed', 'skipped']

    FIELDS = {
        'issue_id': None,            # empty for tests and weekly summaries
        'event_id': None,
        'kind': 'created',           # event kind, 'test', 'summary'
        'payload': None,             # extra data, e.g. the summary period
        'channel': '',               # email | reporter | webhook
        'destination': '',
        'dedupe_key': '',
        'status': 'queued', 'attempts': 0, 'next_attempt_at': utcnow,
        'last_error': None, 'created_at': utcnow, 'sent_at': None,
    }
    TIMES = ('next_attempt_at', 'created_at', 'sent_at')

    id = None


class Notice(Record):
    """ Service notice shown to users, e.g. 'Krios01 down until Friday'. """
    LEVELS = ['info', 'warning', 'danger']

    FIELDS = {'instrument_id': None, 'instrument_name': None, 'message': '',
              'level': 'warning', 'active': True, 'created_by_name': '',
              'created_at': utcnow, 'expires_at': None}
    TIMES = ('created_at', 'expires_at')

    id = None


class AuditEvent(Record):
    """ Changes to module settings (routing, instruments, notices). """
    FIELDS = {'at': utcnow, 'actor_id': None, 'actor_name': '', 'action': '',
              'data': dict}
    TIMES = ('at',)

    id = None
