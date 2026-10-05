# Automatic database initialisation

New CSUPOR deployments build their database structure as part of application startup, before the first page is served and before the notification worker starts. A separate SQL import or terminal command is not required.

## Hosting setup

Configure `DATABASE_URL`, or the existing `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DATABASE`, `MYSQL_USER` and `MYSQL_PASSWORD` values, in the hosting provider's environment settings. The included driver is `mysqlconnector`, so a full URL uses `mysql+mysqlconnector://`. Start the repository's application normally.

The startup sequence:

1. Registers all application models and connects to the configured database.
2. If MySQL/MariaDB reports that this database does not exist, connects to the same server with the same credentials and creates that exact database using `utf8mb4`. Authentication, connectivity and other errors do not trigger database creation.
3. Serialises initialisation across MySQL/MariaDB workers with a database-specific advisory lock.
4. Creates missing tables, indexes and foreign keys in dependency order, then applies the supported additive column updates.
5. Creates the required approval and page-access settings only when absent, then allows normal application and worker startup.

The database server and account must be provided by the hosting platform. If that account cannot create a database, create/select an empty database through the provider's control panel and supply its connection details. Creating tables still requires the account's `CREATE` permission; the supported upgrades of existing tables require `ALTER` permission. Application startup does not obtain extra server privileges or substitute different credentials.

## Existing data and recovery

The same initialisation runs on later starts. It does not delete, recreate or clear existing tables, reset permissions, replace settings, or overwrite account passwords. Default role/page access remains in application code until explicitly saved. SMTP delivery remains disabled until configured and enabled through the settings page.

The application uses its ORM schema and targeted additive helpers. It does not execute `sql/schema.sql`, which includes a legacy administrator seed intended for manual installations. Automatic structure creation does not create a built-in account or import personnel data.

MySQL DDL commits independently; the entire setup is therefore not a single transaction. If initial setup stops partway through, a later start can create the remaining missing tables and defaults. This does not repair arbitrary existing objects: for example, a missing standalone index on an already existing table still needs explicit repair. Permission, connection and unexpected schema failures encountered during initialisation stop startup instead of being ignored. Workers wait up to 30 seconds for another initialisation to finish; a timeout stops that worker's startup and can be retried by the hosting service.

New foreign keys match existing MySQL identifier sizes and signedness. The supported automatic column updates remain the exact qualification dates and the flexible work-assignment shift flag. This initialisation is not a general migration runner: unrelated changes to existing columns still use their explicit scripts in `sql/migrations/`.

## Verification

Automated checks cover a clean application process with an empty SQLite database, the complete table set, required constraints and settings, the first public pages, restarts preserving saved data, and recovery of a missing table. Separate MySQL/MariaDB tests cover missing-database handling, safe identifier quoting, supplied credentials, lock ownership/release, and error paths. These checks do not access a hosted production database or send email.
