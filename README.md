# csupor

Flask-based login and personnel data management system backed by MySQL schema `csupor`.

## Features

- Login using **e-mail or username + password**.
- Email password recovery with a single-use link, sent immediately through the configured mail server.
- Account email changes in My profile, confirmed with the current password.
- Registration using **e-mail, username, password** with the default `employee` privilege.
- User privileges can later be assigned through the Privileges page (HR, director and developer access by default).
- User privilege enum: `employee`, `hr`, `ceo`, `developer`.
- Developer-managed page access matrix, shared by navigation, dashboard and server-side route checks.
- Optional first administrator setup through hosting environment variables, without SQL or a terminal.
- Numeric ascending user ID using MySQL auto-increment primary key.
- Additional personnel profile data after registration.
- Dependents management.
- Educational qualifications management (multiple records supported).
- Optional teacher professional exam record.

## Internationalisation

The application uses Flask-Babel translations. Hungarian (`hu`) is the default locale for a new browser session or device, regardless of the browser's `Accept-Language` header. English (`en`) remains available from the header language selector. An explicit language choice is stored in the session and retained through login and logout; without a valid saved choice, the interface uses Hungarian.

Date fields retain the browser's native calendar and regional field order. Fields without a stricter date limit use `max="9999-12-31"`, matching Python's supported year range. This also lets the browser advance from the year segment after four digits instead of waiting for a six-digit year. New date fields should specify a four-digit upper year limit too; keep any stricter business limit, such as today's date for birth dates or completed qualifications.

Translation extraction is configured in `babel.cfg`. After adding or updating translatable strings, update the existing catalogue, fill in the new Hungarian translations, and compile it:

```bash
pybabel extract -k lazy_gettext -F babel.cfg -o messages.pot .
pybabel update --no-fuzzy-matching -i messages.pot -d app/translations -l hu
# Fill in the new msgstr entries in app/translations/hu/LC_MESSAGES/messages.po.
pybabel compile -d app/translations
```

Enum display labels live in `app/i18n.py`; keep the stored enum values unchanged. The `lazy_gettext` extraction keyword also includes approval-policy labels. Translation coverage is checked by `tests/test_admin_localisation.py`.

See [Hungarian administration and contract lists](docs/design/admin-localisation.md) for the updated workflows and previews.

See [personal records, named approvals and legal entitlements](docs/design/people-context.md) for the full system names, dependent editing, workplace birthdays and personalised Púétv./Mt. references.

## SQL schema file

The application builds its database structure automatically on first startup; importing SQL is not required for a new deployment. An explicit MySQL schema script is also available at `sql/schema.sql` for manual installations. It includes a legacy administrator seed, so it is not run by the application bootstrap.
For a manual installation only:

```bash
mysql -u root -p < sql/schema.sql
```

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Configure environment:
   ```bash
   cp .env.example .env
   # edit values as needed
   set -a
   source .env
   set +a
   ```
3. Run app:
   ```bash
   python run.py
   ```

On a hosting platform, set the same environment variables in its configuration panel and start the application from the repository. Before serving its first page, CSUPOR connects to the configured database and creates all missing tables, indexes, foreign keys and required default settings. If the MySQL/MariaDB database itself is missing, it also attempts to create that exact configured database using the supplied account, with `utf8mb4` encoding. No terminal command or SQL import is needed for the structure. If the provider does not permit applications to create databases, create an empty database in its hosting panel and supply those connection details. The application account must be allowed to create tables.

Concurrent MySQL/MariaDB workers serialise their initialisation using a database-specific lock. Repeated startup preserves existing records and settings; an interrupted first setup can resume creating the remaining missing tables and default settings. New integer foreign keys inherit the actual referenced column type, supporting both SQL-script installations with unsigned user IDs and older ORM-created installations with signed IDs. Startup also adds missing qualification-date columns and the optional flexible-shift flag, preserving existing values; these upgrades require `ALTER` permission. Other column changes require the applicable scripts in `sql/migrations/`. See [automatic database initialisation](docs/database-initialisation.md) for behaviour and hosting requirements.

