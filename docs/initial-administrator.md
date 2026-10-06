# First administrator setup

A new installation can create its first administrator from values entered in the hosting provider's control panel. No SQL import, database editor or terminal command is needed. Ordinary public registration always creates an employee account; it does not grant administrative access to the first visitor.

## DigitalOcean App Platform

Open **Apps → your CSUPOR app → Settings → your web service → Environment Variables → Edit**, then add all three variables with **Run time** scope:

| Key | Value |
| --- | --- |
| `INITIAL_ADMIN_USERNAME` | The username you want to use to sign in |
| `INITIAL_ADMIN_EMAIL` | Your email address |
| `INITIAL_ADMIN_PASSWORD` | A new login password of at least 12 characters |

Select **Encrypt** for the password. This is the account's login password, separate from the application's `SECRET_KEY`. Save the configuration and redeploy. When deployment has completed, use the application's normal login page with your chosen email or username and password. Do not register a second account.

The account receives **Developer** privilege. It can open **Page access** and **Privileges**, allowing you to configure access and assign other users' privileges. Ownership and leave-approval rules still apply as described in [page access](page-access.md).

After confirming that you can sign in and open **Page access**, remove all three `INITIAL_ADMIN_*` variables from the hosting settings and save. The account and password remain in the database. Change the login password through **Change password**, not by editing initialisation variables.

If separate web and worker components start at the same time, configure the same three values consistently on every component that runs CSUPOR startup, or use shared app-level environment variables. Their MySQL initialisation is serialised by the existing database lock.

## If you have already registered

When no developer exists yet, you can supply the **exact username and email of your existing account, together with its current password**. Startup verifies that both identifiers refer to the same account and checks the password before granting developer privilege. It preserves the account ID, password hash, profile and other records.

If the existing password is shorter than 12 characters, first sign in to that account and update it through **Change password**, then use the updated password for setup.

A reused email with a different username, a reused username with a different email, or an incorrect existing password stops initialisation with an explanatory error. No account is overwritten or promoted in these cases. Correct the configuration in the hosting panel and redeploy. Alternatively, supply a different unused username and email to create a separate administrator account.

## Behaviour and persistence

- With no initial administrator variables, startup continues normally without creating an account.
- If any developer account already exists, the initial administrator configuration is ignored. Redeployment or a changed environment password does not reset or replace an existing administrator.
- When no developer exists, all three values are required. Usernames support up to 50 characters and emails up to 120. The password must contain 12–1024 Unicode characters; it is used exactly as supplied, without trimming.
- Passwords use the application's existing password hashing. Credentials are never written to source files or application log messages, and no email is sent during setup.
- The account and its profile are saved together. A failed setup cannot leave a half-created administrator.

This feature uses the existing user and profile tables and needs no database migration. The environment variables authorise initial administrator creation only; public registration and page-access checks retain their normal rules.
