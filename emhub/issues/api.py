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
JSON API of the Microscope Issues module, mounted at /api/mi.

Every endpoint requires an authenticated EMhub session. Requests that change
data must send the session CSRF token in the 'X-MI-CSRF' header (templates get
it from mi_csrf_token()). Errors use proper HTTP status codes and a JSON body
{"error": message, "errors": {field: message}}.
"""

import csv
import datetime as dt
import hmac
import io
import secrets

import flask
import flask_login

from .authz import Forbidden, MANAGER, require
from .service import ValidationError, NotFound, Conflict

issues_bp = flask.Blueprint('issues_api', __name__)

CSRF_HEADER = 'X-MI-CSRF'


def csrf_token():
    token = flask.session.get('mi_csrf')
    if not token:
        token = flask.session['mi_csrf'] = secrets.token_urlsafe(32)
    return token


def _service():
    return flask.current_app.issues


def _error(msg, status, errors=None):
    body = {'error': msg}
    if errors:
        body['errors'] = errors
    return flask.jsonify(body), status


@issues_bp.before_request
def _check_request():
    if not flask_login.current_user.is_authenticated:
        return _error('Login required.', 401)
    if flask.request.method not in ('GET', 'HEAD', 'OPTIONS'):
        sent = flask.request.headers.get(CSRF_HEADER, '')
        expected = flask.session.get('mi_csrf', '')
        if not expected or not hmac.compare_digest(sent, expected):
            return _error('Invalid or missing CSRF token.', 403)


@issues_bp.errorhandler(Forbidden)
def _forbidden(e):
    return _error(str(e), 403)


@issues_bp.errorhandler(NotFound)
def _not_found(e):
    return _error(str(e), 404)


@issues_bp.errorhandler(ValidationError)
def _invalid(e):
    return _error('Please correct the highlighted fields.', 422, e.errors)


@issues_bp.errorhandler(Conflict)
def _conflict(e):
    _service().store.rollback()
    return _error(str(e) or 'This issue was changed by someone else. '
                  'Reload it and try again.', 409)


def _json_body():
    data = flask.request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError({'body': 'Expected a JSON object.'})
    return data


@issues_bp.route('/meta', methods=['GET'])
def meta():
    """ Everything the report form needs: instruments, problem areas,
    severities, the user's issue roles and the CSRF token. """
    svc = _service()
    roles = svc.roles()
    kb = svc.knowledge()
    instruments = []
    for inst in svc.instruments():
        d = inst.json()
        areas = svc.problem_areas(inst)
        d['problem_areas'] = [a['id'] for a in areas]
        # Symptoms can be limited to some instruments too
        d['symptoms'] = {a['id']: [sy['id'] for sy in a.get('symptoms', [])] for a in areas}
        instruments.append(d)
    return flask.jsonify({
        'instruments': instruments,
        'problem_areas': kb['problem_areas'],
        'severities': kb['severities'],
        'context_options': kb.get('context_options', {}),
        'instrument_groups': kb.get('instrument_groups', []),
        'roles': sorted(roles),
        'csrf_token': csrf_token(),
    })


@issues_bp.route('/issues', methods=['GET'])
def list_issues():
    svc = _service()
    args = flask.request.args
    try:
        instrument_id = int(args['instrument_id']) if args.get('instrument_id') else None
    except ValueError:
        raise ValidationError({'instrument_id': 'Must be a number.'})
    status = args.get('status')
    issues = svc.list_issues(status=None if status in (None, '', 'all') else status,
                             instrument_id=instrument_id,
                             mine=args.get('mine') in ('1', 'true'))
    return flask.jsonify({'issues': [svc.issue_json(i) for i in issues]})


@issues_bp.route('/issues', methods=['POST'])
def create_issue():
    svc = _service()
    issue = svc.create_issue(_json_body())
    return flask.jsonify({'issue': _issue_or_receipt(svc, issue)}), 201


def _issue_or_receipt(svc, issue):
    """ Managers get the issue, reporters only the receipt. """
    if MANAGER in svc.roles():
        return svc.issue_json(issue, detail=True)
    return svc.receipt_json(issue)


