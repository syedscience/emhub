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
"""

import datetime as dt
import io
import json
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


class TestIssues(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ['EMHUB_INSTANCE'] = get_test_instance()
        from emhub import create_app
        cls.files = tempfile.mkdtemp(prefix='emhub-issues-files-')
        cls.app = create_app({'TESTING': True, 'SECRET_KEY': 'test',
                              'EMHUB_ISSUES': True,
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

    def get_issue(self, issue_id):
        """ The full issue, as a manager sees it. """
        r = self.client_for(self.manager).get('/api/mi/issues/%d' % issue_id)
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()['issue']

    def new_issue(self, **overrides):
        """ Report as the reporter, return the full issue. """
        r = self.report(**overrides)
        self.assertEqual(r.status_code, 201, r.get_json())
        return self.get_issue(r.get_json()['issue']['id'])

    def outbox(self, issue_id=None):
        with self.app.app_context():
            if issue_id:
                return self.svc.store.messages(issue_id=issue_id)
            return self.svc.store.messages()

    def content(self, user, **params):
        r = self.client_for(user).post('/get_content', data=params)
        return r.status_code, r.get_data(as_text=True)

    # ------------------------- storage ---------------------------------------
    def test_stored_in_emhub_project(self):
        issue = self.new_issue(title='Stored as an entry')
        dm = self.app.dm
        with self.app.app_context():
            project = dm.get_project_by(status='special:issues')
            self.assertIsNotNone(project)
            entry = dm.get_entry_by(id=issue['id'])
            self.assertEqual((entry.project_id, entry.type), (project.id, 'microscope_issue'))
            self.assertEqual(entry.title, 'Stored as an entry')
            self.assertEqual(entry.extra['data']['ticket'], issue['ticket'])
            # Hidden from the normal project lists (they show 'active' projects)
            self.assertNotEqual(project.status, 'active')
        self.assertFalse(os.path.exists(os.path.join(self.app.instance_path,
                                                     'emhub-issues.sqlite')))

    def test_instruments_from_emhub(self):
        with self.app.app_context():
            names = {i.name: i for i in self.svc.instruments(reportable_only=False)}
        self.assertEqual(names['Krios01'].instrument_type, 'tem')
        self.assertEqual(names['Krios01'].id, names['Krios01'].resource_id)
        self.assertEqual(names['Vitrobot01'].instrument_type, 'plunge')
        self.assertFalse(names['Krios02'].is_reportable)  # inactive in EMhub

    # ------------------------- access ----------------------------------------
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
        # A plunge freezer only gets the problems a plunge freezer can have
        r = self.client_for(self.manager).get('/api/mi/meta')
        vitrobot = next(i for i in r.get_json()['instruments'] if i['name'] == 'Vitrobot01')
        self.assertIn('plunge_blot', vitrobot['problem_areas'])
        for area in ('beam', 'vacuum', 'detector', 'loader', 'data', 'light_laser'):
            self.assertNotIn(area, vitrobot['problem_areas'])
        self.assertTrue(data['instrument_groups'])
        r = self.client_for(self.manager).get('/api/mi/meta')
        self.assertEqual(r.get_json()['roles'], ['manager', 'reporter'])

    def test_reporters_only_get_a_receipt(self):
        r = self.report(contact='Room B-101')
        self.assertEqual(r.status_code, 201, r.get_json())
        receipt = r.get_json()['issue']
        self.assertRegex(receipt['ticket'], r'^MI-\d{4}-\d{4}$')
        self.assertEqual(set(receipt), {'id', 'ticket', 'title', 'instrument_name',
                                        'severity', 'status'})

        issue = self.get_issue(receipt['id'])
        self.assertEqual(issue['status'], 'new')
        self.assertEqual(issue['reporter_name'], self.reporter.name)
        self.assertEqual(issue['contact'], 'Room B-101')
        self.assertEqual(issue['symptoms'][0]['id'], 'drift')
        self.assertEqual(issue['context']['software'], 'EPU')
        self.assertEqual([e['kind'] for e in issue['events']], ['created'])
        self.assertIn(('reporter', self.reporter.email),
                      [(m.channel, m.destination) for m in self.outbox(issue['id'])])

    def test_only_managers_see_issues(self):
        issue = self.new_issue()
        url = '/api/mi/issues/%d' % issue['id']
        for user in (self.reporter, self.other):
            client = self.client_for(user)
            self.assertEqual(client.get(url).status_code, 403)
            self.assertEqual(client.get('/api/mi/issues').status_code, 403)
            self.assertEqual(client.get('/api/mi/export.csv').status_code, 403)
            for params in ({'content_id': 'mi_issues'},
                           {'content_id': 'mi_issue', 'issue_id': str(issue['id'])},
                           {'content_id': 'mi_insights'}):
                status, html = self.content(user, **params)
                self.assertIn('error-modal-title', html, params)
                self.assertNotIn(issue['title'], html, params)
            status, html = self.content(user, content_id='mi_report')
            self.assertNotIn('error-modal-title', html)
        for user in (self.manager, self.admin):
            self.assertEqual(self.client_for(user).get(url).status_code, 200)

    def test_generic_emhub_routes_blocked(self):
        issue = self.new_issue(title='Secret history')
        with self.app.app_context():
            pid = self.svc.store.project_id()
        for user in (self.reporter, self.manager):
            client = self.client_for(user)
            r = client.post('/api/get_entries', json={})
            ids = [e['id'] for e in json.loads(r.get_data(as_text=True))]
            self.assertNotIn(issue['id'], ids)
            self.assertTrue(ids)  # other entries are still listed
            r = client.post('/api/get_projects', json={})
            self.assertNotIn(pid, [p['id'] for p in json.loads(r.get_data(as_text=True))])
            for url in ('/api/update_entry', '/api/update_entry_extra', '/api/delete_entry'):
                r = client.post(url, json={'attrs': {'id': issue['id'], 'extra': {}}})
                self.assertEqual(r.status_code, 403, url)
            r = client.post('/api/delete_project', json={'attrs': {'id': pid}})
            self.assertEqual(r.status_code, 403)
            r = client.post('/api/create_entry', json={'attrs': {
                'project_id': pid, 'type': 'note', 'title': 'x', 'extra': {'data': {}}}})
            self.assertEqual(r.status_code, 403)
            for params in ({'content_id': 'entry_form', 'entry_id': issue['id']},
                           {'content_id': 'project_details', 'project_id': pid}):
                r = client.post('/get_content', data=params)
                self.assertEqual(r.status_code, 403, params)
                self.assertNotIn('Secret history', r.get_data(as_text=True))
            r = client.get('/images/entry?entry=%d&file=x.png' % issue['id'])
            self.assertEqual(r.status_code, 403)
        with self.app.app_context():
            self.assertIsNotNone(self.app.dm.get_entry_by(id=issue['id']))

    # ------------------------- reporting -------------------------------------
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
        issue = self.new_issue(problem_area='vacuum', symptoms=[], severity='question')
        self.assertEqual(issue['severity'], 'stop')
        self.assertEqual(issue['events'][0]['data']['bumped_from'], 'question')

    def test_list_issues(self):
        self.new_issue(title='Listed issue')
        r = self.client_for(self.manager).get('/api/mi/issues?status=open')
        issues = r.get_json()['issues']
        self.assertIn('Listed issue', [i['title'] for i in issues])
        self.assertTrue(all(i['status'] in ('new', 'acknowledged', 'investigating',
                                            'waiting_vendor', 'reopened') for i in issues))
        r = self.client_for(self.manager).get('/api/mi/issues?mine=1')
        self.assertTrue(all(i['is_mine'] for i in r.get_json()['issues']))

    def test_disable_instrument(self):
        url = '/api/mi/instruments/%d' % self.vitrobot.id
        r = self.post(self.client_for(self.manager), url, {'enabled': False}, user=self.manager)
        self.assertEqual(r.status_code, 403)  # admins only

        r = self.post(self.client_for(self.admin), url, {'enabled': False}, user=self.admin)
        self.assertEqual(r.status_code, 200)
        try:
            r = self.report(instrument_id=self.vitrobot.id, problem_area='other', symptoms=[])
            self.assertEqual(r.status_code, 422)
        finally:
            self.post(self.client_for(self.admin), url, {'enabled': True}, user=self.admin)
        r = self.report(instrument_id=self.vitrobot.id, problem_area='other', symptoms=[])
        self.assertEqual(r.status_code, 201)

    def test_unsaved_project_is_forgotten(self):
        """ Reads never create the issues project, and a project created in a
        transaction that was not saved is not remembered (regression: a
        page load created it, the request ended without saving, and every
        later report failed). """
        from emhub.issues import store as store_mod
        old_status = store_mod.PROJECT_STATUS
        store_mod.PROJECT_STATUS = 'special:issues-test'
        try:
            with self.app.app_context():
                dm = self.app.dm
                st = store_mod.IssueStore(dm)
                self.assertEqual(st.notices(), [])
                self.assertIsNone(st.existing_project_id())
                self.assertIsNone(dm.get_project_by(status='special:issues-test'))

                st.project_id()               # created, then the request ends
                dm._db_session.remove()
                self.assertIsNone(st.existing_project_id())

                pid = st.project_id()
                st.commit()
                dm._db_session.remove()
                self.assertEqual(st.existing_project_id(), pid)
                self.assertIsNotNone(dm.get_project_by(id=pid))
                dm._db_session.delete(dm.get_project_by(id=pid))
                dm._db_session.commit()
        finally:
            store_mod.PROJECT_STATUS = old_status

    def test_ticket_numbers(self):
        """ Tickets count up per year from 0001; a failed report uses no number. """
        first = self.new_issue()
        year, n = first['ticket'].split('-')[1:]
        self.assertEqual(len(n), 4)
        self.assertEqual(self.report(severity='nonsense').status_code, 422)
        with self.app.app_context():  # numbered, then rolled back
            from emhub.issues.models import Issue
            store = self.svc.store
            store.new_issue(Issue(title='x', reported_at=dt.datetime.now(dt.timezone.utc)))
            store.rollback()
        second = self.new_issue()
        self.assertEqual(second['ticket'], 'MI-%s-%04d' % (year, int(n) + 1))

    def test_problem_area_editor(self):
        """ Managers add and change problem areas, also per instrument. """
        r = self.client_for(self.reporter).get('/api/mi/catalog')
        self.assertEqual(r.status_code, 403)
        r = self.post(self.client_for(self.reporter), '/api/mi/catalog/areas',
                      {'version': 0, 'area': {'label': 'x'}}, user=self.reporter)
        self.assertEqual(r.status_code, 403)

        mc = self.client_for(self.manager)
        cat = mc.get('/api/mi/catalog').get_json()
        stage = next(a for a in cat['areas'] if a['id'] == 'stage')
        self.assertTrue(stage['builtin'])
        self.assertFalse(stage['changed'])

        # A new area, only for Krios01, with a symptom
        area = {'label': 'Cryo-shield heater', 'only_types': ['tem'],
                'only_instruments': [self.krios.id], 'general': 'Check the heater LED\nNote the time',
                'symptoms': [{'label': 'Heater error on the panel', 'minSeverity': 'blocked',
                              'steps': ['Take a photo of the panel']}]}
        r = self.post(mc, '/api/mi/catalog/areas', {'version': cat['version'], 'area': area},
                      user=self.manager)
        self.assertEqual(r.status_code, 200, r.get_json())
        new = r.get_json()['area']
        self.assertEqual(new['id'], 'cryo_shield_heater')
        self.assertEqual(new['general'], ['Check the heater LED', 'Note the time'])
        version = r.get_json()['catalog']['version']

        # A stale version is refused
        r = self.post(mc, '/api/mi/catalog/areas', {'version': cat['version'], 'area': area},
                      user=self.manager)
        self.assertEqual(r.status_code, 409)

        # A symptom only for Krios01 added to a built-in area
        stage = dict(stage, symptoms=stage['symptoms'] + [
            {'label': 'Stage shakes', 'only_instruments': [self.krios.id]}])
        r = self.post(mc, '/api/mi/catalog/areas', {'version': version, 'area': stage},
                      user=self.manager)
        self.assertEqual(r.status_code, 200, r.get_json())
        version = r.get_json()['catalog']['version']

        meta = self.client_for(self.reporter).get('/api/mi/meta').get_json()
        insts = {i['id']: i for i in meta['instruments']}
        self.assertIn('cryo_shield_heater', insts[self.krios.id]['problem_areas'])
        self.assertNotIn('cryo_shield_heater', insts[self.vitrobot.id]['problem_areas'])
        self.assertIn('stage_shakes', insts[self.krios.id]['symptoms']['stage'])

        issue = self.new_issue(problem_area='cryo_shield_heater',
                               symptoms=[new['symptoms'][0]['id']], severity='question')
        self.assertEqual(issue['severity'], 'blocked')
        r = self.report(instrument_id=self.vitrobot.id, problem_area='cryo_shield_heater',
                        symptoms=[])
        self.assertEqual(r.status_code, 422)

        # Hidden areas are not offered, but old issues keep their name
        r = self.post(mc, '/api/mi/catalog/areas/cryo_shield_heater/hide', {'version': version},
                      user=self.manager)
        self.assertEqual(r.status_code, 200)
        version = r.get_json()['catalog']['version']
        meta = self.client_for(self.reporter).get('/api/mi/meta').get_json()
        self.assertNotIn('cryo_shield_heater', meta['instruments'][0]['problem_areas'])
        status, html = self.content(self.manager, content_id='mi_issue', issue_id=str(issue['id']))
        self.assertIn('Cryo-shield heater', html)

        # Moving and resetting a built-in area
        r = self.post(mc, '/api/mi/catalog/areas/stage/up', {'version': version}, user=self.manager)
        cat = r.get_json()['catalog']
        ids = [a['id'] for a in cat['areas']]
        self.assertLess(ids.index('stage'), ids.index('beam'))
        r = self.post(mc, '/api/mi/catalog/areas/stage/reset', {'version': cat['version']},
                      user=self.manager)
        stage = next(a for a in r.get_json()['catalog']['areas'] if a['id'] == 'stage')
        self.assertFalse(stage['changed'])
        r = self.post(mc, '/api/mi/catalog/areas/cryo_shield_heater/reset',
                      {'version': r.get_json()['catalog']['version']}, user=self.manager)
        self.assertEqual(r.status_code, 422)  # only built-in areas can be reset

        status, html = self.content(self.manager, content_id='mi_catalog')
        self.assertNotIn('error-modal-title', html)
        status, html = self.content(self.reporter, content_id='mi_catalog')
        self.assertIn('error-modal-title', html)
        with self.app.app_context():
            self.assertTrue(any(e.action == 'problem_area_hide' for e in self.svc.store.audit_events()))

    def test_pages(self):
        issue = self.new_issue()
        main = self.client_for(self.reporter).get('/main?content_id=mi_report').get_data(as_text=True)
        self.assertIn('Microscope Issues</a>', main)  # sidebar tab

        status, html = self.content(self.reporter, content_id='mi_report',
                                    resource_id=str(self.krios.resource_id),
                                    submitted=issue['ticket'])
        self.assertNotIn('error-modal-title', html)
        self.assertIn('<b>Report %s sent.</b>' % issue['ticket'], html)
        self.assertIn('id="mi-detector"', html)
        self.assertIn('id="mi-stype"', html)

        for params in ({'content_id': 'mi_issues'},
                       {'content_id': 'mi_issues', 'status': 'all', 'mine': '1'},
                       {'content_id': 'mi_report'},
                       {'content_id': 'mi_issue', 'issue_id': str(issue['id']), 'created': '1'}):
            status, html = self.content(self.manager, **params)
            self.assertNotIn('error-modal-title', html, params)
            self.assertNotIn('Traceback', html, params)

        status, html = self.content(self.manager, content_id='mi_issue', issue_id=str(issue['id']))
        self.assertIn(issue['ticket'], html)
        self.assertIn('data-action="acknowledge"', html)

    # ------------------------- workflow --------------------------------------
    def act(self, user, issue, action, **extra):
        data = {'action': action, 'version': issue['version']}
        data.update(extra)
        r = self.post(self.client_for(user), '/api/mi/issues/%d/transition' % issue['id'],
                      data, user=user)
        return r, (r.get_json() or {}).get('issue')

    def test_workflow(self):
        issue = self.new_issue()

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

        r, _ = self.act(self.reporter, issue3, 'reopen', reason='Still failing')
        self.assertEqual(r.status_code, 403)  # managers reopen
        r, _ = self.act(self.manager, issue3, 'reopen')
        self.assertEqual(r.status_code, 422)  # reason required
        r, issue4 = self.act(self.manager, issue3, 'reopen', reason='Fails again after reload')
        self.assertEqual(issue4['status'], 'reopened')
        self.assertIsNone(issue4['resolved_at'])
        kinds = [e['kind'] for e in issue4['events']]
        self.assertEqual(kinds, ['created', 'acknowledge', 'resolve', 'reopen'])

    def test_concurrent_save_conflict(self):
        """ A change saved by someone else (or the worker) since the issue was
        loaded is never overwritten. """
        from emhub.issues.store import Conflict
        issue = self.new_issue()
        with self.app.app_context():
            store = self.svc.store
            a, b = store.get_issue(issue['id']), store.get_issue(issue['id'])
            a.title = 'First save'
            store.save_issue(a)
            b.title = 'Second save'
            with self.assertRaises(Conflict):
                store.save_issue(b)
            store.rollback()
        self.assertEqual(self.get_issue(issue['id'])['title'], 'First save')

    def test_update_and_downtime(self):
        issue = self.new_issue()
        url = '/api/mi/issues/%d/update' % issue['id']
        client = self.client_for(self.manager)

        r = self.post(client, url, {'version': issue['version'], 'assignee_id': self.other.id},
                      user=self.manager)
        self.assertEqual(r.status_code, 422)  # not a manager

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

    def test_comments(self):
        issue = self.new_issue()
        url = '/api/mi/issues/%d/comments' % issue['id']

        r = self.post(self.client_for(self.reporter), url, {'text': 'Still drifting'}, user=self.reporter)
        self.assertEqual(r.status_code, 403)

        r = self.post(self.client_for(self.manager), url, {'text': 'Check the vendor log', 'internal': True},
                      user=self.manager)
        self.assertEqual(r.status_code, 201)
        r = self.post(self.client_for(self.manager), url, {'text': 'Please reload the grid'},
                      user=self.manager)
        events = r.get_json()['issue']['events']
        self.assertEqual([(e['data'].get('text'), e['internal']) for e in events if e['kind'] == 'comment'],
                         [('Check the vendor log', True), ('Please reload the grid', False)])
        # Only the reply is emailed to the reporter
        comments = [m for m in self.outbox(issue['id']) if m.kind == 'comment']
        self.assertEqual([(m.channel, m.destination) for m in comments],
                         [('reporter', self.reporter.email)])

    def test_duplicate(self):
        original = self.new_issue(title='Original')
        dup = self.get_issue(self.report(user=self.other, title='Same problem').get_json()['issue']['id'])
        r, dup2 = self.act(self.manager, dup, 'duplicate', duplicate_of=original['ticket'])
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(dup2['status'], 'duplicate')
        self.assertEqual(dup2['duplicate_of'], original['ticket'])
        orig = self.get_issue(original['id'])
        self.assertIn(self.other.name, orig['affected'])
        self.assertIn('duplicate_merged', [e['kind'] for e in orig['events']])
        r, _ = self.act(self.manager, orig, 'duplicate', duplicate_of=original['ticket'])
        self.assertEqual(r.status_code, 422)  # not a duplicate of itself

        # Both reporters hear when the original is resolved
        r, _ = self.act(self.manager, orig, 'resolve', resolution_type='Workaround in place',
                        root_cause='Unknown')
        dests = [m.destination for m in self.outbox(original['id']) if m.kind == 'resolve']
        self.assertIn(self.reporter.email, dests)
        self.assertIn(self.other.email, dests)

    def test_attachments(self):
        issue = self.new_issue()
        url = '/api/mi/issues/%d/attachments' % issue['id']
        png = b'\x89PNG\r\n\x1a\n' + b'0' * 100

        def upload(user, data, name):
            return self.client_for(user).post(
                url, data={'file': (io.BytesIO(data), name)},
                headers={'X-MI-CSRF': 'token-%d' % user.id}, content_type='multipart/form-data')

        r = upload(self.reporter, png, '../../shot.png')
        self.assertEqual(r.status_code, 201)
        self.assertNotIn('attachments', r.get_json()['issue'])  # a receipt only
        self.assertEqual(upload(self.reporter, b'not an image', 'fake.png').status_code, 422)
        self.assertEqual(upload(self.reporter, b'MZ\x90\x00', 'tool.exe').status_code, 422)
        self.assertEqual(upload(self.other, png, 'x.png').status_code, 403)  # not their report
        r = upload(self.manager, b'vendor log line\n', 'vendor.log')
        self.assertEqual(r.status_code, 201)

        files = r.get_json()['issue']['attachments']
        self.assertEqual([a['filename'] for a in files], ['shot.png', 'vendor.log'])
        shot = files[0]
        file_url = '/api/mi/issues/%d/attachments/%s' % (issue['id'], shot['id'])
        r = self.client_for(self.manager).get(file_url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, png)
        self.assertEqual(r.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(self.client_for(self.reporter).get(file_url).status_code, 403)
        self.assertEqual(self.client_for(self.manager).get(
            '/api/mi/issues/%d/attachments/nope' % issue['id']).status_code, 404)

    def test_notices(self):
        data = {'instrument_id': self.krios.id, 'message': 'Krios01 cryo-cycle until Friday'}
        r = self.post(self.client_for(self.reporter), '/api/mi/notices', data, user=self.reporter)
        self.assertEqual(r.status_code, 403)
        r = self.post(self.client_for(self.manager), '/api/mi/notices', data, user=self.manager)
        self.assertEqual(r.status_code, 201)
        notice = r.get_json()['notice']
        msgs = lambda iid: [n['message'] for n in self.client_for(self.reporter).get(
            '/api/mi/notices?instrument_id=%d' % iid).get_json()['notices']]
        self.assertIn(data['message'], msgs(self.krios.id))  # everyone sees notices
        self.assertNotIn(data['message'], msgs(self.vitrobot.id))
        self.post(self.client_for(self.manager), '/api/mi/notices/%d/remove' % notice['id'], {}, user=self.manager)
        self.assertNotIn(data['message'], msgs(self.krios.id))

    # ------------------------- notifications ---------------------------------
    def with_settings(self, settings):
        client = self.client_for(self.admin)
        r = self.post(client, '/api/mi/settings', settings, user=self.admin)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.addCleanup(self.post, client, '/api/mi/settings', {}, self.admin)

    def test_settings_validation(self):
        bad = {'email': ['not-an-email'], 'webhooks': ['unknown-hook'],
               'routes': [{'instruments': ['NoSuchScope'], 'min_severity': 'stop'}]}
        r = self.post(self.client_for(self.admin), '/api/mi/settings', bad, user=self.admin)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(set(r.get_json()['errors']), {'email', 'webhooks', 'routes.0'})
        r = self.post(self.client_for(self.manager), '/api/mi/settings', {}, user=self.manager)
        self.assertEqual(r.status_code, 403)  # admins only

    def run_worker(self, worker):
        while worker.run_once():
            pass

    def test_routing_and_worker(self):
        from emhub.issues.worker import NotificationWorker, RetryLater, PermanentError
        self.with_settings({'email': ['facility@example.org'], 'webhooks': ['teams-cryoem'],
                            'routes': [{'instruments': ['Krios01'], 'min_severity': 'blocked',
                                        'email': ['krios-team@example.org']}]})
        quiet = self.new_issue(severity='question', title='Routing quiet')
        urgent = self.new_issue(severity='stop', title='Routing urgent')
        dests = lambda i: sorted(m.destination for m in self.outbox(i['id']))
        self.assertEqual(dests(quiet), sorted(['facility@example.org', 'teams-cryoem', self.reporter.email]))
        self.assertIn('krios-team@example.org', dests(urgent))

        sent, posted = [], []
        worker = NotificationWorker(self.app, send_email=lambda *a: sent.append(a),
                                    post_webhook=lambda *a: posted.append(a))
        self.run_worker(worker)
        self.assertTrue(all(m.status == 'accepted' for m in self.outbox(urgent['id'])))
        to_team = [(s, b) for to, s, b in sent if to == 'krios-team@example.org']
        self.assertTrue(to_team[0][0].startswith('[%s] New issue - Stop now - Krios01' % urgent['ticket']))
        self.assertIn('https://emhub.example.org/main?content_id=mi_issue&issue_id=%d' % urgent['id'],
                      to_team[0][1])
        # The reporter's receipt has no link: reporters cannot open issues
        receipt = [(s, b) for to, s, b in sent
                   if to == self.reporter.email and urgent['ticket'] in s][0]
        self.assertIn('We received your report', receipt[0])
        self.assertNotIn('content_id=mi_issue', receipt[1])
        url, card = posted[-1]
        self.assertEqual(url, 'https://example.invalid/hook')
        self.assertEqual(card['attachments'][0]['contentType'], 'application/vnd.microsoft.card.adaptive')

        # Failures: transient errors are retried later, permanent ones fail
        failing = self.new_issue(title='Delivery failure')

        def flaky_email(to, subject, body):
            raise RetryLater('SMTP down')

        def broken_hook(url, payload):
            raise PermanentError('HTTP 404')

        worker = NotificationWorker(self.app, send_email=flaky_email, post_webhook=broken_hook)
        worker.run_once()
        status = {m.channel: m for m in self.outbox(failing['id'])}
        self.assertEqual(status['email'].status, 'retrying')
        self.assertGreater(status['email'].next_attempt_at, dt.datetime.now(dt.timezone.utc))
        self.assertEqual(status['webhook'].status, 'failed')
        # The report itself is untouched by delivery failures
        self.assertEqual(self.get_issue(failing['id'])['title'], 'Delivery failure')

        r = self.post(self.client_for(self.admin), '/api/mi/outbox/%d/retry' % status['webhook'].id, {},
                      user=self.admin)
        self.assertEqual(r.get_json()['status'], 'queued')

    def test_progress_emails(self):
        from emhub.issues.worker import NotificationWorker
        issue = self.new_issue()
        r, issue = self.act(self.manager, issue, 'acknowledge')
        msgs = [(m.kind, m.channel, m.destination) for m in self.outbox(issue['id'])]
        self.assertIn(('acknowledge', 'reporter', self.reporter.email), msgs)
        # No email to the person who did the action
        self.assertNotIn(self.manager.email, [m[2] for m in msgs if m[0] == 'acknowledge'])
        sent = []
        self.run_worker(NotificationWorker(self.app, send_email=lambda *a: sent.append(a),
                                           post_webhook=lambda *a: None))
        body = [b for to, s, b in sent if to == self.reporter.email
                and s.startswith('[%s] Acknowledged' % issue['ticket'])][0]
        self.assertIn('The facility staff are looking at your report.', body)
        self.assertNotIn('content_id=mi_issue', body)

    def test_escalation(self):
        self.with_settings({'escalation': {'email': ['oncall@example.org']},
                            'ack_minutes': {'stop': 2}})
        issue = self.new_issue(severity='stop', title='Escalate me')
        with self.app.app_context():
            soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1)
            self.svc.escalate_overdue(now=soon)
            later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=3)
            self.assertGreaterEqual(self.svc.escalate_overdue(now=later), 1)
            self.svc.escalate_overdue(now=later)  # only once
        msgs = [(m.kind, m.destination) for m in self.outbox(issue['id'])]
        self.assertEqual(msgs.count(('escalated', 'oncall@example.org')), 1)

        acked = self.new_issue(severity='stop', title='Acknowledged in time')
        self.act(self.manager, acked, 'acknowledge')
        with self.app.app_context():
            self.svc.escalate_overdue(now=later)
        self.assertNotIn('escalated', [m.kind for m in self.outbox(acked['id'])])

    def test_settings_page(self):
        status, html = self.content(self.admin, content_id='mi_settings')
        self.assertNotIn('error-modal-title', html)
        self.assertIn('teams-cryoem', html)
        for user in (self.reporter, self.manager):
            status, html = self.content(user, content_id='mi_settings')
            self.assertIn('error-modal-title', html)

    # ------------------------- insights --------------------------------------
    def test_insights_page_and_access(self):
        self.new_issue(title='For insights')
        status, html = self.content(self.manager, content_id='mi_insights')
        self.assertNotIn('error-modal-title', html)
        self.assertIn('Incidents per week', html)
        status, html = self.content(self.manager, content_id='mi_insights', start='2026-13-01')
        self.assertIn('error-modal-title', html)

    def test_csv_export(self):
        self.new_issue(title='=HYPERLINK("http://evil")', contact='lab')
        r = self.client_for(self.manager).get('/api/mi/export.csv')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, 'text/csv')
        import csv as _csv
        rows = list(_csv.DictReader(io.StringIO(r.get_data(as_text=True).lstrip('﻿'))))
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
            lb = dm.create_project(title='Krios01 logbook', status='special:logbook',
                                   validate=False, extra={'resource_id': self.krios.resource_id})
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
            before = len(self.svc.store.issues())
            entries = len(dm.get_entries())
            report = import_logbooks(self.svc, [lb_id, orphan_id], dry_run=True)
            self.assertEqual((report['imported'], report['skipped']), (2, 1))
            self.assertEqual(len(self.svc.store.issues()), before)  # dry run saves nothing
            self.assertEqual(len(dm.get_entries()), entries)
            report = import_logbooks(self.svc, [lb_id, orphan_id], dry_run=False)
            self.assertEqual(report['imported'], 2)
            again = import_logbooks(self.svc, [lb_id], dry_run=False)
            self.assertEqual((again['imported'], again['logbooks'][0]['already']), (0, 2))
            # The logbooks themselves are not changed
            self.assertEqual(len(dm.get_project_by(id=lb_id).entries), 2)

        issues = self.client_for(self.manager).get('/api/mi/issues?status=all').get_json()['issues']
        k3 = [i for i in issues if i['title'] == 'K3 not ready'][0]
        noise = [i for i in issues if i['title'] == 'Stage noise'][0]
        self.assertEqual((k3['status'], k3['severity']), ('closed', 'stop'))
        self.assertEqual((noise['status'], noise['severity']), ('acknowledged', 'degraded'))
        self.assertTrue(k3['imported'])
        detail = self.get_issue(k3['id'])
        self.assertEqual([e['kind'] for e in detail['events']], ['imported', 'comment', 'comment'])
        self.assertTrue(all(e['internal'] for e in detail['events']))
        # Imported history never notifies anyone
        self.assertEqual(self.outbox(k3['id']), [])


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
