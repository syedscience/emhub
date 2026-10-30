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

    emh-issues status             Show where issues are stored and how many
    emh-issues instruments        List the EMhub resources and if they accept reports
    emh-issues init-config        Create the 'config:issues' form with defaults
    emh-issues worker [--once]    Send notifications and escalate overdue issues
    emh-issues import-logbooks 219,220 [--map 223=12] [--commit]
                                  Import EMhub logbook entries as history

EMHUB_INSTANCE must point to the EMhub instance folder.
"""

import argparse
import sys


def _app():
    from emhub import create_app
    return create_app()


def main():
    p = argparse.ArgumentParser(prog='emh-issues')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='Show where issues are stored and how many.')
    sub.add_parser('instruments', help='List the EMhub resources that accept reports.')
    sub.add_parser('init-config', help="Create the 'config:issues' form with defaults.")
    imp = sub.add_parser('import-logbooks', help='Import EMhub logbook entries as history.')
    imp.add_argument('logbooks', help='Comma-separated logbook (project) ids.')
    imp.add_argument('--map', action='append', default=[], metavar='LOGBOOK=RESOURCE',
                     help='Instrument (EMhub resource id) for a logbook without one.')
    imp.add_argument('--commit', action='store_true',
                     help='Save the import. Without it, only a dry-run report is shown.')
    w = sub.add_parser('worker', help='Send notifications and escalate overdue issues.')
    w.add_argument('--once', action='store_true', help='Process once and exit.')
    w.add_argument('--interval', type=int, default=10, help='Seconds between runs.')
    args = p.parse_args()

    app = _app()
    if app.issues is None and args.command != 'init-config':
        sys.exit("Set EMHUB_ISSUES = True in the instance config.py first.")

    if args.command == 'status':
        from .store import PROJECT_STATUS
        with app.app_context():
            store = app.issues.store
            pid = store.existing_project_id()
            print(f"Storage:  EMhub database, project status '{PROJECT_STATUS}'"
                  f" (id {pid if pid else 'not created yet'})")
            if pid:
                issues = store.issues()
                open_ = sum(1 for i in issues if i.is_open)
                pending = len(store.messages(status=['queued', 'retrying', 'sending']))
                failed = len(store.messages(status='failed'))
                print(f"Issues:   {len(issues)} ({open_} open)")
                print(f"Messages: {pending} waiting, {failed} failed")

    elif args.command == 'instruments':
        with app.app_context():
            for inst in app.issues.instruments(reportable_only=False):
                state = 'reportable' if inst.is_reportable else 'not reportable'
                print(f"{inst.id:4d}  {inst.name:24s} {inst.instrument_type:6s} {state}")

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

    elif args.command == 'import-logbooks':
        from .importers import import_logbooks
        try:
            ids = [int(x) for x in args.logbooks.split(',') if x.strip()]
            mapping = {int(k): int(v) for k, v in (m.split('=') for m in args.map)}
        except ValueError:
            sys.exit("Use numeric ids, e.g. 219,220 --map 223=12")
        with app.app_context():
            report = import_logbooks(app.issues, ids, mapping, dry_run=not args.commit)
        for lb in report['logbooks']:
            print(f"Logbook {lb['logbook_id']} ({lb['title']}): {lb['entries']} entries, "
                  f"{lb['imported']} to import, {lb['already']} already imported, "
                  f"{len(lb['skipped'])} skipped")
            for sk in lb['skipped']:
                print(f"    skipped entry {sk['entry']}: {sk['reason']}")
        print(f"Total: {report['imported']} imported, {report['skipped']} skipped.")
        if report['dry_run']:
            print("Dry run: nothing was saved. Add --commit to import.")

    elif args.command == 'worker':
        import logging
        from .worker import NotificationWorker
        logging.basicConfig(level=logging.INFO,
                            format='%(asctime)s %(levelname)s %(message)s')
        worker = NotificationWorker(app)
        if args.once:
            print(worker.run_once())
        else:
            worker.run_forever(args.interval)


if __name__ == '__main__':
    main()
