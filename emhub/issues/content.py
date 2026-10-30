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
EMhub pages (content_id -> template) of the Microscope Issues module:

    mi_report    Report a new issue (optional resource_id preselects it);
                 the Microscope Issues tab for everyone but managers
    mi_issues    Issue list (managers), their landing page of the tab
    mi_issue     Issue detail, history, comments, files and actions (managers)
    mi_insights  Insights (managers): trends, response, downtime, backlog
    mi_settings  Admin settings: notifications, instruments, delivery health
    mi_catalog   Problem areas, symptoms and waiting advice (managers)
"""

import datetime as dt

import flask_login

from .api import csrf_token
from .authz import MANAGER, ADMIN, require


def register_content(dc):

    def _svc():
        if getattr(dc.app, 'issues', None) is None:
            raise Exception('The Microscope Issues module is not enabled.')
        dc.check_user_access('microscope_issues')
        return dc.app.issues

    def _page(svc):
        """ What the shared page header and tabs need. """
        roles = svc.roles()
        return {'is_manager': MANAGER in roles, 'is_admin': ADMIN in roles,
                'facility_name': svc.config().get('facility_name') or 'Cryo-EM Facility'}

    def _int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @dc.content
    def mi_issues(**kwargs):
        svc = _svc()
        roles = svc.roles()
        require(roles, MANAGER)
        status = kwargs.get('status', 'open')
        instrument_id = _int(kwargs.get('instrument_id'))
        mine = kwargs.get('mine') in ('1', 'true')
        assigned = kwargs.get('assigned')
        issues = svc.list_issues(status=None if status == 'all' else status,
                                 instrument_id=instrument_id, mine=mine,
                                 assigned=assigned)
        instruments = svc.instruments(reportable_only=False)
        open_issues = svc.list_issues(status='open')
        me = svc.app.user.id
        return {
            **_page(svc),
            'stats': {
                'open': len(open_issues),
                'urgent': sum(1 for i in open_issues if i.severity in ('stop', 'blocked')),
                'new': sum(1 for i in open_issues if i.status in ('new', 'reopened')),
                'mine': sum(1 for i in open_issues if i.assignee_id == me),
            },
            'urgent_counts': {i.instrument_id: True for i in open_issues
                              if i.severity in ('stop', 'blocked')},
            'issues': [svc.issue_json(i) for i in issues],
            'instruments': [i for i in instruments if i.is_reportable],
            'open_counts': svc.counts(),
            'notices': [svc.notice_json(n) for n in svc.notices()],
            'filters': {'status': status, 'instrument_id': instrument_id,
                        'mine': mine, 'assigned': assigned},
            'severities': svc.severities(),
            'areas': {a['id']: a['label'] for a in svc.problem_areas(include_hidden=True)},
            'csrf_token': csrf_token(),
        }

    @dc.content
    def mi_insights(**kwargs):
        svc = _svc()
        require(svc.roles(), MANAGER)
        start, end = svc.parse_period(kwargs.get('start') or None, kwargs.get('end') or None,
                                      days=_int(kwargs.get('days')) or 90)
        instrument_id = _int(kwargs.get('instrument_id'))
        include_imported = kwargs.get('imported', '1') not in ('0', 'false')
        data = svc.insights(start, end, instrument_id, include_imported)
        tz = dc.app.dm.timezone
        filters = {'start': start.astimezone(tz).date().isoformat(),
                   'end': (end.astimezone(tz) - dt.timedelta(days=1)).date().isoformat(),
                   'instrument_id': instrument_id or '',
                   'imported': '1' if include_imported else '0'}
        return {
            **_page(svc),
            'data': data,
            'filters': filters,
            'instruments': svc.instruments(reportable_only=False),
            'severities': svc.severities(),
            'outbox': svc.outbox_summary(check_access=False),
        }

    @dc.content
    def mi_report(**kwargs):
        svc = _svc()
        selected = None
        if resource_id := _int(kwargs.get('resource_id')):
            inst = svc.get_instrument(resource_id)
            selected = inst.id if inst and inst.is_reportable else None
        roles, config = svc.roles(), svc.config()
        user = flask_login.current_user
        pi = user.get_pi() if user.is_authenticated else None
        return {
            'selected_instrument_id': selected,
            # Shown read-only: who is reporting comes from the EMhub login
            'reporter': {'name': user.name, 'email': user.email or '',
                         'pi': pi.name if pi and pi != user else ''},
            # Ticket of the report just sent, shown as a confirmation
            'submitted': str(kwargs.get('submitted') or '')[:32],
            'is_manager': MANAGER in roles,
            'is_admin': ADMIN in roles,
            'facility_name': config.get('facility_name') or 'Cryo-EM Facility',
            'staff_phone': config.get('staff_phone') or '',
            'csrf_token': csrf_token(),
        }

    @dc.content
    def mi_issue(**kwargs):
        svc = _svc()
        issue = svc.get_issue(kwargs.get('issue_id'))
        data = svc.issue_json(issue, detail=True)
        kb = svc.knowledge()
        return {
            **_page(svc),
            'issue': data,
            'created': kwargs.get('created') == '1' and data['is_mine'],
            'severities': svc.severities(),
            'areas': {a['id']: a['label'] for a in svc.problem_areas(include_hidden=True)},
            'managers': svc.manager_users(),
            'resolution_types': kb['resolution_types'],
            'root_causes': kb['root_causes'],
            'notices': [svc.notice_json(n) for n in svc.notices(issue.instrument_id)],
            'csrf_token': csrf_token(),
        }

    @dc.content
    def mi_catalog(**kwargs):
        svc = _svc()
        roles = svc.roles()
        require(roles, MANAGER)
        return {
            **_page(svc),
            'selected_area': str(kwargs.get('area') or ''),
            'csrf_token': csrf_token(),
        }

    @dc.content
    def mi_settings(**kwargs):
        svc = _svc()
        require(svc.roles(), MANAGER)
        outbox = svc.outbox_summary()
        return {
            **_page(svc),
            'notifications': svc.notification_settings(),
            'webhooks': svc.webhook_names(),
            'instruments': svc.instruments(reportable_only=False),
            'severities': svc.severities(),
            'outbox': outbox,
            'csrf_token': csrf_token(),
        }