To create your first administrator without a terminal, set `INITIAL_ADMIN_USERNAME`, `INITIAL_ADMIN_EMAIL` and `INITIAL_ADMIN_PASSWORD` in the hosting platform's environment settings, then redeploy. Mark the password as encrypted/secret. If there is no developer account yet, startup creates the requested account with developer privilege, which can manage page access and user privileges. Ordinary registration continues to create employee accounts. After a successful administrator login, remove these three initialisation variables. See [first administrator setup](docs/initial-administrator.md), including how to use an account you have already registered.

## Interface

The portal uses a responsive light interface with sidebar navigation, a mobile menu, and shared styling for employee and management screens. See [the design preview and verification notes](docs/design/README.md) for screenshots. Front-end assets are served locally by Flask; no additional build step or CDN is required.

See [compact leave limits and grouped navigation](docs/design/compact-limits-navigation.md) for the responsive allowance editor, desktop flyout groups and dependent birth-date validation.

See [year boundaries and annual GYÁP forms](docs/design/year-boundaries-gyap.md) for leave requests spanning calendar years and HR-managed childcare sickness benefit documents.

Developers can open **Page access** (`/page-access`) to configure access to each page for employee, HR, director and developer privileges. Their access to this editor is permanently enabled. Denied pages are hidden from menus and the dashboard and reject direct requests, including child forms and actions. Existing ownership and leave-approval rules still apply. The role descriptions below are the initial defaults; see [page access and its repeatable migration](docs/page-access.md) for configuration and scope.

**Settings** groups **Privileges**, **Page access** and **Email settings**, with each entry visible only when permitted. The personal leave calendar additionally requires at least one employment contract, including an inactive or future contract. Users with no contract cannot open it, even when their role has page access.

The login page links to **Forgot your password?**. Recovery links expire after 30 minutes and work once. **My profile** has a separate account email form requiring the current password. See [email password recovery](docs/password-recovery.md) for hosting, delivery and security details. This update replaces legacy login sessions, so users must sign in again once after deployment; later password changes revoke earlier sessions automatically.

## Profile administration and account display

HR and director accounts can search user profiles and filter by active/inactive contracts or profile completeness. Active status uses inclusive contract dates in Europe/Budapest. The existing completion checklist determines whether details are complete; whitespace-only fields do not count, and incomplete profiles show a yellow status.

Deleting a user requires access to User profiles and the signed-in user's own password in a confirmation dialog. This permanently removes the account and its owned records, including contracts, leave requests and profile photo. Other users' records and shared annual GYÁP forms remain, with references to the deleted approver/uploader cleared. Self-deletion and deletion of the final director account are blocked. Deletion is transactional, CSRF-protected, and rolls back on failure.

Users can upload their own JPEG, PNG or WebP profile photo (up to 5 MiB). Photos are decoded, oriented, stripped of metadata and resized before database storage. The new `profile_photos` table is created automatically at startup using the existing MySQL identifier compatibility handling; no existing table columns change. Install the updated requirements for Pillow support. Only the owner and users with User profiles access can retrieve a photo. The sidebar account link shows the name and newest active contract's job title, with username and privilege fallbacks.

The profile-photo editor supports dragging, zooming and a circular crop preview before saving. Existing photos can be edited again, with keyboard controls as well as mouse/touch input. Saving the image keeps any unsaved personal-details form values intact. New uploads retain a private, metadata-free source image in `profile_photo_sources` so later edits can recover areas outside the previous crop. This table is created automatically; older photos use their existing image as the editable source. The server validates crop coordinates and checks photo versions to prevent stale edits from overwriting a newer photo.

Birthday reminders keep shared-workplace visibility for employees, show every director's birthday to everyone, and show every active employee's birthday to directors. Names are bold and the reminder uses singular/plural wording. Leave-calendar abbreviations have a visible legend and full accessible labels. Weekly hours remain editable on contracts but are omitted from the summary table.

Qualifications and professional exams record the exact date obtained and use the label **Document number**. New or edited records require a valid date from 1900-01-01 through the current Budapest calendar day. Existing year-only records retain their original year and explicitly show that the exact date is missing; the qualification editor allows the owner to supply it. Startup adds a nullable `date_obtained` column to each of `educational_qualifications` and `professional_exams` when missing, requiring `ALTER` permission on existing MySQL tables. It does not invent a month/day or change old records. The legacy year remains for compatibility and is synchronised when a full date is saved.

