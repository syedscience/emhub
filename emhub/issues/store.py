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
Storage of the Microscope Issues module in the EMhub database.

All records live in one hidden EMhub project with status 'special:issues'
(created on first use), the same way EMhub keeps its logbooks:

    entry type         one entry per
    microscope_issue   issue, with its history, symptoms, files, downtime
    mi_notice          service notice
    mi_message         queued notification (email, Teams)
    mi_audit           change to the module settings

The record is stored as JSON in entry.extra['data']. Entries are written
directly with the EMhub database session, so the worker can save without a
logged in user. EMhub's generic project and entry routes are blocked for
this project (see guard.py): only the module reads and writes it.
"""

from sqlalchemy import func

from .models import (Issue, OutboxMessage, Notice, AuditEvent, as_utc, utcnow)

PROJECT_STATUS = 'special:issues'
PROJECT_TITLE = 'Microscope Issues'

ISSUE_TYPE = 'microscope_issue'
NOTICE_TYPE = 'mi_notice'
MESSAGE_TYPE = 'mi_message'
AUDIT_TYPE = 'mi_audit'
ENTRY_TYPES = (ISSUE_TYPE, NOTICE_TYPE, MESSAGE_TYPE, AUDIT_TYPE)


class Conflict(Exception):
    """ The issue changed since it was loaded (optimistic locking). """
    pass


class IssueStore:
    def __init__(self, dm):
        self.dm = dm
        self._project_id = None

    @property
    def session(self):
        return self.dm._db_session

    def commit(self):
        self.session.commit()

    def rollback(self):
        self.session.rollback()
        self._project_id = None  # the project may have been created in it

    # ------------------------- Project ---------------------------------------
    def _owner_id(self):
        """ The project belongs to the first admin, so it never shows up as a
        project of a user's lab. """
        users = sorted(self.dm.get_users(), key=lambda u: u.id)
        admin = next((u for u in users if u.is_admin), users[0] if users else None)
        if admin is None:
            raise Exception('EMhub has no users yet.')
        return admin.id

    def project_id(self):
        if self._project_id is None:
            project = self.dm.get_project_by(status=PROJECT_STATUS)
            if project is None:
                now = utcnow()
                owner = self._owner_id()
                project = self.dm.Project(
                    status=PROJECT_STATUS, title=PROJECT_TITLE, date=now,
                    description='Microscope issues reported in EMhub. Managed by '
                                'the Microscope Issues module, do not edit.',
                    user_id=owner, creation_date=now, creation_user_id=owner,
                    last_update_date=now, last_update_user_id=owner,
                    extra={'user_can_edit': False})
                # Saved with the first record, in the same transaction
                self.session.add(project)
                self.session.flush()
            self._project_id = project.id
        return self._project_id

    def existing_project_id(self):
        """ The project id, or None if no issue was stored yet. """
        if self._project_id is None:
            project = self.dm.get_project_by(status=PROJECT_STATUS)
            self._project_id = project.id if project else None
        return self._project_id

    def owns_project(self, project_id):
        pid = self.existing_project_id()
        try:
            return pid is not None and int(project_id) == pid
        except (TypeError, ValueError):
            return False

    def owns_entry(self, entry_id):
        pid = self.existing_project_id()
        if pid is None:
            return False
        try:
            entry = self.dm.get_entry_by(id=int(entry_id))
        except (TypeError, ValueError):
            return False
        return entry is not None and entry.project_id == pid

    # ------------------------- Entries ---------------------------------------
    def _query(self, entry_type, **fields):
        """ Entries of a type, optionally where extra.data.<field> == value. """
        Entry = self.dm.Entry
        q = (self.session.query(Entry)
             .filter(Entry.project_id == self.project_id(), Entry.type == entry_type))
        for name, value in fields.items():
            column = func.json_extract(Entry.extra, f'$.data.{name}')
            if isinstance(value, (list, tuple, set)):
                q = q.filter(column.in_(list(value)))
            else:
                q = q.filter(column == value)
        return q.order_by(Entry.id).populate_existing()

    def _user_id(self, user):
        uid = getattr(user, 'id', None)
        if uid and self.dm.get_user_by(id=uid) is not None:
            return uid
        return self._owner_id()

    def _new_entry(self, entry_type, title, date, user=None):
        now = utcnow()
        uid = self._user_id(user)
        entry = self.dm.Entry(project_id=self.project_id(), type=entry_type,
                              title=(title or '')[:256], date=date or now,
                              creation_date=now, creation_user_id=uid,
                              last_update_date=now, last_update_user_id=uid,
                              extra={'data': {}})
        self.session.add(entry)
        self.session.flush()  # assign entry.id
        return entry

    @staticmethod
    def _write(entry, record, user_id=None):
        # A new dict, so SQLAlchemy sees the JSON column changed
        entry.extra = {'data': record.to_dict()}
        entry.last_update_date = utcnow()
        if user_id:
            entry.last_update_user_id = user_id

    @staticmethod
    def _load(cls, entry):
        record = cls.from_dict((entry.extra or {}).get('data') or {})
        record.id = entry.id
        record._entry = entry
        return record

    # ------------------------- Issues ----------------------------------------
    def issues(self, **fields):
        return [self._load(Issue, e) for e in self._query(ISSUE_TYPE, **fields)]

    def get_issue(self, issue_id):
        try:
            entry = self.dm.get_entry_by(id=int(issue_id))
        except (TypeError, ValueError):
            return None
        if entry is None or entry.type != ISSUE_TYPE or entry.project_id != self.project_id():
            return None
        self.session.refresh(entry)
        return self._load(Issue, entry)

    def find_issue(self, **fields):
        return next(iter(self.issues(**fields)), None)

    def new_issue(self, issue, user=None):
        """ Create the entry of a new issue (committed by save_issue). """
        entry = self._new_entry(ISSUE_TYPE, issue.title, issue.reported_at, user)
        issue.id, issue._entry = entry.id, entry
        if not issue.ticket:
            issue.ticket = self._next_ticket(as_utc(issue.reported_at).year)
        return issue

    def _next_ticket(self, year):
        """ Next ticket of the year: MI-2026-0001, MI-2026-0002...
        The counters are kept in the project, updated in the same transaction
        as the new issue, so a rolled back report does not use a number. Two
        reports at once cannot get the same number: the database lets only
        one transaction write at a time, the other one fails and is retried
        by the user. """
        project = self.session.get(self.dm.Project, self.project_id())
        self.session.refresh(project)
        extra = dict(project.extra or {})
        counters = dict(extra.get('tickets') or {})
        n = int(counters.get(str(year), 0))
        while True:  # skip numbers already taken (e.g. counters edited by hand)
            n += 1
            ticket = f'MI-{year}-{n:04d}'
            if not self._query(ISSUE_TYPE, ticket=ticket).first():
                break
        counters[str(year)] = n
        extra['tickets'] = counters
        project.extra = extra  # a new dict, so SQLAlchemy sees the change
        self.session.flush()
        return ticket

    def save_issue(self, issue, user=None, messages=(), commit=True):
        """ Save the issue and its queued messages in one transaction.
        Raises Conflict if someone else saved the issue since it was loaded. """
        entry = issue._entry
        stored = (entry.extra or {}).get('data') or {}
        if stored:  # not a new issue
            self.session.refresh(entry)
            stored = (entry.extra or {}).get('data') or {}
            if stored.get('version') != issue.version:
                raise Conflict()
            issue.version += 1
        issue.updated_at = utcnow()
        entry.title = issue.title[:256]
        entry.description = issue.description
        entry.date = issue.reported_at
        self._write(entry, issue, getattr(user, 'id', None) and self._user_id(user))
        for msg in messages:
            self.add_message(msg)
        if commit:
            self.commit()
        return issue

    # ------------------------- Messages --------------------------------------
    def add_message(self, msg):
        title = f'{msg.kind} {msg.channel} {msg.destination}'
        entry = self._new_entry(MESSAGE_TYPE, title, msg.created_at)
        msg.id, msg._entry = entry.id, entry
        self._write(entry, msg)
        return msg

    def save_message(self, msg):
        self._write(msg._entry, msg)

    def messages(self, **fields):
        return [self._load(OutboxMessage, e) for e in self._query(MESSAGE_TYPE, **fields)]

    def get_message(self, message_id):
        entry = self.dm.get_entry_by(id=int(message_id))
        if entry is None or entry.type != MESSAGE_TYPE or entry.project_id != self.project_id():
            return None
        return self._load(OutboxMessage, entry)

    def message_exists(self, dedupe_key):
        return self._query(MESSAGE_TYPE, dedupe_key=dedupe_key).first() is not None

    # ------------------------- Notices and audit -----------------------------
    def notices(self, **fields):
        return [self._load(Notice, e) for e in self._query(NOTICE_TYPE, **fields)]

    def add_notice(self, notice, user=None):
        entry = self._new_entry(NOTICE_TYPE, notice.message[:256], notice.created_at, user)
        notice.id, notice._entry = entry.id, entry
        self._write(entry, notice)
        return notice

    def save_notice(self, notice):
        self._write(notice._entry, notice)

    def add_audit(self, event, user=None):
        entry = self._new_entry(AUDIT_TYPE, event.action, event.at, user)
        event.id = entry.id
        self._write(entry, event)
        return event

    def audit_events(self):
        return [self._load(AuditEvent, e) for e in self._query(AUDIT_TYPE)]
