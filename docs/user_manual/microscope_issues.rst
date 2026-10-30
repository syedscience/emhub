Microscope Issues
=================

*Module contributed by Syed Asfarul Haque (St. Jude Children's Research Hospital).*

The Microscope Issues module lets facility users report instrument problems
directly from EMhub and lets staff follow each report until the instrument is
working again. It appears as its own **Microscope Issues** tab in the main menu.

The module is optional and disabled by default.


Reporting an issue
------------------

1. Open **Microscope Issues** and click **Report an issue**, or use a link that
   preselects an instrument: ``/main?content_id=mi_report&resource_id=<id>``
   (useful for QR codes placed next to each instrument).
2. Select the instrument and the problem area. Only problem areas relevant to
   that type of instrument are listed (for example, cryo-FIB problems are not
   offered for a TEM).
3. Tick the symptoms you see. A **While you wait** panel shows what to do now,
   checks you can safely make, what to avoid and what to include in the report.
4. Choose how urgent the problem is. Some problems (for example vacuum or LN2
   alarms) are always treated as urgent, and the form says so when it raises
   the urgency.
5. Click **Send report**. The report gets a ticket number (``MI-2026-00042``)
   and the facility staff are notified.

Your name and email come from your EMhub login; they are never typed in.


Following an issue
------------------

* If someone already reported the problem, the report form lists the open
  issues on that instrument. Click **I'm also affected** to follow that issue
  instead of creating a duplicate.
* The issue page shows the history, comments and files (screenshots, PDF or
  log files up to 20 MB). Reporters and affected users can comment, add files
  and **reopen** a resolved issue if the problem comes back.
* Reporters get an email receipt, and emails when staff acknowledge, reply,
  resolve or reopen their issue.


Staff workflow
--------------

Issues move through these states::

    New -> Acknowledged -> Investigating -> Waiting for vendor -> Resolved -> Closed
                                     (Reopened, Duplicate)

* The first staff action (acknowledge, comment, change) is recorded as the
  **first response**. Acknowledging assigns the issue to you if nobody has it.
* **Instrument state** (operational, degraded, unavailable) is separate from the
  ticket status. Changing it records downtime intervals, which are closed when
  the issue is resolved, so restoration time is measured separately from
  administrative closure.
* **Resolve** asks how it was fixed, the root cause, notes, user time lost and
  whether data was lost. These feed the insights.
* **Duplicate of...** closes an issue and adds its reporter to the original.
* Comments and files can be marked **staff only**.
* **Post notice** shows a service banner (for one instrument or all of them)
  on the issue pages and the report form, optionally until a given time.


Who can see what
----------------

* **Reporters** (any EMhub user with access to the tab) can report issues and
  see all open issues, so they can check whether a problem is already known.
  Contact details of other reporters are hidden.
* **Staff** (EMhub managers, users with a ``staff-<unit>`` role, or roles
  mapped in ``config:issues``) see full details of every issue.
* **Admins** can enable or disable reporting for each instrument.

Access to the tab itself is controlled with the ``microscope_issues`` key in
the ``content`` section of ``config:permissions``. If the key is not present,
every logged-in user can see the tab.


Setup
-----

Install the optional dependencies and enable the module in the instance
``config.py``:

.. code-block:: bash

    pip install "emhub[issues]"

.. code-block:: python

    EMHUB_ISSUES = True
    # Optional, defaults to emhub-issues.sqlite in the instance folder.
    # PostgreSQL is recommended for production; keep the password out of
    # config.py by setting ISSUES_DB_URL in the environment instead.
    ISSUES_DB_URL = 'postgresql+psycopg://user@localhost/emhub_issues'

Issues are stored in their own database, separate from the EMhub SQLite file.
EMhub users and resources are referenced by their ids, and instrument names are
copied so the history is kept if a resource is renamed.

Then create the database schema and the default settings:

.. code-block:: bash

    export EMHUB_INSTANCE=/path/to/instance
    emh-issues migrate            # create or upgrade the issues database
    emh-issues init-config        # create the 'config:issues' form
    emh-issues sync-instruments   # list EMhub resources and their type
    emh-issues status             # check the schema is up to date

Restart EMhub after enabling the module.

Notifications and the worker
............................

Notifications are queued in the database together with the change that
triggered them and sent by a separate worker process, so a mail or Teams
outage never blocks or loses a report. Run it as a service:

.. code-block:: bash

    emh-issues worker              # runs every 10 seconds
    emh-issues worker --once       # process once, e.g. from cron

Email uses the EMhub mail settings (``MAIL_SERVER``, ``MAIL_PORT``,
``MAIL_USE_TLS``, ``MAIL_DEFAULT_SENDER``). Failed deliveries are retried with
increasing delays; permanent failures are listed in **Settings** where admins
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

Admins then choose recipients in **Microscope Issues -> Settings**: facility
emails and webhooks for every new issue, extra recipients by instrument and
urgency, escalation contacts and acknowledgement times, and a **Queue test**
button to check delivery.

Settings (``config:issues``)
............................

.. code-block:: json

    {
        "roles": {"staff": ["staff-cryoem"], "manager": ["head"], "admin": []},
        "notifications": {
            "email": ["cryoem-facility@example.org"],
            "webhooks": [],
            "receipt_to_reporter": true
        }
    }

``problem_areas`` can also be set here to replace the default list of problem
areas, symptoms and troubleshooting guidance
(``emhub/issues/knowledge/problem_areas.json``). The default guidance is a
starting point: facility staff should review it against their own procedures.
