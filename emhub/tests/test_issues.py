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
Tests for the Microscope Issues module (emhub/issues).

Run with:  python -m unittest emhub.tests.test_issues

Tests run on SQLite. They also run on PostgreSQL if ISSUES_TEST_PG_URL is set
or the 'pgserver' package (embedded PostgreSQL) is installed.
"""

import datetime as dt
import io
import os
import shutil
import tempfile
import unittest

from emhub.data import DataManager
from emhub.data.imports.test import TestData

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_JSON = os.path.join(HERE, '..', 'data', 'imports', 'test_instance_data.json')

_instance = None


def get_test_instance():
    """ Create (once) an EMhub instance folder with the test dataset. """
    global _instance
    if _instance is None:
        _instance = tempfile.mkdtemp(prefix='emhub-issues-test-')
        dm = DataManager(_instance, cleanDb=True)
        TestData(dm, TEST_JSON, fix_dates=True)
        dm.close()
    return _instance


def tearDownModule():
    if _instance:
        shutil.rmtree(_instance, ignore_errors=True)


def get_pg_url():
    if url := os.environ.get('ISSUES_TEST_PG_URL'):
        return url
    try:
        import pgserver
    except ImportError:
        return None
    pgdata = tempfile.mkdtemp(prefix='emhub-issues-pg-')
    server = pgserver.get_server(pgdata, cleanup_mode='delete')
    server.psql('CREATE DATABASE emhub_issues_test;')
    uri = server.get_uri('emhub_issues_test')
    return uri.replace('postgresql://', 'postgresql+psycopg://', 1)


class IssuesTestBase:
    """ Tests shared by the SQLite and PostgreSQL test cases. """
    DB_URL = None

    @classmethod
    def setUpClass(cls):
        instance = get_test_instance()
        os.environ['EMHUB_INSTANCE'] = instance
        url = cls.get_db_url(instance)
        if url is None:
            raise unittest.SkipTest('No database available')

        from emhub import create_app
        from emhub.issues.db import IssuesDb
        db = IssuesDb(url)
        db.engine.dispose()
        with db.engine.begin() as conn:  # start from an empty database
            db.metadata().drop_all(conn)
            conn.exec_driver_sql('DROP TABLE IF EXISTS mi_alembic_version')
        db.migrate()

        cls.files = tempfile.mkdtemp(prefix='emhub-issues-files-')
        cls.app = create_app({'TESTING': True, 'SECRET_KEY': 'test',
                              'EMHUB_ISSUES': True, 'ISSUES_DB_URL': url,
                              'ISSUES_FILES': cls.files,
                              'ISSUES_BASE_URL': 'https://emhub.example.org',
                              'ISSUES_WEBHOOKS': {'teams-cryoem': 'https://example.invalid/hook'}})
        cls.svc = cls.app.issues

        users = cls.app.dm.get_users()
        plain = [u for u in users if u.roles == ['user'] and u.is_active]
        cls.reporter, cls.other = plain[0], plain[1]
        cls.manager = next(u for u in users if u.is_manager and not u.is_admin)
        cls.admin = next(u for u in users if u.is_admin)

        with cls.app.app_context():
            insts = cls.svc.instruments()
            cls.krios = next(i for i in insts if i.name == 'Krios01')
            cls.vitrobot = next(i for i in insts if i.name == 'Vitrobot01')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, 'files', ''), ignore_errors=True)

    @classmethod
    def get_db_url(cls, instance):
        return cls.DB_URL

    # ------------------------- helpers ---------------------------------------
    def client_for(self, user=None):
        client = self.app.test_client()
        if user is not None:
            with client.session_transaction() as sess:
                sess['_user_id'] = str(user.id)
                sess['_fresh'] = True
                sess['mi_csrf'] = 'token-%d' % user.id
        return client

    def post(self, client, url, data, user=None, csrf=True):
        headers = {'X-MI-CSRF': 'token-%d' % user.id} if csrf and user else {}
        return client.post(url, json=data, headers=headers)

    def report(self, user=None, **overrides):
        user = user or self.reporter
        data = {'instrument_id': self.krios.id, 'problem_area': 'stage',
                'symptoms': ['drift'], 'severity': 'degraded',
                'title': 'Stage drift after cassette exchange',
                'description': 'Drift above 5 A/s for 30 min.',
                'context': {'software': 'EPU', 'session_status': 'Running'}}
        data.update(overrides)
        client = self.client_for(user)
        return self.post(client, '/api/mi/issues', data, user=user)

    # ------------------------- tests -----------------------------------------
    def test_schema_is_current(self):
        self.assertTrue(self.svc.db.is_current())

    def test_instruments_synced_from_emhub(self):
        with self.app.app_context():
            names = {i.name: i for i in self.svc.instruments(reportable_only=False)}
        self.assertEqual(names['Krios01'].instrument_type, 'tem')
        self.assertEqual(names['Vitrobot01'].instrument_type, 'prep')
        self.assertFalse(names['Krios02'].is_reportable)  # inactive in EMhub

    def test_requires_login(self):
        client = self.client_for(None)
        self.assertEqual(client.get('/api/mi/meta').status_code, 401)
        self.assertEqual(client.post('/api/mi/issues', json={}).status_code, 401)

    def test_requires_csrf(self):
        client = self.client_for(self.reporter)
        r = self.post(client, '/api/mi/issues', {}, user=self.reporter, csrf=False)
        self.assertEqual(r.status_code, 403)

    def test_meta(self):
        r = self.client_for(self.reporter).get('/api/mi/meta')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        names = [i['name'] for i in data['instruments']]
        self.assertIn('Krios01', names)
        self.assertNotIn('Krios02', names)
        self.assertEqual(data['roles'], ['reporter'])
        krios = next(i for i in data['instruments'] if i['name'] == 'Krios01')
        self.assertIn('beam', krios['problem_areas'])
        self.assertNotIn('fib', krios['problem_areas'])

    def test_create_issue(self):
        r = self.report()
        self.assertEqual(r.status_code, 201, r.get_json())
        issue = r.get_json()['issue']
        self.assertRegex(issue['ticket'], r'^MI-\d{4}-\d{5}$')
        self.assertEqual(issue['status'], 'new')
        self.assertEqual(issue['reporter_name'], self.reporter.name)
        self.assertEqual(issue['symptoms'][0]['id'], 'drift')
        self.assertEqual(issue['context']['software'], 'EPU')
        self.assertEqual([e['kind'] for e in issue['events']], ['created'])

        from emhub.issues.models import OutboxMessage
        from sqlalchemy import select
        with self.app.app_context():
            msgs = list(self.svc.session.scalars(
                select(OutboxMessage).where(OutboxMessage.issue_id == issue['id'])))
            self.assertIn(('receipt', self.reporter.email),
                          [(m.channel, m.destination) for m in msgs])

    def test_idempotent_submit(self):
        r1 = self.report(idempotency_key='same-key-1')
        r2 = self.report(idempotency_key='same-key-1')
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r1.get_json()['issue']['id'], r2.get_json()['issue']['id'])

    def test_validation(self):
        r = self.report(title='  ', instrument_id=999999, severity='whatever')
        self.assertEqual(r.status_code, 422)
        errors = r.get_json()['errors']
        self.assertEqual(set(errors), {'title', 'instrument_id', 'severity'})

        r = self.report(problem_area='fib')  # not valid for a TEM
        self.assertEqual(r.status_code, 422)
        self.assertIn('problem_area', r.get_json()['errors'])

        r = self.report(symptoms=['not-a-symptom'])
        self.assertIn('symptoms', r.get_json()['errors'])

        r = self.report(occurred_at='2999-01-01T10:00')
        self.assertIn('occurred_at', r.get_json()['errors'])

    def test_severity_raised_to_minimum(self):
        r = self.report(problem_area='vacuum', symptoms=[], severity='question')
        issue = r.get_json()['issue']
        self.assertEqual(issue['severity'], 'stop')
        self.assertEqual(issue['events'][0]['data']['bumped_from'], 'question')

    def test_visibility(self):
        issue = self.report(contact='Room B-101').get_json()['issue']
        url = '/api/mi/issues/%d' % issue['id']

        other = self.client_for(self.other).get(url).get_json()['issue']
        for key in ('reporter_name', 'reporter_email', 'contact'):
            self.assertNotIn(key, other)

        staff = self.client_for(self.manager).get(url).get_json()['issue']
        self.assertEqual(staff['contact'], 'Room B-101')
        self.assertEqual(staff['reporter_email'], self.reporter.email)

    def test_list_issues(self):
        self.report(title='Listed issue')
        r = self.client_for(self.other).get('/api/mi/issues?status=open')
        titles = [i['title'] for i in r.get_json()['issues']]
        self.assertIn('Listed issue', titles)
        r = self.client_for(self.other).get('/api/mi/issues?mine=1')
        mine = r.get_json()['issues']
        self.assertTrue(all(i['is_mine'] for i in mine))
        self.assertNotIn('Listed issue', [i['title'] for i in mine])

    def test_disable_instrument(self):
        url = '/api/mi/instruments/%d' % self.vitrobot.id
        r = self.post(self.client_for(self.reporter), url, {'enabled': False}, user=self.reporter)
        self.assertEqual(r.status_code, 403)

        r = self.post(self.client_for(self.admin), url, {'enabled': False}, user=self.admin)
        self.assertEqual(r.status_code, 200)
        try:
            r = self.report(instrument_id=self.vitrobot.id, problem_area='other', symptoms=[])
            self.assertEqual(r.status_code, 422)
        finally:
            self.post(self.client_for(self.admin), url, {'enabled': True}, user=self.admin)

    def test_pages(self):
        issue = self.report().get_json()['issue']
        client = self.client_for(self.reporter)

        main = client.get('/main?content_id=mi_issues').get_data(as_text=True)
        self.assertIn('Microscope Issues</a>', main)  # sidebar tab

        for params in ({'content_id': 'mi_issues'},
                       {'content_id': 'mi_issues', 'status': 'all', 'mine': '1'},
                       {'content_id': 'mi_report', 'resource_id': str(self.krios.resource_id)},
                       {'content_id': 'mi_issue', 'issue_id': str(issue['id']), 'created': '1'}):
            html = client.post('/get_content', data=params).get_data(as_text=True)
            self.assertNotIn('error-modal-title', html, params)
            self.assertNotIn('Traceback', html, params)

        html = client.post('/get_content', data={'content_id': 'mi_issue',
                                                 'issue_id': str(issue['id'])}).get_data(as_text=True)
        self.assertIn(issue['ticket'], html)

    # ------------------------- Phase 2: workflow ------------------------------
    def act(self, user, issue, action, **extra):
        data = {'action': action, 'version': issue['version']}
        data.update(extra)
        r = self.post(self.client_for(user), '/api/mi/issues/%d/transition' % issue['id'],
                      data, user=user)
        return r, (r.get_json() or {}).get('issue')

    def test_workflow(self):
        issue = self.report().get_json()['issue']

        r, _ = self.act(self.reporter, issue, 'acknowledge')
        self.assertEqual(r.status_code, 403)  # reporters cannot triage

        r, issue2 = self.act(self.manager, issue, 'acknowledge')
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(issue2['status'], 'acknowledged')
        self.assertIsNotNone(issue2['acknowledged_at'])
        self.assertEqual(issue2['assignee_name'], self.manager.name)

        r, _ = self.act(self.manager, issue, 'investigate')  # stale version
        self.assertEqual(r.status_code, 409)

        r, _ = self.act(self.manager, issue2, 'close')  # only resolved issues
        self.assertEqual(r.status_code, 422)

        r, _ = self.act(self.manager, issue2, 'resolve')
        self.assertEqual(r.status_code, 422)
        self.assertEqual(set(r.get_json()['errors']), {'resolution_type', 'root_cause'})

        r, issue3 = self.act(self.manager, issue2, 'resolve',
                             resolution_type='Fixed by staff on site',
                             root_cause='Instrument hardware',
                             resolution_notes='Re-seated the cassette.', time_lost_min=45)
        self.assertEqual(issue3['status'], 'resolved')
        self.assertEqual(issue3['resolution']['time_lost_min'], 45)

        r, _ = self.act(self.other, issue3, 'reopen', reason='Still failing')
        self.assertEqual(r.status_code, 403)  # not involved in this issue
        r, _ = self.act(self.reporter, issue3, 'reopen')
        self.assertEqual(r.status_code, 422)  # reason required
        r, issue4 = self.act(self.reporter, issue3, 'reopen', reason='Fails again after reload')
        self.assertEqual(issue4['status'], 'reopened')
        self.assertIsNone(issue4['resolved_at'])
        kinds = [e['kind'] for e in issue4['events']]
        self.assertEqual(kinds, ['created', 'acknowledge', 'resolve', 'reopen'])

    def test_update_and_downtime(self):
        issue = self.report().get_json()['issue']
        url = '/api/mi/issues/%d/update' % issue['id']
        client = self.client_for(self.manager)

        r = self.post(client, url, {'version': issue['version'], 'assignee_id': self.other.id},
                      user=self.manager)
        self.assertEqual(r.status_code, 422)  # not a staff member

        r = self.post(client, url, {'version': issue['version'], 'severity': 'blocked',
                                    'instrument_state': 'unavailable',
                                    'state_since': '2020-01-01T08:00'}, user=self.manager)
        self.assertEqual(r.status_code, 200, r.get_json())
        issue = r.get_json()['issue']
        self.assertEqual(issue['severity'], 'blocked')
        self.assertEqual(len(issue['downtimes']), 1)
        self.assertIsNone(issue['downtimes'][0]['end'])

        r, issue = self.act(self.manager, issue, 'resolve', resolution_type='Workaround in place',
                            root_cause='Unknown')
        self.assertEqual(issue['instrument_state'], 'operational')
        self.assertIsNotNone(issue['downtimes'][0]['end'])
        # Reporters do not see downtime bookkeeping
        mine = self.client_for(self.reporter).get('/api/mi/issues/%d' % issue['id']).get_json()['issue']
        self.assertNotIn('downtimes', mine)

    def test_comments_and_join(self):
        issue = self.report().get_json()['issue']
        url = '/api/mi/issues/%d/comments' % issue['id']

        r = self.post(self.client_for(self.reporter), url, {'text': 'Still drifting'}, user=self.reporter)
        self.assertEqual(r.status_code, 201)
        r = self.post(self.client_for(self.other), url, {'text': 'Me too'}, user=self.other)
        self.assertEqual(r.status_code, 403)
        r = self.post(self.client_for(self.reporter), url, {'text': 'x', 'internal': True}, user=self.reporter)
        self.assertEqual(r.status_code, 403)

        r = self.post(self.client_for(self.other), '/api/mi/issues/%d/join' % issue['id'], {}, user=self.other)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['issue']['is_affected'])
        r = self.post(self.client_for(self.other), url, {'text': 'Me too'}, user=self.other)
        self.assertEqual(r.status_code, 201)

        r = self.post(self.client_for(self.manager), url, {'text': 'Check the vendor log', 'internal': True},
                      user=self.manager)
        staff_view = r.get_json()['issue']
        self.assertIn('Check the vendor log', [e['data'].get('text') for e in staff_view['events']])
        mine = self.client_for(self.reporter).get('/api/mi/issues/%d' % issue['id']).get_json()['issue']
        self.assertNotIn('Check the vendor log', [e['data'].get('text') for e in mine['events']])
        self.assertEqual(mine['affected'], [self.other.name])

    def test_duplicate(self):
        original = self.report(title='Original').get_json()['issue']
        dup = self.report(user=self.other, title='Same problem').get_json()['issue']
        r, dup2 = self.act(self.manager, dup, 'duplicate', duplicate_of=original['ticket'])
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(dup2['status'], 'duplicate')
        self.assertEqual(dup2['duplicate_of'], original['ticket'])
        orig = self.client_for(self.manager).get('/api/mi/issues/%d' % original['id']).get_json()['issue']
        self.assertIn(self.other.name, orig['affected'])
        r, _ = self.act(self.manager, original, 'duplicate', duplicate_of=original['ticket'])
        self.assertEqual(r.status_code, 422)  # not a duplicate of itself

    def test_attachments(self):
        issue = self.report().get_json()['issue']
        url = '/api/mi/issues/%d/attachments' % issue['id']
        png = b'\x89PNG\r\n\x1a\n' + b'0' * 100

        def upload(user, data, name, internal=False):
            return self.client_for(user).post(
                url, data={'file': (io.BytesIO(data), name), 'internal': '1' if internal else '0'},
                headers={'X-MI-CSRF': 'token-%d' % user.id}, content_type='multipart/form-data')

        self.assertEqual(upload(self.reporter, png, '../../shot.png').status_code, 201)
        self.assertEqual(upload(self.reporter, b'not an image', 'fake.png').status_code, 422)
        self.assertEqual(upload(self.reporter, b'MZ\x90\x00', 'tool.exe').status_code, 422)
        self.assertEqual(upload(self.other, png, 'x.png').status_code, 403)
        r = upload(self.manager, b'vendor log line\n', 'vendor.log', internal=True)
        self.assertEqual(r.status_code, 201)

        staff_files = r.get_json()['issue']['attachments']
        self.assertEqual([a['filename'] for a in staff_files], ['shot.png', 'vendor.log'])
        mine = self.client_for(self.reporter).get('/api/mi/issues/%d' % issue['id']).get_json()['issue']
        self.assertEqual([a['filename'] for a in mine['attachments']], ['shot.png'])

        shot, log = staff_files
        r = self.client_for(self.reporter).get('/api/mi/attachments/%d' % shot['id'])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, png)
        self.assertEqual(r.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(self.client_for(self.other).get('/api/mi/attachments/%d' % shot['id']).status_code, 403)
        self.assertEqual(self.client_for(self.reporter).get('/api/mi/attachments/%d' % log['id']).status_code, 404)

    def test_notices(self):
        data = {'instrument_id': self.krios.id, 'message': 'Krios01 cryo-cycle until Friday'}
        r = self.post(self.client_for(self.reporter), '/api/mi/notices', data, user=self.reporter)
        self.assertEqual(r.status_code, 403)
        r = self.post(self.client_for(self.manager), '/api/mi/notices', data, user=self.manager)
        self.assertEqual(r.status_code, 201)
        notice = r.get_json()['notice']
        msgs = lambda iid: [n['message'] for n in self.client_for(self.reporter).get(
            '/api/mi/notices?instrument_id=%d' % iid).get_json()['notices']]
        self.assertIn(data['message'], msgs(self.krios.id))
        self.assertNotIn(data['message'], msgs(self.vitrobot.id))
        self.post(self.client_for(self.manager), '/api/mi/notices/%d/remove' % notice['id'], {}, user=self.manager)
        self.assertNotIn(data['message'], msgs(self.krios.id))

    def test_duplicates_lookup(self):
        issue = self.report(problem_area='detector', symptoms=[], title='Detector not ready').get_json()['issue']
        r = self.client_for(self.other).get('/api/mi/duplicates?instrument_id=%d&problem_area=detector'
                                            % self.krios.id)
        found = r.get_json()['issues']
        self.assertEqual(found[0]['problem_area'], 'detector')
        self.assertIn(issue['id'], [i['id'] for i in found])

    # ------------------------- Phase 2: notifications -------------------------
    def with_settings(self, settings):
        client = self.client_for(self.admin)
        r = self.post(client, '/api/mi/settings', settings, user=self.admin)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.addCleanup(self.post, client, '/api/mi/settings', {}, self.admin)

    def outbox(self, issue_id=None):
        from emhub.issues.models import OutboxMessage
        from sqlalchemy import select
        with self.app.app_context():
            q = select(OutboxMessage).order_by(OutboxMessage.id)
            if issue_id:
                q = q.where(OutboxMessage.issue_id == issue_id)
            return list(self.svc.session.scalars(q))

    def test_settings_validation(self):
        bad = {'email': ['not-an-email'], 'webhooks': ['unknown-hook'],
               'routes': [{'instruments': ['NoSuchScope'], 'min_severity': 'stop'}]}
        r = self.post(self.client_for(self.admin), '/api/mi/settings', bad, user=self.admin)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(set(r.get_json()['errors']), {'email', 'webhooks', 'routes.0'})
        r = self.post(self.client_for(self.manager), '/api/mi/settings', {}, user=self.manager)
        self.assertEqual(r.status_code, 403)  # admins only

    def test_routing_and_worker(self):
        from emhub.issues.worker import NotificationWorker, RetryLater, PermanentError
        self.with_settings({'email': ['facility@example.org'], 'webhooks': ['teams-cryoem'],
                            'routes': [{'instruments': ['Krios01'], 'min_severity': 'blocked',
                                        'email': ['krios-team@example.org']}]})
        quiet = self.report(severity='question', title='Routing quiet').get_json()['issue']
        urgent = self.report(severity='stop', title='Routing urgent').get_json()['issue']
        dests = lambda i: sorted(m.destination for m in self.outbox(i['id']))
        self.assertEqual(dests(quiet), sorted(['facility@example.org', 'teams-cryoem', self.reporter.email]))
        self.assertIn('krios-team@example.org', dests(urgent))

        sent, posted = [], []
        worker = NotificationWorker(self.app, send_email=lambda *a: sent.append(a),
                                    post_webhook=lambda *a: posted.append(a))
        while worker.run_once():
            pass
        self.assertTrue(all(m.status == 'accepted' for m in self.outbox(urgent['id'])))
        subjects = [s for to, s, body in sent if to == 'krios-team@example.org']
        self.assertTrue(subjects[0].startswith('[%s] New issue - Stop now - Krios01' % urgent['ticket']))
        body = [b for to, s, b in sent if to == 'krios-team@example.org'][0]
        self.assertIn('https://emhub.example.org/main?content_id=mi_issue&issue_id=%d' % urgent['id'], body)
        url, card = posted[-1]
        self.assertEqual(url, 'https://example.invalid/hook')
        self.assertEqual(card['attachments'][0]['contentType'], 'application/vnd.microsoft.card.adaptive')

        # Failures: transient errors are retried later, permanent ones fail
        failing = self.report(title='Delivery failure').get_json()['issue']

        def flaky_email(to, subject, body):
            raise RetryLater('SMTP down')

        def broken_hook(url, payload):
            raise PermanentError('HTTP 404')

        worker = NotificationWorker(self.app, send_email=flaky_email, post_webhook=broken_hook)
        worker.run_once()
        status = {m.channel: m for m in self.outbox(failing['id'])}
        self.assertEqual(status['email'].status, 'retrying')
        self.assertGreater(status['email'].next_attempt_at.replace(tzinfo=dt.timezone.utc)
                           if status['email'].next_attempt_at.tzinfo is None
                           else status['email'].next_attempt_at,
                           dt.datetime.now(dt.timezone.utc))
        self.assertEqual(status['webhook'].status, 'failed')
        # The report itself is untouched by delivery failures
        self.assertEqual(self.client_for(self.reporter).get('/api/mi/issues/%d' % failing['id']).status_code, 200)

        r = self.post(self.client_for(self.admin), '/api/mi/outbox/%d/retry' % status['webhook'].id, {},
                      user=self.admin)
        self.assertEqual(r.get_json()['status'], 'queued')

    def test_progress_emails(self):
        issue = self.report().get_json()['issue']
        r, issue = self.act(self.manager, issue, 'acknowledge')
        msgs = [(m.kind, m.destination) for m in self.outbox(issue['id'])]
        self.assertIn(('acknowledge', self.reporter.email), msgs)
        # No email to the person who did the action
        self.assertNotIn(('acknowledge', self.manager.email), msgs)

    def test_escalation(self):
        self.with_settings({'escalation': {'email': ['oncall@example.org']},
                            'ack_minutes': {'stop': 2}})
        issue = self.report(severity='stop', title='Escalate me').get_json()['issue']
        with self.app.app_context():
            soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1)
            self.svc.escalate_overdue(now=soon)
            later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=3)
            self.assertGreaterEqual(self.svc.escalate_overdue(now=later), 1)
            self.svc.escalate_overdue(now=later)  # only once
        msgs = [(m.kind, m.destination) for m in self.outbox(issue['id'])]
        self.assertEqual(msgs.count(('escalated', 'oncall@example.org')), 1)

        acked = self.report(severity='stop', title='Acknowledged in time').get_json()['issue']
        self.act(self.manager, acked, 'acknowledge')
        with self.app.app_context():
            self.svc.escalate_overdue(now=later)
        self.assertNotIn('escalated', [m.kind for m in self.outbox(acked['id'])])

    def test_settings_page(self):
        r = self.client_for(self.admin).post('/get_content', data={'content_id': 'mi_settings'})
        html = r.get_data(as_text=True)
        self.assertNotIn('error-modal-title', html)
        self.assertIn('teams-cryoem', html)
        r = self.client_for(self.reporter).post('/get_content', data={'content_id': 'mi_settings'})
        self.assertIn('error-modal-title', r.get_data(as_text=True))

    def test_issue_page_as_staff(self):
        issue = self.report().get_json()['issue']
        html = self.client_for(self.manager).post('/get_content', data={
            'content_id': 'mi_issue', 'issue_id': str(issue['id'])}).get_data(as_text=True)
        self.assertNotIn('error-modal-title', html)
        self.assertIn('Staff actions', html)
        html = self.client_for(self.reporter).post('/get_content', data={
            'content_id': 'mi_issue', 'issue_id': str(issue['id'])}).get_data(as_text=True)
        self.assertNotIn('Staff actions', html)


    # ------------------------- Phase 3: insights ------------------------------
    def test_insights_page_and_access(self):
        self.report(title='For insights')
        html = self.client_for(self.manager).post('/get_content', data={
            'content_id': 'mi_insights'}).get_data(as_text=True)
        self.assertNotIn('error-modal-title', html)
        self.assertIn('Incidents per week', html)
        html = self.client_for(self.reporter).post('/get_content', data={
            'content_id': 'mi_insights'}).get_data(as_text=True)
        self.assertIn('error-modal-title', html)  # managers only
        bad = self.client_for(self.manager).post('/get_content', data={
            'content_id': 'mi_insights', 'start': '2026-13-01'}).get_data(as_text=True)
        self.assertIn('error-modal-title', bad)

    def test_csv_export(self):
        self.report(title='=HYPERLINK("http://evil")', contact='lab')
        r = self.client_for(self.reporter).get('/api/mi/export.csv')
        self.assertEqual(r.status_code, 403)
        r = self.client_for(self.manager).get('/api/mi/export.csv')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, 'text/csv')
        import csv as _csv
        rows = list(_csv.DictReader(io.StringIO(r.get_data(as_text=True).lstrip('\ufeff'))))
        evil = [x for x in rows if 'HYPERLINK' in x['title']][0]
        self.assertTrue(evil['title'].startswith("'="))
        self.assertEqual(evil['reporter_email'], self.reporter.email)

    def test_weekly_summary(self):
        from emhub.issues.worker import NotificationWorker
        self.with_settings({'summary_email': ['head@example.org']})
        tz = self.app.dm.timezone
        monday_8am = dt.datetime(2026, 11, 2, 8, 0, tzinfo=tz)  # a Monday
        with self.app.app_context():
            self.assertEqual(self.svc.enqueue_weekly_summary(now=monday_8am), 1)
            self.assertEqual(self.svc.enqueue_weekly_summary(now=monday_8am), 0)  # once a week
            self.assertEqual(self.svc.enqueue_weekly_summary(
                now=monday_8am + dt.timedelta(days=2)), 0)
        msg = [m for m in self.outbox() if m.kind == 'summary'][-1]
        self.assertEqual(msg.payload, {'start': '2026-10-26', 'end': '2026-11-01', 'week': '2026-W44'})
        sent = []
        NotificationWorker(self.app, send_email=lambda *a: sent.append(a),
                           post_webhook=lambda *a: None).run_once()
        subject, body = [(s_, b) for to, s_, b in sent if to == 'head@example.org'][0]
        self.assertIn('weekly summary 2026-W44', subject)
        self.assertIn('New incidents:', body)

    def test_import_logbooks(self):
        import flask_login
        from emhub.issues.importers import import_logbooks
        dm = self.app.dm
        with self.app.test_request_context():
            flask_login.login_user(self.admin)
            lb = dm.create_project(title='Krios01 logbook %s' % self.__class__.__name__,
                                   status='special:logbook', validate=False,
                                   extra={'resource_id': self.krios.resource_id})
            orphan = dm.create_project(title='Computing logbook', status='special:logbook',
                                       validate=False, extra={})
            day = dt.datetime(2026, 5, 4, 14, 0, tzinfo=dt.timezone.utc)
            dm.create_entry(project_id=lb.id, type='logbook_microscope', title='K3 not ready',
                            description='Camera server restart needed', date=day,
                            extra={'data': {'category': 'detector', 'tags': ['k3'], 'actions': [
                                {'date': '2026/05/04', 'status': 'critical', 'action': 'Reported'},
                                {'date': '2026/05/06', 'status': 'solved', 'action': 'Restarted'}]}})
            dm.create_entry(project_id=lb.id, type='logbook_microscope', title='Stage noise',
                            date=day, extra={'data': {'actions': [
                                {'date': '2026/05/05', 'status': 'medium', 'action': 'Seen twice'}]}})
            dm.create_entry(project_id=orphan.id, type='logbook_microscope', title='NAS full',
                            date=day, extra={'data': {}})
            lb_id, orphan_id = lb.id, orphan.id

        with self.app.app_context():
            report = import_logbooks(self.svc, [lb_id, orphan_id], dry_run=True)
            self.assertEqual(report['imported'], 2)
            self.assertEqual(report['skipped'], 1)
            report = import_logbooks(self.svc, [lb_id, orphan_id], dry_run=False)
            self.assertEqual(report['imported'], 2)
            again = import_logbooks(self.svc, [lb_id], dry_run=False)
            self.assertEqual((again['imported'], again['logbooks'][0]['already']), (0, 2))

        issues = self.client_for(self.manager).get('/api/mi/issues?status=all').get_json()['issues']
        k3 = [i for i in issues if i['title'] == 'K3 not ready'][0]
        noise = [i for i in issues if i['title'] == 'Stage noise'][0]
        self.assertEqual((k3['status'], k3['severity']), ('closed', 'stop'))
        self.assertEqual((noise['status'], noise['severity']), ('acknowledged', 'degraded'))
        detail = self.client_for(self.manager).get('/api/mi/issues/%d' % k3['id']).get_json()['issue']
        self.assertEqual([e['kind'] for e in detail['events']], ['imported', 'comment', 'comment'])
        self.assertTrue(all(e['internal'] for e in detail['events']))
        # Imported history never notifies anyone
        self.assertEqual(self.outbox(k3['id']), [])


class TestIssuesSqlite(IssuesTestBase, unittest.TestCase):
    @classmethod
    def get_db_url(cls, instance):
        return 'sqlite:///' + os.path.join(instance, 'emhub-issues.sqlite')


class TestIssuesPostgres(IssuesTestBase, unittest.TestCase):
    @classmethod
    def get_db_url(cls, instance):
        return get_pg_url()


class TestIssuesDisabled(unittest.TestCase):
    def test_tab_hidden_when_disabled(self):
        os.environ['EMHUB_INSTANCE'] = get_test_instance()
        from emhub import create_app
        app = create_app({'TESTING': True, 'SECRET_KEY': 'test'})
        self.assertIsNone(app.issues)
        user = next(u for u in app.dm.get_users() if u.is_admin)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user.id)
        html = client.get('/main?content_id=dashboard').get_data(as_text=True)
        self.assertNotIn('Microscope Issues', html)
        self.assertEqual(client.get('/api/mi/meta').status_code, 404)



class TestInsightsDefinitions(unittest.TestCase):
    """ Insight definitions on hand-made fixtures with known answers. """

    def setUp(self):
        from types import SimpleNamespace as NS
        self.NS = NS
        self.t0 = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)

    def at(self, hours):
        return self.t0 + dt.timedelta(hours=hours)

    def issue(self, id, inst=1, reported=0, ack=None, resolved=None, occurred=None,
              status='resolved', downtimes=(), imported=False, area='stage',
              severity='blocked', events=(), resolution=None):
        NS = self.NS
        return NS(id=id, instrument_id=inst, instrument_name='Krios%02d' % inst,
                  reported_at=self.at(reported),
                  acknowledged_at=self.at(ack) if ack is not None else None,
                  resolved_at=self.at(resolved) if resolved is not None else None,
                  occurred_at=self.at(occurred) if occurred is not None else None,
                  status=status, imported=imported, problem_area=area, severity=severity,
                  assignee_name=None, symptoms=[], resolution=resolution or {},
                  events=[NS(kind=k, at=self.at(h), data=d) for k, h, d in events],
                  downtimes=[NS(instrument_id=inst, state=st, start=self.at(a),
                                end=self.at(b) if b is not None else None)
                             for st, a, b in downtimes])

    def compute(self, issues, open_issues=(), now_h=24 * 30, **kw):
        from emhub.issues.insights import compute
        return compute(issues, list(open_issues), {1: 'Krios01', 2: 'Krios02'},
                       self.t0, self.at(24 * 30), self.at(now_h), dt.timezone.utc, **kw)

    def test_merge_and_percentiles(self):
        from emhub.issues.insights import merge_intervals, percentile
        a = self.at
        self.assertEqual(merge_intervals([(a(0), a(4)), (a(2), a(6)), (a(8), a(9))]),
                         [(a(0), a(6)), (a(8), a(9))])
        self.assertEqual(percentile([10, 20, 30, 40], 0.5), 25)
        self.assertAlmostEqual(percentile(range(1, 11), 0.9), 9.1)
        self.assertIsNone(percentile([], 0.5))

    def test_overlapping_downtime_counted_once(self):
        issues = [self.issue(1, downtimes=[('unavailable', 10, 20)], resolved=20, ack=1),
                  self.issue(2, downtimes=[('unavailable', 15, 30), ('degraded', 30, 40)],
                             resolved=40, ack=2)]
        r = self.compute(issues, booked_hours={1: 100.0})
        krios = r['per_instrument'][0]
        self.assertEqual(krios['downtime_hours'], 20.0)  # 10-30 merged, degraded excluded
        self.assertEqual(krios['availability'], round(1 - 20 / 720, 4))
        self.assertEqual(krios['per_100_booked_hours'], 2.0)

    def test_open_downtime_clipped_to_now(self):
        r = self.compute([self.issue(1, status='investigating', downtimes=[('unavailable', 700, None)])],
                         now_h=710)
        self.assertEqual(r['per_instrument'][0]['downtime_hours'], 10.0)

    def test_response_and_restoration(self):
        issues = [
            # response 30 min; restoration from occurred_at (-2h) to downtime end (5h) = 7h
            self.issue(1, reported=0, ack=0.5, occurred=-2, resolved=8,
                       downtimes=[('unavailable', 0, 5)]),
            # no downtime: restoration = resolved - reported = 3h; response 90 min
            self.issue(2, reported=10, ack=11.5, resolved=13),
            # reopened, unresolved: counted as an incident, not in restoration
            self.issue(3, reported=20, ack=21, status='reopened'),
            # imported history: excluded from timing
            self.issue(4, reported=30, ack=30, resolved=31, imported=True),
            # duplicate report: not an incident
            self.issue(5, reported=40, status='duplicate'),
        ]
        r = self.compute(issues)
        t = r['totals']
        self.assertEqual((t['incidents'], t['duplicates']), (4, 1))
        self.assertEqual(t['response_min']['n'], 3)
        self.assertEqual(t['response_min']['median'], 60.0)
        self.assertEqual(t['restoration_h']['n'], 2)
        self.assertEqual(t['restoration_h']['median'], 5.0)
        self.assertEqual(r['coverage']['unresolved'], 1)
        self.assertEqual(r['coverage']['imported'], 1)

    def test_vendor_wait_and_recurrence(self):
        from emhub.issues.insights import vendor_wait_hours
        i = self.issue(1, events=[('created', 0, {}),
                                  ('waiting_vendor', 2, {'to': 'waiting_vendor'}),
                                  ('investigate', 26, {'to': 'investigating'}),
                                  ('resolve', 30, {'to': 'resolved'})])
        self.assertEqual(vendor_wait_hours(i, self.at(100)), 24.0)
        issues = [self.issue(1, reported=0), self.issue(2, reported=24 * 10),
                  self.issue(3, reported=24 * 29, area='beam'),
                  self.issue(4, inst=2, reported=0, area='other'),
                  self.issue(5, inst=2, reported=24, area='other'),
                  self.issue(6, inst=2, reported=0, imported=True),
                  self.issue(7, inst=2, reported=24, imported=True)]
        rec = self.compute(issues)['recurrence']
        self.assertEqual([(x['instrument'], x['repeats']) for x in rec], [('Krios01', 1)])

    def test_booked_hours_only_real_bookings(self):
        from emhub.issues.insights import booked_hours_by_resource
        NS = self.NS
        b = [NS(type='booking', resource_id=1, start=self.at(-5), end=self.at(5)),
             NS(type='slot', resource_id=1, start=self.at(0), end=self.at(48)),
             NS(type='downtime', resource_id=1, start=self.at(0), end=self.at(48))]
        self.assertEqual(dict(booked_hours_by_resource(b, self.t0, self.at(24))), {1: 5.0})


if __name__ == '__main__':
    unittest.main()