## Manual portal testing

### Configurable leave approvals

Users with Director privilege (stored as `ceo`) can open **Management → Approval settings** (also linked from the dashboard and leave manager). The application-wide rule covers all legal entities:

| Rule | Approval needed |
| --- | --- |
| Director only | One director approval |
| Nursery head/deputy only | One approval from the relevant nursery head or deputy |
| Both (default) | Director approval and nursery head/deputy approval |
| Either | One approval from either group |

Saving applies the rule to new and pending requests. Pending requests whose recorded approvals satisfy the new rule become approved in the same transaction; the director who changed the rule is recorded as the decision-maker, while the original approval records are preserved. Approved, rejected, cancelled and pending-cancellation requests are unchanged. Selecting a stricter rule does not reopen completed decisions.

Nursery head/deputy approvals remain scoped to their legal entities. Deputies cannot approve their own requests. Existing director/nursery head self-approvals follow the selected rule. An eligible reviewer may reject a pending request in any mode; a rejection closes it. Cancellation decisions use the same eligible roles and entity scope: either eligible reviewer can resolve a cancellation, including under the **Both** rule. Review lists and action buttons follow the current rule. The calendar names eligible people for the selected contract and combines overlapping roles.

The new `leave_approval_settings` table and its default **Both** row are created automatically on startup; this feature needs no changes to existing tables or manual migration. Its user foreign key uses the same signed/unsigned compatibility handling as the other newly created tables. The setting persists in the database, records its latest editor/time, and is shared by all workers. On MySQL, a settings row lock serialises rule changes with leave submissions and manager decisions. Stale settings forms cannot silently overwrite another director's change.

Run the regression suite with `python -m unittest discover -s tests -v`.

### Leave email notifications

Developer accounts have a separate **Settings** menu (`/settings`) for the SMTP host, port, connection security, optional credentials, sender and public application URL. Email is disabled until configured and enabled. If no encryption key is configured, use **Create encryption key** on that page: the application securely creates a persistent private key without a terminal or restart. Existing `EMAIL_SECRET_KEY` or non-default `SECRET_KEY` configurations keep working. SMTP passwords are stored encrypted and are never displayed again. Keep the private `instance/email-secret.key` file across deployments and backups when using browser setup.

Eligible reviewers receive approval/cancellation tasks, and applicants receive changes to their own requests. Each recipient gets one daily digest at **20:00 Europe/Budapest**. Changes concerning leave that starts within 24 hours, or has already started, are queued for immediate delivery. Notifications use the current email address on the user's account. Pending tasks are checked again before sending.

The persistent queue survives restarts and retries failed deliveries. The built-in worker runs by default for MySQL deployments while the application process remains running; a separately supervised worker is also available. Three new tables are created at startup, or can be created beforehand using the explicit repeatable SQL migration. See [email setup, scheduling and operations](docs/email-notifications.md) for commands and deployment requirements.

### Full portal checklist

For an end-to-end, role-based checklist covering every portal screen and workflow, see [the manual portal test guide](PORTAL_TESTING.md).


## Troubleshooting

### MySQL error 1045 (Access denied for user)

If startup fails with `1045, "Access denied for user ..."`, your credentials in the connection string are incorrect.

If the error ends with `(using password: NO)`, your app is connecting **without any password**. In this project, that means either:
- `MYSQL_PASSWORD` is unset/empty, or
- `DATABASE_URL` does not include `:password@`.

Also note: `export $(cat .env | xargs)` can silently break values that contain special characters (such as `#`, `$`, spaces), causing `MYSQL_PASSWORD` to load incorrectly. Prefer `source .env` (shown above).

Use one of these approaches:

1. Set full URL:
   ```bash
   export DATABASE_URL='mysql+mysqlconnector://root:YOUR_REAL_PASSWORD@localhost:3306/csupor'
   ```
2. Or set split MySQL variables:
   ```bash
   export MYSQL_USER=root
   export MYSQL_PASSWORD='YOUR_REAL_PASSWORD'  # leave empty if your root user has no password
   export MYSQL_HOST=localhost
   export MYSQL_PORT=3306
   export MYSQL_DATABASE=csupor
   ```

