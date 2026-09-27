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

## Internationalization

The application is prepared for Flask-Babel based translations. English (`en`) is the default locale and Hungarian (`hu`) is registered as an additional supported locale. Users can switch languages from the header language selector; the selected locale is stored in the session and otherwise falls back to the browser's `Accept-Language` header.

Translation extraction is configured in `babel.cfg`. After adding or updating translatable strings, generate and compile catalogs with Flask-Babel tooling, for example:

```bash
pybabel extract -F babel.cfg -o messages.pot .
pybabel init -i messages.pot -d app/translations -l hu
pybabel compile -d app/translations
```


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
