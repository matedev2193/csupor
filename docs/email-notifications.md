# Leave email notifications

## Configure delivery

1. Install `requirements.txt`, including `cryptography`.
2. Sign in with a **developer** account and open **Settings** (`/settings`). Other privileges cannot read or change this page.
3. If prompted, click **Create encryption key**. The application generates and privately stores a random key on the server. No terminal command, manual environment edit or application restart is needed. If a valid key is already configured, the page shows that encryption is ready and keeps that key unchanged.
4. Enter the SMTP hostname, port, security mode, optional username/password, sender address/name and the application's canonical public URL. STARTTLS and SSL/TLS verify server certificates. The unencrypted option supports a trusted local mail relay.
5. Enable notifications and save. Leaving the password empty preserves the stored password; use the explicit removal option to clear it.

The settings form is CSRF-protected, checks concurrent edits, and never returns the stored password or encrypted value. Sending uses one recipient per message, with no CC/BCC. The destination is the current `User.email`, not an address copied into a request or an event. SMTP credentials are only decrypted for delivery. Raw SMTP errors, credentials and message bodies are not logged.

### Key storage and existing installations

Browser setup writes `instance/email-secret.key`, outside Flask's public static directory and excluded from Git. The key is random, persistent and readable only by the application account on POSIX systems. Creation is atomic across workers; repeated clicks retain the existing key. The key is never returned in the page, session, database or logs. This change adds no database table or column and requires no migration.

Key selection uses a valid explicit `EMAIL_SECRET_KEY` first, then the private key file, then an existing non-default `SECRET_KEY` for compatibility. Example/default values are never used for SMTP encryption. Browser setup does not rotate an existing usable key or change the application's session secret. Keep the usual strong `SECRET_KEY` configuration for Flask sessions.

Preserve the private key file with the application's persistent data when updating or moving the installation, and back it up securely alongside the database. Web and separate notification workers must share the same key file or the same explicit `EMAIL_SECRET_KEY`. If releases use different directories or separate hosts, use shared persistent storage for the private instance folder or a shared environment key.

If the private instance folder is not writable, the page shows an actionable storage error and changes no SMTP settings; the hosting provider must grant the application access to that private folder. Unreadable, damaged or symbolic-link key files are not replaced automatically. When credentials already exist but the key is missing, restore that key, or explicitly disable email and remove the saved password before generating a replacement and re-entering the password. A changed but usable configured key instead prompts you to enter the SMTP password again.

## Recipients and timing

New pending requests notify the eligible directors and/or nursery heads/deputies under the current approval policy. Leadership is limited to the request's legal entity and inclusive assignment dates in Budapest time. Deputies cannot review their own request. An account with overlapping roles receives one notification per event.

Changes to approval rules, privileges or leadership appointments notify newly eligible reviewers about existing pending tasks. The worker also detects dated leadership appointments when they become active and discovers existing pending tasks when first enabled. It does not send recurring reminders for an already notified task or replay completed requests.

Applicants are notified about approval (including partial approval), rejection, modification and cancellation, as well as cancellation requests, rejection of cancellation and withdrawal of a cancellation request. Automatic approvals and approvals resulting from a policy change also generate notifications. Unchanged submissions and rolled-back transactions do not. A pending submission alone does not send a separate confirmation to its applicant.

Cancellation decisions use the policy's eligible roles, independently of recorded approvals for the original request. Either eligible reviewer can resolve a cancellation; the **Both** rule does not introduce a second cancellation-approval stage.

Ordinary changes are collected per account for the next **20:00 Europe/Budapest** digest. A change committed after 20:00 belongs to the following day's digest. The calculation follows daylight-saving transitions. A leave date begins at 00:00 in Budapest; a change is urgent when that start is at most 24 hours away, including already-started leave. When dates change, both the old and new start are considered. Urgent changes bypass the daily digest.

Immediately before delivery, the worker rechecks ownership, request state, approval rules and current reviewer access. Superseded or completed task notifications are omitted. Owner updates remain as a history of changes. Emails contain names, dates, status and links, without leave notes or medical categories. No email is sent on days without relevant changes.

## Keep a worker running

With a MySQL database, `EMAIL_WORKER_ENABLED` defaults to true. The application starts a background worker, wakes it after changes and polls every 30 seconds, including when no web requests arrive. The application process must stay running at 20:00 and for urgent delivery. SQLite defaults to false for development/test isolation; opt in explicitly if using SQLite outside tests.

For a separately supervised process, set `EMAIL_WORKER_ENABLED=false` on the web application and run this command with the same environment and database:

```bash
flask --app run notifications worker
```

Use your process manager to start and restart this process alongside the web application. A one-shot dispatcher is also available:

```bash
flask --app run notifications send-due
```

The continuous worker provides immediate wake-up in the web process or up to 30 seconds of polling latency in a separate process. A cron-only installation delivers urgent messages only at its cron interval. After downtime, overdue messages are processed when a worker resumes. Nothing can send while all application/worker processes are stopped.

## Persistence and recovery

The new `mail_server_settings`, `leave_notifications` and `mail_batches` tables are created by the existing startup table-creation mechanism. Operators can instead run `sql/migrations/2026-10-04-add-mail-settings-and-leave-notifications.sql` before restarting. The script is repeatable and preserves existing data. Both paths respect the actual signed/unsigned integer types of existing user and leave identifiers. No existing leave records are rewritten and no historical changes are invented.

Leave changes and their outbox records commit in the same database transaction. SMTP happens afterwards, so server failures cannot roll back a successful leave decision. Messages remain queued while email is disabled. Failed batches retry with increasing delays from five minutes up to six hours. Multiple workers claim batches atomically, and abandoned claims become retryable after ten minutes. A stable Message-ID is reused on retries.

As with ordinary SMTP outboxes, a process failure after the mail server accepts a message but before the database records success may lead to a duplicate on retry. Stable identifiers assist deduplication but do not promise exactly-once receipt. A transaction arriving after that day's digest was frozen is retained for the next digest rather than generating a second daily summary.

Inspect `mail_batches.status`, `attempts`, `next_attempt_at` and the sanitised `last_error` category to diagnose failures. `disabled`, `configuration`, `authentication`, `connection`, `delivery` and `internal` are categories, not raw provider responses. Deleting an account removes its notification records and clears its attribution on shared SMTP settings; deleting a leave removes associated events.

## Verification

Run `python -m unittest discover -s tests -v`. Notification tests use temporary SQLite databases and mocked SMTP. Schema tests compile MySQL DDL against reflected identifier fixtures. They do not connect to production databases or send email to users.

Any future leave-date, category or note editor must take `snapshot_leave_request()` before changing the request and call `record_leave_change()` before the same commit. This records modifications without copying sensitive notes/categories into email payloads.
