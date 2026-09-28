# csupor

Flask-based login and personnel data management system backed by MySQL schema `csupor`.

## Features

- Login using **e-mail or username + password**.
- Registration using **e-mail, username, password** with the default `employee` privilege.
- User privileges can later be assigned by users with the `hr` or `ceo` privilege.
- User privilege enum: `employee`, `hr`, `ceo`, `developer`.
- Numeric ascending user ID using MySQL auto-increment primary key.
- Additional personnel profile data after registration.
- Dependents management.
- Educational qualifications management (multiple records supported).
- Optional teacher professional exam record.

## Internationalisation

The application uses Flask-Babel translations. English (`en`) is the default locale and Hungarian (`hu`) is registered as an additional supported locale. Users can switch languages from the header language selector; the selected locale is stored in the session and otherwise falls back to the browser's `Accept-Language` header.

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

An explicit MySQL schema script is available at `sql/schema.sql`.
You can run it directly, for example:

```bash
mysql -u root -p < sql/schema.sql
```

## Setup

1. Create database schema:
   ```sql
   CREATE DATABASE csupor;
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Configure environment:
   ```bash
   cp .env.example .env
   # edit values as needed
   set -a
   source .env
   set +a
   ```
4. Run app:
   ```bash
   python run.py
   ```

Missing tables are created automatically on startup. On MySQL/MariaDB, new integer foreign keys inherit the actual referenced column type, supporting both SQL-script installations with unsigned user IDs and older ORM-created installations with signed IDs. Existing tables and data are not altered; column changes still require the applicable scripts in `sql/migrations/`.

## Interface

The portal uses a responsive light interface with sidebar navigation, a mobile menu, and shared styling for employee and management screens. See [the design preview and verification notes](docs/design/README.md) for screenshots. Front-end assets are served locally by Flask; no additional build step or CDN is required.

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

Nursery head/deputy approvals remain scoped to their legal entities. Deputies cannot approve their own requests. Existing director/nursery head self-approvals follow the selected rule. An eligible reviewer may reject a pending request in any mode; a rejection closes it. Existing cancellation permissions remain unchanged. Review lists and approval buttons follow the current rule. The calendar names eligible people for the selected contract and combines overlapping roles.

The new `leave_approval_settings` table and its default **Both** row are created automatically on startup; this feature needs no changes to existing tables or manual migration. Its user foreign key uses the same signed/unsigned compatibility handling as the other newly created tables. The setting persists in the database, records its latest editor/time, and is shared by all workers. On MySQL, a settings row lock serialises rule changes with leave submissions and manager decisions. Stale settings forms cannot silently overwrite another director's change.

Run the regression suite with `python -m unittest discover -s tests -v`.

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
