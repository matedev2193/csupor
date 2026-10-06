# Email password recovery

The login page offers **Forgot your password?**. Enter the email address saved
in the account to receive a link and choose a new password of 12–1024 characters.
The link expires after 30 minutes and can be used once. Passwords are never sent
by email. The account is not signed in automatically after recovery.

A developer must first enable email delivery and configure a valid application
URL in **Settings → Email settings**. Recovery messages use this server and are
sent immediately, independently of the evening leave notification digest. The
configured URL is the only source of link origins; browser Host headers cannot
change it. Configure the public HTTPS address in production.

All request responses are identical whether an account exists, email is disabled,
a request is limited or SMTP fails. Account lookup and SMTP run in a bounded
background executor so HTTP response times do not reveal account existence.
Each process permits two concurrent deliveries and sixteen pending/running tasks.
An application restart can interrupt an unsent request; the user can request
another link after one minute. There is no persistent outbound queue for password
recovery. Transient connection and delivery failures are retried once using the
same Message-ID; authentication/configuration failures are not retried. Delivery
errors, including unexpected background exceptions, are logged as fixed reason
codes without provider responses, addresses or secrets. Forked workers initialise
their own executor and capacity instead of inheriting another process's threads.

Shared database limits allow one request per email per minute, three per email
per hour and 120 per server-observed requester address per hour. Only accepted
requests spend an email's hourly allowance; minute-blocked repeated clicks do
not. The larger aggregate allowance accommodates hosting proxies shared by
multiple users while retaining strict per-email limits. Email
identities are compared without case; rate-limit keys are HMACs, not plain email
or IP addresses. Forwarded headers are not trusted for these limits. Deployments
whose proxy exposes one shared remote address also share that requester limit.
Expired buckets and token hashes older than one day are removed on subsequent
valid recovery requests. Limits do not lock login or invalidate existing links.

The reset secret has 256 bits of randomness; the database stores only its SHA-256
digest. The emailed URL holds the secret in a fragment. A small local script moves
it to a hidden form field and removes it from the address bar, so it is absent
from HTTP request URLs, access logs and Referer headers. JavaScript is needed to
open the secure email link. Forms require a session CSRF token, and recovery pages
send `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.

Reset tokens are bound to the current account email and password hash. Changing
either invalidates previous links. Redeeming one atomically claims it and updates
unchanged credentials in a single transaction; competing uses and competing links
cannot both change the password. Previously authenticated sessions are invalidated
by the new password. Merely opening an email link does not consume it.

In **My profile**, the account owner can update their email address in a separate
form by confirming their current password. Addresses are normalised, validated
and checked for case-insensitive uniqueness. The change leaves the personal
profile and password unchanged; future notifications and recovery emails use
the new address. Administrative profile editing does not expose this form.
Both email and signed-in password changes use conditional writes, so an
in-flight request cannot overwrite a concurrent recovery or account edit.

Login sessions contain a keyed fingerprint of the current password hash. The
first deployment of this change requires previously signed-in users to log in
again. A normal password change preserves the confirming browser's session and
revokes earlier sessions elsewhere; recovery requires a fresh login everywhere.

Authorised users can inspect **Settings → Email settings → Recent password reset
emails** without a terminal. The last 20 requests show their status, attempt
count and a fixed reason such as a rate limit, unmatched account, invalid
application URL, authentication failure or unavailable worker. Times use
Europe/Budapest. **Accepted by the email server** means SMTP accepted the message,
not that it reached the inbox. Recipient addresses are visible only through the
protected settings page. No tokens, passwords, message bodies or raw provider
responses are retained. At most 1000 diagnostics are kept; records older than
seven days are hidden and pruned when new requests are recorded. Diagnostic
storage failures do not prevent delivery. Unfinished queued entries can indicate
a process restart; the user can request a new link.

Application startup automatically adds `password_reset_tokens`,
`password_reset_throttles` and `password_reset_deliveries`. The optional additive
SQL migration for tokens and throttles is
`sql/migrations/2026-10-06-add-password-reset.sql`; it preserves existing records
and matches the deployed `users.id` integer type and signedness.
Delivery diagnostics have their own additive migration:
`sql/migrations/2026-10-06-add-password-reset-delivery-diagnostics.sql`.
