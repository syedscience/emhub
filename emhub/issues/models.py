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
Database models for the Microscope Issues module.

These tables live in their own database (see ISSUES_DB_URL), separate from
the EMhub SQLite database. EMhub users and resources are referenced by their
stable EMhub ids, with a snapshot of names so history survives renames.
All timestamps are stored in UTC.
"""

import datetime as dt

from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Integer,
                        JSON, String, Text, UniqueConstraint, Index)
from sqlalchemy.orm import declarative_base, relationship


Base = declarative_base()


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


# Timezone-aware datetime column type. SQLite returns naive values,
# so use as_utc() when reading them back.
UtcDateTime = DateTime(timezone=True)


def as_utc(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


class Instrument(Base):
    """ An EMhub resource that issues can be reported against. """
    __tablename__ = 'mi_instruments'

    id = Column(Integer, primary_key=True)
    resource_id = Column(Integer, nullable=False, unique=True)  # EMhub resources.id
    name = Column(String(64), nullable=False)
    tags = Column(String(256), nullable=False, default='')
    instrument_type = Column(String(16), nullable=False, default='other')
    emhub_status = Column(String(32), nullable=False, default='active')
    # Admins can disable reporting for a resource without touching EMhub
    enabled = Column(Boolean, nullable=False, default=True)
    synced_at = Column(UtcDateTime, nullable=False, default=utcnow)

    @property
    def is_reportable(self):
        return self.enabled and self.emhub_status == 'active'

    def json(self):
        return {'id': self.id, 'resource_id': self.resource_id,
                'name': self.name, 'instrument_type': self.instrument_type,
                'emhub_status': self.emhub_status, 'enabled': self.enabled}


class Issue(Base):
    __tablename__ = 'mi_issues'

    STATUSES = ['new', 'acknowledged', 'investigating', 'waiting_vendor',
                'resolved', 'closed', 'reopened', 'duplicate']
    OPEN_STATUSES = ['new', 'acknowledged', 'investigating',
                     'waiting_vendor', 'reopened']
    INSTRUMENT_STATES = ['operational', 'degraded', 'unavailable', 'unknown']

    id = Column(Integer, primary_key=True)
    ticket = Column(String(32), unique=True)
    instrument_id = Column(Integer, ForeignKey('mi_instruments.id'), nullable=False)
    instrument_name = Column(String(64), nullable=False)  # snapshot at report time
    problem_area = Column(String(64), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=False, default='')
    tried = Column(Text, nullable=False, default='')
    severity = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False, default='new')
    instrument_state = Column(String(16), nullable=False, default='unknown')

    reporter_id = Column(Integer, nullable=False)  # EMhub users.id
    reporter_name = Column(String(256), nullable=False)
    reporter_email = Column(String(128), nullable=False, default='')
    contact = Column(String(256), nullable=False, default='')

    occurred_at = Column(UtcDateTime)  # when the problem started (reporter)
    reported_at = Column(UtcDateTime, nullable=False, default=utcnow)  # server time
    acknowledged_at = Column(UtcDateTime)
    resolved_at = Column(UtcDateTime)
    closed_at = Column(UtcDateTime)
    updated_at = Column(UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow)

    assignee_id = Column(Integer)
    assignee_name = Column(String(256))

    # Session context: detector, software, session_type, session_status,
    # onset, booking_id, session_id
    context = Column(JSON, nullable=False, default=dict)
    # Staff closure: resolution_type, root_cause, resolution_notes,
    # data_lost, time_lost_min, vendor_ref
    resolution = Column(JSON, nullable=False, default=dict)

    duplicate_of_id = Column(Integer, ForeignKey('mi_issues.id'))
    imported = Column(Boolean, nullable=False, default=False)
    legacy_source = Column(String(64))
    legacy_id = Column(String(64))
    idempotency_key = Column(String(64), unique=True)
    version = Column(Integer, nullable=False, default=1)

    instrument = relationship('Instrument')
    symptoms = relationship('IssueSymptom', cascade='all, delete-orphan',
                            order_by='IssueSymptom.id')
    events = relationship('IssueEvent', cascade='all, delete-orphan',
                          order_by='IssueEvent.id')
    affected = relationship('AffectedUser', cascade='all, delete-orphan',
                            order_by='AffectedUser.id')
    attachments = relationship('Attachment', cascade='all, delete-orphan',
                               order_by='Attachment.id')
    downtimes = relationship('DowntimeInterval', cascade='all, delete-orphan',
                             order_by='DowntimeInterval.id')

    __mapper_args__ = {'version_id_col': version}
    __table_args__ = (
        Index('ix_mi_issues_instrument_status', 'instrument_id', 'status'),
        Index('ix_mi_issues_reported_at', 'reported_at'),
        Index('ix_mi_issues_reporter', 'reporter_id'),
        UniqueConstraint('legacy_source', 'legacy_id', name='uq_mi_issues_legacy'),
    )

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES


class IssueSymptom(Base):
    __tablename__ = 'mi_issue_symptoms'

    id = Column(Integer, primary_key=True)
    issue_id = Column(Integer, ForeignKey('mi_issues.id', ondelete='CASCADE'),
                      nullable=False, index=True)
    symptom_id = Column(String(64), nullable=False)
    label = Column(String(256), nullable=False)  # snapshot of the label

    __table_args__ = (UniqueConstraint('issue_id', 'symptom_id'),)


class IssueEvent(Base):
    """ Append-only history of everything that happens to an issue. """
    __tablename__ = 'mi_issue_events'

    id = Column(Integer, primary_key=True)
    issue_id = Column(Integer, ForeignKey('mi_issues.id', ondelete='CASCADE'),
                      nullable=False, index=True)
    at = Column(UtcDateTime, nullable=False, default=utcnow)
    actor_id = Column(Integer)
    actor_name = Column(String(256), nullable=False, default='')
    kind = Column(String(32), nullable=False)  # created, status, comment, ...
    data = Column(JSON, nullable=False, default=dict)
    internal = Column(Boolean, nullable=False, default=False)  # staff-only


class OutboxMessage(Base):
    """ Notification queued in the same transaction as the event that
    triggered it; delivered by the worker (emh-issues worker). """
    __tablename__ = 'mi_outbox'

    STATUSES = ['queued', 'sending', 'accepted', 'retrying', 'failed', 'skipped']

    id = Column(Integer, primary_key=True)
    # Empty for test messages sent from the settings page
    issue_id = Column(Integer, ForeignKey('mi_issues.id', ondelete='CASCADE'),
                      index=True)
    event_id = Column(Integer, ForeignKey('mi_issue_events.id', ondelete='CASCADE'))
    kind = Column(String(32), nullable=False, default='created')  # event kind, 'test', 'summary'
    payload = Column(JSON)  # extra data, e.g. the period of a weekly summary
    channel = Column(String(16), nullable=False)  # email | receipt | webhook
    destination = Column(String(512), nullable=False)
    dedupe_key = Column(String(128), nullable=False, unique=True)
    status = Column(String(16), nullable=False, default='queued')
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(UtcDateTime, nullable=False, default=utcnow)
    last_error = Column(Text)
    created_at = Column(UtcDateTime, nullable=False, default=utcnow)
    sent_at = Column(UtcDateTime)

    __table_args__ = (Index('ix_mi_outbox_status_next', 'status', 'next_attempt_at'),)


class AffectedUser(Base):
    """ Other users hit by the same problem ("I'm also affected"). """
    __tablename__ = 'mi_affected_users'

    id = Column(Integer, primary_key=True)
    issue_id = Column(Integer, ForeignKey('mi_issues.id', ondelete='CASCADE'),
                      nullable=False, index=True)
    user_id = Column(Integer, nullable=False)
    user_name = Column(String(256), nullable=False)
    user_email = Column(String(128), nullable=False, default='')
    joined_at = Column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint('issue_id', 'user_id'),)


class Attachment(Base):
    """ File attached to an issue. The file itself is stored on disk
    (ISSUES_FILES folder) under a random name; only metadata is here. """
    __tablename__ = 'mi_attachments'

    id = Column(Integer, primary_key=True)
    issue_id = Column(Integer, ForeignKey('mi_issues.id', ondelete='CASCADE'),
                      nullable=False, index=True)
    filename = Column(String(256), nullable=False)  # sanitized original name
    stored_name = Column(String(64), nullable=False, unique=True)
    content_type = Column(String(128), nullable=False)
    size = Column(Integer, nullable=False)
    internal = Column(Boolean, nullable=False, default=False)  # staff-only
    uploaded_by_id = Column(Integer, nullable=False)
    uploaded_by_name = Column(String(256), nullable=False)
    uploaded_at = Column(UtcDateTime, nullable=False, default=utcnow)


class DowntimeInterval(Base):
    """ Period when an instrument was degraded or unavailable because of an
    issue. 'end' is empty while it lasts. Kept separate from the ticket
    status so restoration time is measured independently of closure. """
    __tablename__ = 'mi_downtime'

    id = Column(Integer, primary_key=True)
    issue_id = Column(Integer, ForeignKey('mi_issues.id', ondelete='CASCADE'),
                      nullable=False, index=True)
    instrument_id = Column(Integer, ForeignKey('mi_instruments.id'), nullable=False)
    state = Column(String(16), nullable=False)  # degraded | unavailable
    start = Column(UtcDateTime, nullable=False)
    end = Column(UtcDateTime)

    __table_args__ = (Index('ix_mi_downtime_instrument', 'instrument_id', 'start'),)


class Notice(Base):
    """ Service notice shown to users, e.g. 'Krios01 down until Friday'. """
    __tablename__ = 'mi_notices'

    LEVELS = ['info', 'warning', 'danger']

    id = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey('mi_instruments.id'))  # empty: all
    message = Column(String(1000), nullable=False)
    level = Column(String(16), nullable=False, default='warning')
    active = Column(Boolean, nullable=False, default=True)
    created_by_name = Column(String(256), nullable=False)
    created_at = Column(UtcDateTime, nullable=False, default=utcnow)
    expires_at = Column(UtcDateTime)

    instrument = relationship('Instrument')


class AuditEvent(Base):
    """ Changes to module settings (routing, instruments, notices). """
    __tablename__ = 'mi_audit'

    id = Column(Integer, primary_key=True)
    at = Column(UtcDateTime, nullable=False, default=utcnow)
    actor_id = Column(Integer)
    actor_name = Column(String(256), nullable=False, default='')
    action = Column(String(64), nullable=False)
    data = Column(JSON, nullable=False, default=dict)
