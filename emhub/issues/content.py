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

    mi_issues    Issue list, the landing page of the Microscope Issues tab
    mi_report    Report a new issue (optional resource_id preselects it)
    mi_issue     Issue detail, history, comments, files and staff actions
    mi_settings  Admin settings: notifications, instruments, delivery health
"""

from .api import csrf_token
from .authz import STAFF, ADMIN, require


def register_content(dc):

    def _svc():
        if getattr(dc.app, 'issues', None) is None:
            raise Exception('The Microscope Issues module is not enabled.')
        dc.check_user_access('microscope_issues')
        return dc.app.issues

    def _int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @dc.content
    def mi_issues(**kwargs):
        svc = _svc()
        roles = svc.roles()
        status = kwargs.get('status', 'open')
        instrument_id = _int(kwargs.get('instrument_id'))
        mine = kwargs.get('mine') in ('1', 'true')
        assigned = kwargs.get('assigned') if STAFF in roles else None
        issues = svc.list_issues(status=None if status == 'all' else status,
                                 instrument_id=instrument_id, mine=mine,
                                 assigned=assigned)
        instruments = svc.instruments(reportable_only=False)
        return {
            'issues': [svc.issue_json(i) for i in issues],
            'instruments': [i for i in instruments if i.is_reportable],
            'open_counts': svc.counts(),
            'notices': [svc.notice_json(n) for n in svc.notices()],
            'filters': {'status': status, 'instrument_id': instrument_id,
                        'mine': mine, 'assigned': assigned},
            'severities': svc.severities(),
            'areas': {a['id']: a['label'] for a in svc.problem_areas()},
            'is_staff': STAFF in roles,
            'is_admin': ADMIN in roles,
            'csrf_token': csrf_token(),
        }

    @dc.content
    def mi_report(**kwargs):
        svc = _svc()
        selected = None
        if resource_id := _int(kwargs.get('resource_id')):
            svc.sync_instruments()
            inst = svc.get_instrument(resource_id=resource_id)
            selected = inst.id if inst and inst.is_reportable else None
        return {
            'selected_instrument_id': selected,
            'csrf_token': csrf_token(),
        }

    @dc.content
    def mi_issue(**kwargs):
        svc = _svc()
        roles = svc.roles()
        issue = svc.get_issue(kwargs.get('issue_id'))
        data = svc.issue_json(issue, detail=True)
        kb = svc.knowledge()
        return {
            'issue': data,
            'created': kwargs.get('created') == '1' and data['is_mine'],
            'severities': svc.severities(),
            'areas': {a['id']: a['label'] for a in svc.problem_areas()},
            'staff': svc.staff_users() if STAFF in roles else [],
            'resolution_types': kb['resolution_types'],
            'root_causes': kb['root_causes'],
            'notices': [svc.notice_json(n) for n in svc.notices(issue.instrument_id)],
            'is_staff': STAFF in roles,
            'csrf_token': csrf_token(),
        }

    @dc.content
    def mi_settings(**kwargs):
        svc = _svc()
        require(svc.roles(), ADMIN)
        outbox = svc.outbox_summary()
        return {
            'notifications': svc.notification_settings(),
            'webhooks': svc.webhook_names(),
            'instruments': svc.instruments(reportable_only=False),
            'severities': svc.severities(),
            'outbox': outbox,
            'csrf_token': csrf_token(),
        }
