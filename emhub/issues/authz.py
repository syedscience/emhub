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
Issue roles, derived from the EMhub user roles.

    reporter: any active EMhub user who can see the Microscope Issues tab
    staff:    triage and resolve issues, see internal notes
    manager:  insights, exports
    admin:    module settings

By default EMhub managers are staff + manager, EMhub admins get every role and
any 'staff-<unit>' role makes a user staff. Extra EMhub roles can be mapped in
the 'config:issues' form, e.g.:

    {"roles": {"staff": ["staff-cryoem"], "manager": ["head"], "admin": []}}
"""

REPORTER, STAFF, MANAGER, ADMIN = 'reporter', 'staff', 'manager', 'admin'


class Forbidden(Exception):
    """ Raised when the current user lacks the required issue role. """
    pass


def user_issue_roles(user, config=None):
    """ Return the set of issue roles for an EMhub user. """
    if user is None or not getattr(user, 'is_authenticated', False):
        return set()
    if not user.is_active:
        return set()

    mapping = (config or {}).get('roles', {})
    user_roles = set(user.roles or [])

    def _mapped(role):
        return bool(user_roles.intersection(mapping.get(role, [])))

    roles = {REPORTER}

    if user.is_manager or any(r.startswith('staff-') for r in user_roles) or _mapped(STAFF):
        roles.add(STAFF)
    if user.is_manager or _mapped(MANAGER):
        roles.add(MANAGER)
    if user.is_admin or _mapped(ADMIN):
        roles.update({STAFF, MANAGER, ADMIN})

    return roles


def require(roles, role):
    if role not in roles:
        raise Forbidden(f"This action requires the '{role}' role.")
