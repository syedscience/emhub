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
QR codes for the Microscope Issues pages, drawn as inline SVG on the server.
segno is optional (pip install emhub[issues]); without it there are no
codes, only the links.
"""

import flask

try:
    import segno
except ImportError:  # optional dependency
    segno = None


def available():
    return segno is not None


def base_url(app):
    """ Public address of EMhub, for links people open from a phone. """
    base = app.config.get('ISSUES_BASE_URL') or ''
    if not base and flask.has_request_context():
        base = flask.request.url_root
    return base.rstrip('/')


def svg(url, scale=4, title=''):
    """ Inline SVG of a QR code for url, or '' without segno. """
    if segno is None:
        return ''
    code = segno.make(url, error='m', micro=False)
    return code.svg_inline(scale=scale, border=2, dark='#101828', title=title or None,
                           omitsize=False)