`DATABASE_URL` takes precedence when both are set.

### MySQL error 1005 / errno 150 when creating `leave_years`

The SQL schema defines `users.id` as `INT UNSIGNED`, while older ORM-created databases use signed `INT`. Creating `leave_years.imported_by_id` with a different size or signedness causes MySQL to reject the foreign key.

Update the application code and restart it. Startup now reads the referenced column type and applies it to the missing table's creation statement. This supports both existing variants without converting IDs, disabling foreign-key checks, or deleting tables.

If the error remains, collect the definitions and latest InnoDB foreign-key error:

```sql
SHOW CREATE TABLE users;
SHOW ENGINE INNODB STATUS;
```

This fix creates missing tables; it does not apply pending column migrations to existing tables.

## Schema regression checks

With `requirements.txt` installed, run:

```bash
python -m unittest discover -s tests -v
```

The checks cover generated MySQL DDL for signed/unsigned identifiers, fresh and existing schemas, and an actual SQLite create/restart cycle that preserves stored records. MySQL DDL tests use SQLAlchemy's dialect and a reflected-schema fixture; they do not require or modify a live MySQL server.

## Working-time register

The top-level **Working-time register** menu shows the signed-in employee's own schedule and monthly exports. Any employee with a contract can access it, including previous contracts for historical records. HR and directors use **Management → Working-time management** to select employees, generate and verify schedules, correct daily entries, record group mergers and follow a link to group management. **Management → Groups** opens the independent `/groups` list, with workplace selection, assigned staff and separate create/edit pages. The group editor manages both group details and dated employee assignments. The personal page remains personal even for HR and directors.

The scheduler uses the configured Hungarian working calendar and approved absences, accounts for unpaid breaks and trainee teaching hours, alternates assigned weekly shifts, fills morning/afternoon coverage for employees with **No assigned shift**, and distributes early opening duties as evenly as possible among that day's eligible morning-shift staff. Opening duty cannot move an afternoon worker onto the morning shift. Conflicting requirements produce actionable issues instead of excessive hours or scheduling absent staff.

Generated hours are a draft until HR/director verification. Changed source data or stale browser revisions cannot silently produce a confirmed/exported outdated register. For teachers, totals represent scheduled **bound working time**, with teaching hours tracked separately; the remainder of a 40-hour contract is not automatically treated as worked. Partial weeks are explicitly totalled within the selected month.

The PDF is a single portrait A4 page. Job title, workplace and group are header information, leaving room for daily times, weekly totals and signatures. Long notes use explicit abbreviations with a legend; the CSV preserves the full text, dated assignments and individual intervals.

Five additional tables are created automatically at startup, with existing MySQL foreign-key type compatibility preserved. Install the updated requirements for ReportLab PDF support. Bundled DejaVu fonts support Hungarian names without system-font dependencies. See [the allocation algorithm and operating workflow](docs/working-time.md) for the detailed institutional rules and conflict handling.


### Optional group shifts and schema update

`/groups/new?place_id=…` creates a group; `/groups/<id>/edit` edits the group and its employee assignments. The former `/worktime/groups` address redirects to the standalone list. Groups access is required (HR/director by default) and is configured separately from Working-time management. An empty shift selection means **No assigned shift**: flexible teachers complement their partners, and a flexible nursery assistant can cover an absent teacher’s afternoon without also opening that day. A fixed 0/1 weekly rotation keeps its previous meaning.

The `work_assignments.flexible_shift` Boolean column has a false default, so existing assignments retain their stored rotation. Application startup adds the column if missing; this needs `ALTER` permission. Administrators can instead run [`2026-10-04-add-flexible-work-assignment-shifts.sql`](sql/migrations/2026-10-04-add-flexible-work-assignment-shifts.sql) beforehand. The script and startup update can both be repeated without resetting assignments. No tables or existing fields are dropped.

Scheduling rule version 3 marks previously generated registers as needing regeneration before confirmation/export. Existing saved entries remain until an explicit regeneration; the usual manual-entry replacement safeguard still applies. Tests use temporary SQLite databases and MySQL dialect/reflection fixtures, without connecting to production MySQL.