@issues_bp.route('/issues/<int:issue_id>', methods=['GET'])
def get_issue(issue_id):
    svc = _service()
    issue = svc.get_issue(issue_id)
    return flask.jsonify({'issue': svc.issue_json(issue, detail=True)})


@issues_bp.route('/instruments', methods=['GET'])
def list_instruments():
    svc = _service()
    show_all = flask.request.args.get('all') in ('1', 'true') and MANAGER in svc.roles()
    return flask.jsonify({'instruments': [i.json() for i in
                                          svc.instruments(reportable_only=not show_all)]})


@issues_bp.route('/instruments/<int:instrument_id>', methods=['POST'])
def update_instrument(instrument_id):
    data = _json_body()
    if 'enabled' not in data:
        raise ValidationError({'enabled': 'Required.'})
    inst = _service().set_instrument_enabled(instrument_id, data['enabled'])
    return flask.jsonify({'instrument': inst.json()})


@issues_bp.route('/issues/<int:issue_id>/transition', methods=['POST'])
def transition(issue_id):
    svc = _service()
    data = _json_body()
    issue = svc.transition(issue_id, data.get('action'), data)
    return flask.jsonify({'issue': svc.issue_json(issue, detail=True)})


@issues_bp.route('/issues/<int:issue_id>/update', methods=['POST'])
def update_issue(issue_id):
    svc = _service()
    issue = svc.update_issue(issue_id, _json_body())
    return flask.jsonify({'issue': svc.issue_json(issue, detail=True)})


@issues_bp.route('/instruments/<int:instrument_id>/open_issues', methods=['GET'])
def open_on_instrument(instrument_id):
    """ Open issues on an instrument, shown on the report form. """
    return flask.jsonify({'issues': _service().open_on_instrument(instrument_id)})


@issues_bp.route('/issues/<int:issue_id>/photo_link', methods=['GET'])
def photo_link(issue_id):
    """ Link and QR code to add photos to one's own report from a phone. """
    from . import qr
    svc = _service()
    svc.own_issue(issue_id)  # reporters only get links to their own reports
    url = (qr.base_url(flask.current_app) +
           f'/main?content_id=mi_add_photos&issue_id={issue_id}')
    return flask.jsonify({'url': url, 'svg': qr.svg(url, scale=4, title='Add photos')})


@issues_bp.route('/issues/<int:issue_id>/join', methods=['POST'])
def join_issue(issue_id):
    """ 'I have this too' from the report form. """
    svc = _service()
    svc.join_issue(issue_id)
    return flask.jsonify({'issue': next(i for i in svc.open_on_instrument(
        svc._issue(issue_id).instrument_id) if i['id'] == issue_id)})


@issues_bp.route('/issues/<int:issue_id>/comments', methods=['POST'])
def add_comment(issue_id):
    svc = _service()
    data = _json_body()
    svc.add_comment(issue_id, data.get('text'), bool(data.get('internal')))
    return flask.jsonify({'issue': svc.issue_json(svc.get_issue(issue_id), detail=True)}), 201


@issues_bp.route('/issues/<int:issue_id>/attachments', methods=['POST'])
def add_attachments(issue_id):
    svc = _service()
    files = flask.request.files.getlist('file')
    if not files:
        raise ValidationError({'file': 'Select a file to upload.'})
    internal = flask.request.form.get('internal') in ('1', 'true')
    for f in files:
        svc.add_attachment(issue_id, f.filename, f.stream, internal=internal)
    return flask.jsonify({'issue': _issue_or_receipt(svc, svc.store.get_issue(issue_id))}), 201


@issues_bp.route('/issues/<int:issue_id>/attachments/<attachment_id>', methods=['GET'])
def get_attachment(issue_id, attachment_id):
    att, path = _service().get_attachment(issue_id, attachment_id)
    inline = att.content_type.startswith('image/') or att.content_type == 'application/pdf'
    resp = flask.send_file(path, mimetype=att.content_type,
                           as_attachment=not inline, download_name=att.filename)
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Content-Security-Policy'] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    resp.headers['Cache-Control'] = 'private, no-store'
    return resp


