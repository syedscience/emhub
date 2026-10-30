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
Microscope Issues module for EMhub.

Lets facility users report instrument problems from EMhub and gives the EMhub
managers a workflow (acknowledge, investigate, resolve) plus notifications
and insights. Only managers can see the issues, their history and the
insights; other users can report and get email updates.

The module is optional and disabled by default. Enable it in the instance
config.py with:

    EMHUB_ISSUES = True

Issues are stored in the EMhub database, in a hidden project (status
'special:issues') created on first use, like EMhub logbooks. No extra
database or migration is needed. Attachments are saved in the
'issue_files' folder of the instance (or ISSUES_FILES).
"""


def init_issues(app):
    """ Wire the Microscope Issues module into the EMhub app.
    Called from create_app when EMHUB_ISSUES is enabled.
    """
    from .store import IssueStore
    from .service import IssuesService
    from .api import issues_bp, csrf_token
    from .content import register_content
    from . import guard

    store = IssueStore(app.dm)
    app.issues = IssuesService(app, store)
    app.register_blueprint(issues_bp, url_prefix='/api/mi')
    app.jinja_env.globals['mi_csrf_token'] = csrf_token
    register_content(app.dc)
    guard.install(app, store)
    return app.issues
