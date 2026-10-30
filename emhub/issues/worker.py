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
Notification worker: delivers the outbox and escalates overdue issues.

Run it as a service next to EMhub (run a single worker process):

    EMHUB_INSTANCE=/path/to/instance emh-issues worker

Email goes through the EMhub mail settings (MAIL_SERVER, MAIL_PORT,
MAIL_USE_TLS, MAIL_DEFAULT_SENDER). Webhooks are posted as Microsoft Teams
Adaptive Cards, which is what the Teams Workflows "When a Teams webhook
request is received" trigger expects. 'accepted' only means the mail server
or webhook took the message; it does not prove delivery.
"""

import datetime as dt
import logging
import random
import smtplib
import time

from .models import as_utc, utcnow

logger = logging.getLogger('emhub.issues.worker')

MAX_ATTEMPTS = 8
SENDING_TIMEOUT = dt.timedelta(minutes=10)

SUBJECTS = {
    'created': 'New issue',
    'escalated': 'NOT ACKNOWLEDGED',
    'acknowledge': 'Acknowledged',
    'resolve': 'Resolved',
    'reopen': 'Reopened',
    'duplicate': 'Marked as duplicate',
    'comment': 'New comment',
}


class PermanentError(Exception):
    """ Delivery can never succeed (bad address, unknown webhook...). """
    pass


class RetryLater(Exception):
    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


def backoff_seconds(attempts):
    """ Exponential backoff with jitter: ~30s, 1m, 2m, ... capped at 1h. """
    return min(3600, 30 * 2 ** max(0, attempts - 1)) * random.uniform(0.5, 1.5)


class NotificationWorker:
    def __init__(self, app, send_email=None, post_webhook=None):
        self.app = app
        self.svc = app.issues
        self._send_email = send_email or self._smtp_send
        self._post_webhook = post_webhook or self._http_post
        self._mail = None

    # ------------------------- Message content -------------------------------
    def issue_url(self, issue):
        base = (self.app.config.get('ISSUES_BASE_URL') or '').rstrip('/')
        return f'{base}/main?content_id=mi_issue&issue_id={issue.id}'

    def _severity_label(self, issue):
        return self.svc.severities().get(issue.severity, {}).get('label', issue.severity)

    def _area_label(self, issue):
        areas = {a['id']: a['label'] for a in self.svc.problem_areas(include_hidden=True)}
        return areas.get(issue.problem_area, issue.problem_area)

    def build_summary(self, msg):
        """ Weekly summary email, computed when it is sent. """
        p = msg.payload or {}
        start, end = self.svc.parse_period(p.get('start'), p.get('end'))
        data = self.svc.insights(start, end, check_access=False)
        t = data['totals']

        def _num(v, unit):
            return f'{v:.0f} {unit}' if v is not None else 'n/a'

        lines = [f"Microscope Issues: week {p.get('week', '')} "
                 f"({p.get('start')} to {p.get('end')})", '',
                 f"New incidents:        {t['incidents']} (+{t['duplicates']} duplicates)",
                 f"Open now:             {t['open_now']}",
                 f"First response:       median {_num(t['response_min']['median'], 'min')}, "
                 f"90% within {_num(t['response_min']['p90'], 'min')} "
                 f"(n={t['response_min']['n']})",
                 f"Restoration:          median {_num(t['restoration_h']['median'], 'h')} "
                 f"(n={t['restoration_h']['n']})",
                 f"Unavailable time:     {t['downtime_hours']} h",
                 f"Escalations:          {t['escalated']}", '',
                 'Per instrument (incidents / booked h / unavailable h):']
        for r in data['per_instrument']:
            if r['incidents'] or r['downtime_hours'] or r['open']:
                lines.append(f"  {r['name']:20s} {r['incidents']:3d} / "
                             f"{r['booked_hours'] or 0:6.1f} / {r['downtime_hours']:6.1f}"
                             f"   ({r['open']} open)")
        oldest = data['backlog']['oldest'][:5]
        if oldest:
            lines += ['', 'Oldest open issues:']
            for i in oldest:
                age = (utcnow() - as_utc(i.reported_at)).days
                lines.append(f'  {i.ticket} {i.instrument_name}: {i.title} ({age} days)')
        base = (self.app.config.get('ISSUES_BASE_URL') or '').rstrip('/')
        lines += ['', f'Insights: {base}/main?content_id=mi_insights']
        return f"[EMhub] Microscope Issues weekly summary {p.get('week', '')}", '\n'.join(lines)

    def build_reporter_email(self, msg, issue, event):
        """ Email to the reporter (or reporters of duplicates). Reporters
        cannot open the issue pages, so the email says it all, without a
        link, internal notes or other people's details. """
        data = event.data if event else {}
        intro = {
            'created': 'Thank you, your report was saved and the facility staff '
                       'have been notified.',
            'acknowledge': 'The facility staff are looking at your report.',
            'resolve': 'Your report was resolved.',
            'reopen': 'Your report was reopened.',
            'duplicate': 'The same problem was already reported. Your report was '
                         'merged with it and you will get its updates.',
            'comment': f'{event.actor_name if event else "The staff"} replied:',
        }.get(msg.kind, f'Update: {msg.kind}')
        subject = {
            'created': f'[{issue.ticket}] We received your report: {issue.title}',
        }.get(msg.kind, f'[{issue.ticket}] {SUBJECTS.get(msg.kind, msg.kind.capitalize())}: '
                        f'{issue.title}')
        lines = [intro, '']
        if msg.kind == 'comment':
            lines += [data.get('text', ''), '']
        elif msg.kind == 'resolve':
            res = data.get('resolution') or {}
            lines += [f"Resolution:  {res.get('resolution_type', '')}"]
            if res.get('resolution_notes'):
                lines += ['', res['resolution_notes']]
            lines += ['']
        elif msg.kind == 'reopen':
            lines += [f"Reason: {data.get('reason', '')}", '']
        elif msg.kind == 'duplicate':
            lines += [f"Original report: {data.get('duplicate_of', '')}", '']
        lines += [f'Ticket:      {issue.ticket}',
                  f'Instrument:  {issue.instrument_name}',
                  f'Problem:     {self._area_label(issue)}',
                  f'Title:       {issue.title}',
                  f'Status:      {issue.status.replace("_", " ")}',
                  '', 'If you have more information, please contact the facility staff.']
        return subject, '\n'.join(lines)

    def build_email(self, msg, issue, event):
        if msg.kind == 'summary':
            return self.build_summary(msg)
        if msg.kind == 'test':
            return ('[EMhub Microscope Issues] Test message',
                    'This is a test message from the EMhub Microscope Issues module.\n'
                    'If you received it, email notifications are working.')
        if msg.channel == 'reporter':
            return self.build_reporter_email(msg, issue, event)
        title = SUBJECTS.get(msg.kind, msg.kind.capitalize())
        subject = (f'[{issue.ticket}] {title} - {self._severity_label(issue)} - '
                   f'{issue.instrument_name}: {issue.title}')
        intro = {
            'created': 'A new issue was reported.',
            'escalated': 'This issue has not been acknowledged in time.',
            'acknowledge': 'Facility staff acknowledged this issue.',
            'resolve': 'This issue was resolved.',
            'reopen': 'This issue was reopened.',
            'duplicate': 'This issue was merged into another one.',
            'comment': 'There is a new comment on this issue.',
        }.get(msg.kind, f'Update: {msg.kind}')

        lines = [intro, '',
                 f'Ticket:      {issue.ticket}',
                 f'Instrument:  {issue.instrument_name}',
                 f'Urgency:     {self._severity_label(issue)}',
                 f'Problem:     {self._area_label(issue)}',
                 f'Title:       {issue.title}',
                 f'Status:      {issue.status.replace("_", " ")}']
        if msg.kind in ('created', 'escalated'):
            lines.append(f'Reported by: {issue.reporter_name}')
            if issue.contact:
                lines.append(f'Contact:     {issue.contact}')
        data = event.data if event else {}
        if msg.kind == 'comment':
            lines += ['', f'{event.actor_name} wrote:', data.get('text', '')]
        elif msg.kind == 'resolve':
            res = data.get('resolution') or {}
            lines += ['', f"Resolution:  {res.get('resolution_type', '')}",
                      f"Root cause:  {res.get('root_cause', '')}"]
            if res.get('resolution_notes'):
                lines += ['', res['resolution_notes']]
        elif msg.kind == 'reopen':
            lines += ['', f"Reason: {data.get('reason', '')}"]
        elif msg.kind == 'duplicate':
            lines += ['', f"Follow it in {data.get('duplicate_of', '')}."]
        elif msg.kind in ('created', 'escalated') and issue.description:
            lines += ['', issue.description[:2000]]
        lines += ['', f'Open in EMhub: {self.issue_url(issue)}']
        return subject, '\n'.join(lines)

    def build_card(self, msg, issue, event):
        """ Adaptive Card wrapped as a Teams Workflows message. """
        if msg.kind == 'test':
            body = [{'type': 'TextBlock', 'weight': 'Bolder', 'size': 'Medium',
                     'text': 'EMhub Microscope Issues: test message'},
                    {'type': 'TextBlock', 'wrap': True,
                     'text': 'Webhook notifications are working.'}]
            actions = []
        else:
            color = {'stop': 'Attention', 'blocked': 'Warning'}.get(issue.severity, 'Default')
            body = [
                {'type': 'TextBlock', 'weight': 'Bolder', 'size': 'Medium', 'wrap': True,
                 'text': f'{SUBJECTS.get(msg.kind, msg.kind)}: {issue.ticket} {issue.title}'},
                {'type': 'TextBlock', 'color': color, 'weight': 'Bolder',
                 'text': f'{self._severity_label(issue)} - {issue.instrument_name}'},
                {'type': 'FactSet', 'facts': [
                    {'title': 'Problem', 'value': self._area_label(issue)},
                    {'title': 'Status', 'value': issue.status.replace('_', ' ')},
                    {'title': 'Reported by', 'value': issue.reporter_name},
                ]},
            ]
            if msg.kind == 'resolve':
                res = (event.data if event else {}).get('resolution') or {}
                body.append({'type': 'TextBlock', 'wrap': True,
                             'text': f"Resolved: {res.get('resolution_type', '')} "
                                     f"(root cause: {res.get('root_cause', '')})"})
            actions = [{'type': 'Action.OpenUrl', 'title': 'Open in EMhub',
                        'url': self.issue_url(issue)}]
        card = {'$schema': 'http://adaptivecards.io/schemas/adaptive-card.json',
                'type': 'AdaptiveCard', 'version': '1.4', 'body': body}
        if actions and actions[0]['url'].startswith('http'):
            card['actions'] = actions
        return {'type': 'message', 'attachments': [{
            'contentType': 'application/vnd.microsoft.card.adaptive',
            'contentUrl': None, 'content': card}]}

    # ------------------------- Transports ------------------------------------
    def _smtp_send(self, to, subject, body):
        import flask_mail
        if not self.app.config.get('MAIL_SERVER'):
            raise PermanentError('MAIL_SERVER is not configured in EMhub.')
        if self._mail is None:
            self._mail = flask_mail.Mail(self.app)
        message = flask_mail.Message(subject, recipients=[to], body=body,
                                     sender=self.app.config.get('MAIL_DEFAULT_SENDER'))
        try:
            with self.app.app_context():
                self._mail.send(message)
        except smtplib.SMTPRecipientsRefused as e:
            raise PermanentError(f'Recipient refused: {e}')
        except (smtplib.SMTPException, OSError) as e:
            raise RetryLater(f'{type(e).__name__}: {e}')

    def _http_post(self, url, payload):
        import requests
        try:
            r = requests.post(url, json=payload, timeout=15)
        except requests.RequestException as e:
            raise RetryLater(f'{type(e).__name__}: {e}')
        if 200 <= r.status_code < 300:
            return
        if r.status_code == 429 or r.status_code >= 500:
            retry_after = r.headers.get('Retry-After')
            raise RetryLater(f'HTTP {r.status_code}',
                             int(retry_after) if str(retry_after).isdigit() else None)
        raise PermanentError(f'HTTP {r.status_code}: {r.text[:200]}')

    # ------------------------- Processing ------------------------------------
    def _claim(self, limit=20):
        """ Take due messages and mark them as 'sending'. Messages stuck in
        'sending' (crashed worker) are retried. """
        store = self.svc.store
        now = utcnow()
        msgs = []
        for m in store.messages(status=['queued', 'retrying', 'sending']):
            due = as_utc(m.next_attempt_at)
            if m.status == 'sending':
                due += SENDING_TIMEOUT
            if due <= now:
                m.status, m.next_attempt_at = 'sending', now
                store.save_message(m)
                msgs.append(m)
            if len(msgs) >= limit:
                break
        store.commit()
        return msgs

    def deliver(self, msg):
        issue = self.svc.store.get_issue(msg.issue_id) if msg.issue_id else None
        event = None
        if issue is not None and msg.event_id:
            event = next((e for e in issue.events if e.id == msg.event_id), None)
        if msg.issue_id and issue is None:
            raise PermanentError(f'Issue {msg.issue_id} not found')
        if msg.channel in ('email', 'reporter'):
            subject, body = self.build_email(msg, issue, event)
            self._send_email(msg.destination, subject, body)
        elif msg.channel == 'webhook':
            url = self.svc.webhook_url(msg.destination)
            if url is None:
                raise PermanentError(f"Webhook '{msg.destination}' is not configured "
                                     f"on the server (ISSUES_WEBHOOKS).")
            self._post_webhook(url, self.build_card(msg, issue, event))
        else:
            raise PermanentError(f'Unknown channel {msg.channel}')

    def process_outbox(self):
        """ Deliver due messages. Returns {'accepted': n, 'retrying': n, ...}. """
        stats = {}
        for msg in self._claim():
            msg.attempts += 1
            try:
                self.deliver(msg)
                msg.status, msg.sent_at, msg.last_error = 'accepted', utcnow(), None
            except PermanentError as e:
                msg.status, msg.last_error = 'failed', str(e)[:2000]
            except Exception as e:  # network errors, throttling, bugs
                retry_after = getattr(e, 'retry_after', None)
                msg.last_error = f'{e}'[:2000] or type(e).__name__
                if msg.attempts >= MAX_ATTEMPTS:
                    msg.status = 'failed'
                else:
                    msg.status = 'retrying'
                    delay = retry_after or backoff_seconds(msg.attempts)
                    msg.next_attempt_at = utcnow() + dt.timedelta(seconds=delay)
            if msg.status != 'accepted':
                logger.warning('Notification %s to %s: %s (%s)', msg.id,
                               msg.destination, msg.status, msg.last_error)
            stats[msg.status] = stats.get(msg.status, 0) + 1
            self.svc.store.save_message(msg)
            self.svc.store.commit()
        return stats

    def run_once(self):
        with self.app.app_context():
            try:
                escalated = self.svc.escalate_overdue()
                self.svc.enqueue_weekly_summary()
                stats = self.process_outbox()
            finally:
                self.app.dm.close()
        if escalated:
            stats['escalated'] = escalated
        return stats

    def run_forever(self, interval=10):
        logger.info('Microscope Issues worker started (every %ss)', interval)
        while True:
            try:
                stats = self.run_once()
                if stats:
                    logger.info('Worker: %s', stats)
            except Exception:
                logger.exception('Worker iteration failed')
            time.sleep(interval)
