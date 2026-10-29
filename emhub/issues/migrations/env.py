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
"""Alembic environment for the Microscope Issues database (not EMhub's)."""
from alembic import context

from emhub.issues.models import Base
from emhub.issues.db import VERSION_TABLE

config = context.config
target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=config.get_main_option('sqlalchemy.url'),
                      target_metadata=target_metadata,
                      version_table=VERSION_TABLE,
                      render_as_batch=True,
                      literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = config.attributes.get('engine')
    if engine is None:
        import sqlalchemy
        engine = sqlalchemy.create_engine(config.get_main_option('sqlalchemy.url'))
    with engine.connect() as connection:
        context.configure(connection=connection,
                          target_metadata=target_metadata,
                          version_table=VERSION_TABLE,
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