@issues_bp.route('/managers', methods=['GET'])
def managers():
    svc = _service()
    require(svc.roles(), MANAGER)
    return flask.jsonify({'managers': [{'id': u['id'], 'name': u['name']}
                                       for u in svc.manager_users()]})


@issues_bp.route('/notices', methods=['GET'])
def notices():
    svc = _service()
    return flask.jsonify({'notices': [svc.notice_json(n) for n in
                                      svc.notices(flask.request.args.get('instrument_id'))]})


@issues_bp.route('/notices', methods=['POST'])
def create_notice():
    svc = _service()
    return flask.jsonify({'notice': svc.notice_json(svc.create_notice(_json_body()))}), 201


@issues_bp.route('/notices/<int:notice_id>/remove', methods=['POST'])
def remove_notice(notice_id):
    svc = _service()
    return flask.jsonify({'notice': svc.notice_json(svc.deactivate_notice(notice_id))})


@issues_bp.route('/catalog', methods=['GET'])
def catalog():
    """ The problem areas, for the editor (managers). """
    return flask.jsonify(_service().catalog_json())


@issues_bp.route('/catalog/areas', methods=['POST'])
def save_area():
    svc = _service()
    area = svc.save_area(_json_body())
    return flask.jsonify({'area': area, 'catalog': svc.catalog_json()})


@issues_bp.route('/catalog/areas/<area_id>/<action>', methods=['POST'])
def change_area(area_id, action):
    svc = _service()
    svc.change_area(area_id, action, _json_body())
    return flask.jsonify({'catalog': svc.catalog_json()})


@issues_bp.route('/settings', methods=['GET'])
def get_settings():
    svc = _service()
    require(svc.roles(), MANAGER)
    return flask.jsonify({'notifications': svc.notification_settings(),
                          'webhooks': svc.webhook_names()})


@issues_bp.route('/settings', methods=['POST'])
def save_settings():
    svc = _service()
    return flask.jsonify({'notifications': svc.save_settings(_json_body())})


@issues_bp.route('/settings/facility', methods=['POST'])
def save_facility():
    return flask.jsonify(_service().save_facility(_json_body()))


@issues_bp.route('/settings/test', methods=['POST'])
def send_test():
    data = _json_body()
    msg = _service().send_test(data.get('channel'), data.get('destination'))
    return flask.jsonify({'message_id': msg.id}), 201


@issues_bp.route('/outbox/<int:message_id>/retry', methods=['POST'])
def retry_message(message_id):
    msg = _service().retry_message(message_id)
    return flask.jsonify({'message_id': msg.id, 'status': msg.status})


def csv_safe(value):
    """ Prevent spreadsheet formula injection in exported cells. """
    value = '' if value is None else str(value)
    if value[:1] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + value
    return value


@issues_bp.route('/export.csv', methods=['GET'])
def export_csv():
    svc = _service()
    args = flask.request.args
    start, end = svc.parse_period(args.get('start') or None, args.get('end') or None)
    try:
        instrument_id = int(args['instrument_id']) if args.get('instrument_id') else None
    except ValueError:
        raise ValidationError({'instrument_id': 'Must be a number.'})
    rows = svc.export_rows(start, end, instrument_id,
                           include_imported=args.get('imported', '1') not in ('0', 'false'))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(svc.EXPORT_COLUMNS)
    for row in rows:
        writer.writerow([csv_safe(row[c]) for c in svc.EXPORT_COLUMNS])
    tz = flask.current_app.dm.timezone
    name = (f"microscope-issues_{start.astimezone(tz):%Y-%m-%d}_"
            f"{end.astimezone(tz) - dt.timedelta(days=1):%Y-%m-%d}.csv")
    resp = flask.Response('\ufeff' + out.getvalue(), mimetype='text/csv')
    resp.headers['Content-Disposition'] = f'attachment; filename="{name}"'
    resp.headers['Cache-Control'] = 'private, no-store'
    return resp
