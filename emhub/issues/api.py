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

import hmac
import secrets

import flask
import flask_login
from sqlalchemy.orm.exc import StaleDataError

from .authz import Forbidden, ADMIN
from .service import ValidationError, NotFound

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


@issues_bp.errorhandler(StaleDataError)
def _conflict(e):
    _service().session.rollback()
    return _error('This issue was changed by someone else. '
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
        d['problem_areas'] = [a['id'] for a in svc.problem_areas(inst)]
        instruments.append(d)
    return flask.jsonify({
        'instruments': instruments,
        'problem_areas': kb['problem_areas'],
        'severities': kb['severities'],
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
    issues = svc.list_issues(status=args.get('status'),
                             instrument_id=instrument_id,
                             mine=args.get('mine') in ('1', 'true'))
    return flask.jsonify({'issues': [svc.issue_json(i) for i in issues]})


@issues_bp.route('/issues', methods=['POST'])
def create_issue():
    svc = _service()
    issue = svc.create_issue(_json_body())
    return flask.jsonify({'issue': svc.issue_json(issue, detail=True)}), 201


@issues_bp.route('/issues/<int:issue_id>', methods=['GET'])
def get_issue(issue_id):
    svc = _service()
    issue = svc.get_issue(issue_id)
    return flask.jsonify({'issue': svc.issue_json(issue, detail=True)})


@issues_bp.route('/instruments', methods=['GET'])
def list_instruments():
    svc = _service()
    show_all = flask.request.args.get('all') in ('1', 'true') and ADMIN in svc.roles()
    return flask.jsonify({'instruments': [i.json() for i in
                                          svc.instruments(reportable_only=not show_all)]})


@issues_bp.route('/instruments/<int:instrument_id>', methods=['POST'])
def update_instrument(instrument_id):
    data = _json_body()
    if 'enabled' not in data:
        raise ValidationError({'enabled': 'Required.'})
    inst = _service().set_instrument_enabled(instrument_id, data['enabled'])
    return flask.jsonify({'instrument': inst.json()})
