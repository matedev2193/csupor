# Page access

Developers can open **Page access** (**Oldalhozzáférések** in Hungarian) under **Settings → Page access**, at `/page-access`. The Settings menu also contains **Privileges** and **Email settings**, showing only the pages the current user can access. The table has one row per application page and one column per stored privilege: employee, HR, director (`ceo`) and developer. Tick a cell to allow the page, clear it to deny the page, then save. Settings apply to all users with that privilege on their next request, including already signed-in sessions.

The editor's own row is fixed: developers always have access and other privileges never do. Both the form and server enforce this rule, independently of saved permission rows. A developer also cannot remove their own developer privilege on the privileges page. Other pages, including email settings, can be granted or revoked through the matrix.

## Scope

The same policy controls sidebar items, dashboard sections and links, direct URLs and form submissions. Child pages and actions inherit their parent page's permission: for example, denying Groups also denies its create/edit forms and legacy URL; denying user profiles also denies editing and deleting users. A denied authenticated request returns HTTP 403 before the handler reads or changes page data. Newly registered application endpoints must explicitly declare a page policy or utility status; otherwise they are denied.

Page access does not replace record ownership or business rules:

- Personal records still belong to the signed-in user. Viewing one's own leave calendar or working-time register also requires at least one contract, including an inactive, historical or future contract. This applies to every privilege, even developers, and cannot be bypassed by a page grant. Accounts that have never had a contract have neither sidebar links nor dashboard cards for those pages, and direct GET/POST requests return HTTP 403. Managing other employees' schedules requires Working-time management; access to Groups alone does not grant schedule access or exports.
- Leave approvals also require director privilege or an active nursery leadership appointment. Approval mode, legal-entity scope, assignment dates and restrictions on approving one's own requests still apply. The matrix can remove an eligible reviewer's access; a tick alone does not appoint a reviewer. This conditional row is enabled for all four privileges by default so existing nursery heads retain access.
- Account deletion still requires the acting user's password and retains the self-deletion and final-director protections. Owning an avatar permits displaying it in the account area even if profile editing is disabled; other users' avatars require access to User profiles. Photo uploads, editing and original image retrieval require My profile access and ownership.
- GYÁP downloads require access to Leave calendar, Leave approvals or GYÁP forms. The existing filename/year checks remain in place.

Login, logout, password reset and language switching remain available. Changing one's own account email inherits My profile access. After login or a successful personal-record save, the application chooses an allowed landing page if the dashboard is disabled. Accounts with no available page see an explanatory screen with logout. Empty menu groups and dashboard sections are hidden. Authenticated responses disallow browser caching while retaining existing privacy directives.

Reviewer notification eligibility follows the same page policy. Revoked reviewers' outstanding task emails are filtered before delivery. Granting access to a newly eligible reviewer queues their outstanding tasks in the same transaction as the permission update. Applicant status notifications and the configured daily/urgent timing remain unchanged.

## Persistence and migration

Two tables are added:

| Table | Purpose |
| --- | --- |
| `page_role_permissions` | Explicit Boolean override for each page/privilege pair |
| `page_access_settings` | Singleton revision, last editor and edit time |

Missing tables are created by application startup. The explicit, repeatable MySQL/MariaDB migration is [`2026-10-05-add-page-access-permissions.sql`](../sql/migrations/2026-10-05-add-page-access-permissions.sql); the full installation schema includes both tables too. The migration inherits the actual size and signedness of `users.id` for the editor reference. It creates only missing tables, changes no existing columns or user privileges, and never resets saved permissions. Startup creates a revision row when missing. Missing permission rows use the established access defaults until a developer saves the matrix.

Saving is CSRF-protected, validates all submitted cells, and commits the entire matrix atomically. An optimistic revision check rejects stale or concurrent edits rather than mixing two administrators' choices. The policy is read from the database per request; worker checks also read committed settings. There is no process-wide permission cache. Deleting a past editor clears the audit reference without deleting the settings.

## Verification

The automated tests cover the endpoint registry and denied GET/POST requests, grants and revocations across roles, fixed developer access, form validation and CSRF, stale/concurrent rollback, notification eligibility, scoped downloads, schema compatibility and persistence. Existing personnel, working-time, photo, leave and SMTP regression tests run alongside them. Development checks use isolated SQLite databases and mocked email delivery; they do not connect to the deployed database or send live email.
