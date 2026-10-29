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

        cls.app = create_app({'TESTING': True, 'SECRET_KEY': 'test',
                              'EMHUB_ISSUES': True, 'ISSUES_DB_URL': url})
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
            self.assertEqual([(m.channel, m.destination) for m in msgs],
                             [('receipt', self.reporter.email)])

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
        self.assertEqual(r.get_json()['issues'], [])

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


if __name__ == '__main__':
    unittest.main()
