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

Lets facility users report instrument problems from EMhub and gives staff a
workflow (acknowledge, investigate, resolve) plus notifications and insights.

The module is optional and disabled by default. Enable it in the instance
config.py with:

    EMHUB_ISSUES = True
    ISSUES_DB_URL = 'postgresql+psycopg://user:pass@host/emhub_issues'

If ISSUES_DB_URL is not set, an SQLite file 'emhub-issues.sqlite' in the
instance folder is used (fine for testing and small facilities).
Create or upgrade the database schema with:  emh-issues migrate
"""

import os


def get_db_url(app):
    """ Return the issues database URL from the config or environment. """
    url = app.config.get('ISSUES_DB_URL') or os.environ.get('ISSUES_DB_URL')
    if not url:
        url = 'sqlite:///' + os.path.join(app.instance_path, 'emhub-issues.sqlite')
    return url


def init_issues(app):
    """ Wire the Microscope Issues module into the EMhub app.
    Called from create_app when EMHUB_ISSUES is enabled.
    """
    from .db import IssuesDb
    from .service import IssuesService
    from .api import issues_bp, csrf_token
    from .content import register_content

    app.issues = IssuesService(app, IssuesDb(get_db_url(app)))
    app.register_blueprint(issues_bp, url_prefix='/api/mi')
    app.jinja_env.globals['mi_csrf_token'] = csrf_token
    register_content(app.dc)

    @app.teardown_appcontext
    def shutdown_issues_session(exception=None):
        app.issues.db.remove()

    return app.issues
