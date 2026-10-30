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
Keep EMhub's generic project and entry routes away from the issues project.

EMhub's /api/get_entries, /api/update_entry, the entry_form page and similar
routes do not check who may see or change an entry. The issues project holds
reporter details and staff notes, so for it:

  - any request outside the module that names the issues project or one of
    its entries (project_id, entry_id, entry, id...) is refused, for every
    user: the module pages are the only way in;
  - the issues project and its entries are removed from the results of
    /api/get_projects and /api/get_entries.
"""

import json

import flask

ID_KEYS = ('project_id', 'entry_project_id', 'pid')
ENTRY_KEYS = ('entry_id', 'entry')
ENTRY_ENDPOINTS = {'api.create_entry', 'api.update_entry', 'api.update_entry_extra',
                   'api.delete_entry'}
PROJECT_ENDPOINTS = {'api.update_project', 'api.delete_project'}


def _request_values():
    """ All parameters of the request: query string, form, JSON body and
    EMhub's 'attrs' wrapper (JSON or form encoded). """
    req = flask.request
    values = [req.args.to_dict(), req.form.to_dict()]
    body = req.get_json(silent=True)
    if isinstance(body, dict):
        values.append(body)
    for d in list(values):
        attrs = d.get('attrs')
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except ValueError:
                attrs = None
        if isinstance(attrs, dict):
            values.append(attrs)
    return values


def install(app, store):

    def _forbidden():
        msg = 'This item is managed by the Microscope Issues module.'
        if flask.request.blueprint == 'api' or flask.request.is_json:
            return flask.jsonify({'error': msg}), 403
        flask.abort(403, msg)

    @app.before_request
    def _guard_issues_project():
        req = flask.request
        if req.blueprint == 'issues_api' or req.endpoint in (None, 'static'):
            return None
        for d in _request_values():
            if str(d.get('content_id', '')).startswith('mi_'):
                continue  # the module's own pages
            if any(store.owns_project(d[k]) for k in ID_KEYS if k in d):
                return _forbidden()
            if any(store.owns_entry(d[k]) for k in ENTRY_KEYS if k in d):
                return _forbidden()
            if 'id' in d:
                if req.endpoint in ENTRY_ENDPOINTS and store.owns_entry(d['id']):
                    return _forbidden()
                if req.endpoint in PROJECT_ENDPOINTS and store.owns_project(d['id']):
                    return _forbidden()
        return None

    def _filtered(view, keep):
        def wrapper(*args, **kwargs):
            resp = flask.make_response(view(*args, **kwargs))
            pid = store.existing_project_id()
            if pid is None or resp.status_code != 200:
                return resp
            try:
                items = json.loads(resp.get_data(as_text=True))
            except ValueError:
                return resp
            if isinstance(items, list):
                resp.set_data(json.dumps([i for i in items
                                          if not isinstance(i, dict) or keep(i, pid)]))
            return resp
        wrapper.__name__ = view.__name__
        return wrapper

    views = app.view_functions
    if 'api.get_entries' in views:
        views['api.get_entries'] = _filtered(
            views['api.get_entries'], lambda e, pid: e.get('project_id') != pid)
    if 'api.get_projects' in views:
        views['api.get_projects'] = _filtered(
            views['api.get_projects'], lambda p, pid: p.get('id') != pid)
