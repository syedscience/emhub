Microscope Issues
=================

*Module contributed by Syed Asfarul Haque (St. Jude Children's Research Hospital).*

The Microscope Issues module lets facility users report instrument problems
directly from EMhub and lets the EMhub managers follow each report until the
instrument is working again. It appears as its own **Instrument Help** tab in
the main menu: for users it opens the report form, for managers the list of
issues.

The module is optional and disabled by default.


Reporting an issue
------------------

1. Open **Instrument Help** (managers: click **Report an issue**), or scan the
   QR code on the instrument. The form opens with your name, PI and email from
   your EMhub login, and with your EMhub booking or running session on now,
   which also chooses the instrument.
2. If no instrument is chosen yet, pick it. Session details (session status,
   when it started, software...) are optional and folded away; a booking on
   now fills in *Running*.
3. Open issues on that instrument are listed first. If yours is one of them,
   click **I have this too**: staff see that it affects you and you get its
   updates, with nothing else to fill in.
4. Otherwise choose the problem area and tick the symptoms you see. Only the
   problems that instrument can have are listed (a plunge freezer gets
   blotting and humidity, not beam or vacuum). A **While you wait** panel shows
   what to do now, checks you can safely make, what to avoid and what to note.
5. An urgency is suggested for the problem; change it if needed. Some problems
   (for example vacuum or LN2 alarms) are always treated as urgent.
6. Click **Send to staff**. The report gets a ticket number (``MI-2026-0042``)
   and the facility staff are notified. Screenshots, PDF or log files (up to
   20 MB) can be sent with the report; on a phone, **Take a photo** opens the
   camera.

After sending, a QR code lets you add photos from your phone to the report
(for example a photo of the error on the instrument screen). Only the
reporter can add files this way.

QR codes and phones
~~~~~~~~~~~~~~~~~~~

Managers print one QR code per instrument from **Setup -> QR codes**.
Scanning it opens Instrument Help with that instrument chosen
(``/main?content_id=mi_report&resource_id=<id>``). The codes use
``ISSUES_BASE_URL`` from the instance ``config.py``, which must be an address
phones can open. QR codes need the optional ``segno`` package
(``pip install emhub[issues]``); without it the page lists the links.

On a phone, Instrument Help can be added to the home screen (Safari: **Share
-> Add to Home Screen**) and then opens like an app, with its own icon.


Staff reports
-------------

Managers report problems they find themselves with the **Staff report**
switch at the top of **Report an issue**. Besides the instrument and problem,
they choose:

* **Found during**: no session (maintenance, alignment, a routine check) or a
  recent booking on that instrument, of any user. That user can be kept
  updated by email, like someone who clicked *I have this too*.
* **When it started**, **how it was found** (routine check, maintenance or
  alignment, a user's session, vendor visit, alarm) and the **instrument
  state now**. *Down* or *Degraded* starts the downtime at the start time.
* **Who handles it**: assigned to themselves and *Investigating*, or left
  *New* for the team.
* **Notify the team**: off by default, so a staff report is only added to the
  issue log (no emails, Teams messages, receipt or escalation). Tick it to
  alert the team as for a user report.

Staff reports are marked *Found by staff* in the issue log, which can be
filtered on them.

Following an issue
------------------

Reporters follow their report by email: a receipt, then an email when it is
acknowledged, when a manager replies, and when it is resolved or reopened.
These emails contain everything the reporter needs and no link, because only
managers can open issues. If the problem comes back, the reporter contacts the
facility and a manager reopens the issue.


Manager workflow
----------------

Issues move through these states::

    New -> Acknowledged -> Investigating -> Waiting for vendor -> Resolved -> Closed
                                     (Reopened, Duplicate)

* The first manager action (acknowledge, reply, change) is recorded as the
  **first response**. Acknowledging assigns the issue to you if nobody has it.
* **Instrument state** (operational, degraded, unavailable) is separate from the
  ticket status. Changing it records downtime intervals, which are closed when
  the issue is resolved, so restoration time is measured separately from
  administrative closure.
* **Resolve** asks how it was fixed, the root cause, notes, user time lost and
  whether data was lost. These feed the insights.
* **Duplicate of...** closes an issue; its reporter then gets the updates of
  the original issue.
* A comment is a reply emailed to the reporter, unless it is marked as an
  **internal note**.
* **Post notice** shows a service banner (for one instrument or all of them)
  on the report form, so users see it before reporting, optionally until a
  given time.


Insights
--------

Managers open **Instrument Help -> Issues & Insights -> Insights** for a date range (default:
last 90 days), optionally for one instrument:

* incidents per week by urgency, open issues by age, oldest open issues;
* per instrument: incidents, booked hours (from EMhub bookings), incidents per
  100 booked hours, unavailable hours, availability, median first response and
  restoration, time waiting for the vendor;
* problem areas, top symptoms, root causes, how issues were resolved and
  recurring problems;
* data coverage (how many incidents lack a response, a start time or a root
  cause) and notification health.

How each number is calculated is shown at the bottom of the page. In short:
duplicates are not counted as incidents; overlapping downtime is counted once;
restoration time is measured to when the instrument was restored, not to
ticket closure; booked hours are not operating hours, so compare rates rather
than raw counts between instruments.

**Export CSV** downloads the issues of the period (managers only). A weekly summary can be emailed every Monday morning (set the
recipients in **Settings**).


Problem areas
-------------

Managers change what users can pick on the report form in **Problem areas**
(a tab on the report page, and a button on the issue list). For each area:

* the name users see, and whether users can try checks or only staff can act
  (users then get holding actions);
* a minimum urgency: reports in the area are sent with at least that urgency;
* which instrument types it is offered for, and optionally only some
  instruments (for example an area only for Krios01);
* general checks ("Start here"), things not to do, holding actions and what to
  write down for staff, one per line;
* the symptoms, each with its own checks, minimum urgency and optionally only
  some instruments.

A preview shows the report form and the **While you wait** panel as a user
on a chosen instrument sees them. Changes apply to new reports at once and are
recorded. Areas can be reordered and hidden; issues already reported in a
hidden area keep its name. A changed built-in area can be reset to the
version shipped with EMhub.

The built-in areas (``emhub/issues/knowledge/problem_areas.json``) are a
starting point: facility staff should review them against their own
procedures.

Importing old logbooks
----------------------

Entries of EMhub logbooks (projects with status ``special:logbook``) can be
imported once as historical issues:

.. code-block:: bash

    emh-issues import-logbooks 219,220,221,222          # dry run: report only
    emh-issues import-logbooks 219,220,221,222 --commit
    emh-issues import-logbooks 223 --map 223=12 --commit  # logbook without instrument

Imported issues keep their original dates, are closed if the last logbook
action is ``solved``, keep the actions as internal notes, never send
notifications and are excluded from response and restoration times. Running
the import again skips entries that were already imported. The logbooks
themselves are not changed.


Who can see what
----------------

* **Users** (anyone with access to the tab) can report issues, add files to
  their own report and see the service notices. They cannot open the list of
  issues, any issue page, the history or the insights.
* **Managers** (EMhub roles ``manager``, ``head`` or ``admin``) see every tab:
  the issues, their full history, the insights and the CSV export, the problem
  areas and the settings (notifications and which instruments take reports).
  They handle the issues.
* **Admins** can also run the one-time logbook import.

Issues are stored in a hidden EMhub project. EMhub's generic project and entry
pages and API routes (``entry_form``, ``/api/get_entries``,
``/api/update_entry``...) refuse that project and its entries for every user,
and leave them out of their lists, so the module pages are the only way to see
or change them.

Access to the tab itself is controlled with the ``microscope_issues`` key in
the ``content`` section of ``config:permissions``. If the key is not present,
every logged-in user can see the tab.


Setup
-----

Enable the module in the instance ``config.py`` and restart EMhub:

.. code-block:: python

    EMHUB_ISSUES = True

There is no database to set up. Issues are stored in the EMhub database, as
entries of a hidden project with status ``special:issues`` (created when the
first issue is reported), the same way EMhub keeps its logbooks. They are
backed up with the rest of the EMhub data. Each issue is one entry, with its
history, symptoms, files and downtime kept in the entry; service notices,
queued notifications and changes to the settings are entries of their own type
(``mi_notice``, ``mi_message``, ``mi_audit``). Attached files are saved in the
``issue_files`` folder of the instance.

Optionally create the default settings and check the instruments:

.. code-block:: bash

    export EMHUB_INSTANCE=/path/to/instance
    emh-issues init-config        # create the 'config:issues' form
    emh-issues instruments        # list EMhub resources and their type
    emh-issues status             # where issues are stored and how many

Notifications and the worker
............................

Notifications are saved together with the change that triggered them and sent
by a separate worker process, so a mail or Teams outage never blocks or loses a
report. Run one worker as a service:

.. code-block:: bash

    emh-issues worker              # runs every 10 seconds
    emh-issues worker --once       # process once, e.g. from cron

Email uses the EMhub mail settings (``MAIL_SERVER``, ``MAIL_PORT``,
``MAIL_USE_TLS``, ``MAIL_DEFAULT_SENDER``). Failed deliveries are retried with
increasing delays; permanent failures are listed in **Settings** where managers
can retry them. The worker also alerts the escalation contacts when an issue is
not acknowledged in time.

Microsoft Teams channels are reached through a Teams *Workflow* ("When a Teams
webhook request is received" -> post card in a channel). Workflow URLs are
secrets, so they are defined only on the server, never in the database:

.. code-block:: python

    # config.py (or environment variables ISSUES_WEBHOOK_<NAME>)
    ISSUES_WEBHOOKS = {'cryoem-channel': 'https://...logic.azure.com/...'}
    ISSUES_BASE_URL = 'https://emhub.example.org'   # links in emails and cards
    ISSUES_FILES = '/data/emhub-issues/files'       # default: <instance>/issue_files

Admins then choose recipients in **Instrument Help -> Setup -> Settings**: facility
emails and webhooks for every new issue, extra recipients by instrument and
urgency, escalation contacts and acknowledgement times, and a **Queue test**
button to check delivery.

Settings (``config:issues``)
............................

.. code-block:: json

    {
        "disabled_instruments": [],
        "notifications": {
            "email": ["cryoem-facility@example.org"],
            "webhooks": [],
            "receipt_to_reporter": true
        }
    }

``facility_name`` and ``staff_phone`` set the name shown on the report form and
the phone number shown for "Stop now" problems. Problem areas are edited in
the **Problem areas** page, not here.
