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
Database connection for the Microscope Issues module.
Kept separate from EMhub's own SQLite database (DbManager).
"""

import os

import sqlalchemy
from sqlalchemy.orm import scoped_session, sessionmaker

from .models import Base

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION_TABLE = 'mi_alembic_version'


class IssuesDb:
    def __init__(self, url, echo=False):
        self.url = url
        kwargs = {'echo': echo, 'future': True}
        if url.startswith('sqlite'):
            kwargs['connect_args'] = {'check_same_thread': False}
        else:
            kwargs.update(pool_pre_ping=True, pool_size=10, max_overflow=5)

        self.engine = sqlalchemy.create_engine(url, **kwargs)

        if url.startswith('sqlite'):
            @sqlalchemy.event.listens_for(self.engine, 'connect')
            def _sqlite_pragmas(dbapi_conn, record):
                cursor = dbapi_conn.cursor()
                cursor.execute('PRAGMA foreign_keys=ON')
                cursor.execute('PRAGMA journal_mode=WAL')
                cursor.close()

        self.Session = scoped_session(sessionmaker(bind=self.engine,
                                                   autoflush=False,
                                                   expire_on_commit=False))

    @property
    def session(self):
        return self.Session()

    def remove(self):
        self.Session.remove()

    def alembic_config(self):
        from alembic.config import Config
        cfg = Config()
        cfg.set_main_option('script_location', os.path.join(HERE, 'migrations'))
        cfg.set_main_option('sqlalchemy.url', self.url.replace('%', '%%'))
        cfg.attributes['engine'] = self.engine
        return cfg

    def migrate(self, revision='head'):
        """ Create or upgrade the schema to the given revision. """
        from alembic import command
        command.upgrade(self.alembic_config(), revision)

    def current_revision(self):
        from alembic.runtime.migration import MigrationContext
        with self.engine.connect() as conn:
            ctx = MigrationContext.configure(
                conn, opts={'version_table': VERSION_TABLE})
            return ctx.get_current_revision()

    def head_revision(self):
        from alembic.script import ScriptDirectory
        return ScriptDirectory.from_config(self.alembic_config()).get_current_head()

    def is_current(self):
        return self.current_revision() == self.head_revision()

    @staticmethod
    def metadata():
        return Base.metadata
