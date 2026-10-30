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
Command line tool for the Microscope Issues module.

    emh-issues migrate            Create or upgrade the issues database schema
    emh-issues status             Show database URL and schema revision
    emh-issues sync-instruments   Copy EMhub resources into the issues database
    emh-issues init-config        Create the 'config:issues' form with defaults
    emh-issues worker [--once]    Send notifications and escalate overdue issues

EMHUB_INSTANCE must point to the EMhub instance folder.
"""

import argparse
import sys


def _app():
    from emhub import create_app
    return create_app()


def _db(app):
    from . import get_db_url
    from .db import IssuesDb
    return IssuesDb(get_db_url(app))


def _safe_url(url):
    """ Hide the password when printing a database URL. """
    from sqlalchemy.engine import make_url
    return make_url(url).render_as_string(hide_password=True)


def main():
    p = argparse.ArgumentParser(prog='emh-issues')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('migrate', help='Create or upgrade the database schema.')
    sub.add_parser('status', help='Show the database and schema revision.')
    sub.add_parser('sync-instruments', help='Copy EMhub resources as instruments.')
    sub.add_parser('init-config', help="Create the 'config:issues' form with defaults.")
    w = sub.add_parser('worker', help='Send notifications and escalate overdue issues.')
    w.add_argument('--once', action='store_true', help='Process once and exit.')
    w.add_argument('--interval', type=int, default=10, help='Seconds between runs.')
    args = p.parse_args()

    app = _app()
    db = _db(app)

    if args.command == 'migrate':
        db.migrate()
        print(f"Issues database at revision {db.current_revision()}: {_safe_url(db.url)}")

    elif args.command == 'status':
        current, head = db.current_revision(), db.head_revision()
        print(f"Database: {_safe_url(db.url)}")
        print(f"Enabled:  {bool(app.config.get('EMHUB_ISSUES', False))}")
        print(f"Revision: {current} (latest: {head})")
        if current != head:
            print("Run 'emh-issues migrate' to upgrade.")
            sys.exit(1)

    elif args.command == 'sync-instruments':
        if app.issues is None:
            sys.exit("Set EMHUB_ISSUES = True in the instance config.py first.")
        with app.app_context():
            app.issues.sync_instruments()
            for inst in app.issues.instruments(reportable_only=False, sync=False):
                state = 'reportable' if inst.is_reportable else 'not reportable'
                print(f"{inst.resource_id:4d}  {inst.name:24s} {inst.instrument_type:6s} {state}")

    elif args.command == 'init-config':
        from .service import DEFAULT_CONFIG
        import flask_login
        with app.test_request_context():
            if app.dm.get_form_by(name='config:issues') is not None:
                print("Form 'config:issues' already exists, nothing changed.")
                return
            # EMhub logs who changes the database, so act as an admin user
            admin = next(u for u in app.dm.get_users() if u.is_admin)
            flask_login.login_user(admin)
            app.dm.create_form(name='config:issues', definition=DEFAULT_CONFIG)
            print(f"Created form 'config:issues' (as admin '{admin.username}'). "
                  f"Edit it in EMhub under Forms.")

    elif args.command == 'worker':
        import logging
        from .worker import NotificationWorker
        if app.issues is None:
            sys.exit("Set EMHUB_ISSUES = True in the instance config.py first.")
        if not db.is_current():
            sys.exit("The issues database is not up to date, run 'emh-issues migrate'.")
        logging.basicConfig(level=logging.INFO,
                            format='%(asctime)s %(levelname)s %(message)s')
        worker = NotificationWorker(app)
        if args.once:
            print(worker.run_once())
        else:
            worker.run_forever(args.interval)


if __name__ == '__main__':
    main()
