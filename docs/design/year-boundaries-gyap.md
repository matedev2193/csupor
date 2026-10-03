# Year boundaries and annual GYÁP forms

Leave requests now check every calendar year touched by their dates. If any year is closed or has not been opened, the error names those years before checking allowance capacity. A request crossing two open years remains one approval request, while working days are charged to the balances valid for each year. Custom validity periods use a shared remaining capacity across years, including existing requests. Numerical shortages and dates outside a balance's validity produce distinct messages.

## Annual blank forms

HR and Director accounts can open **Management → GYÁP forms** (`/gyap-forms`) and upload a blank childcare sickness benefit form for a selected year. Supported formats are PDF, DOC and DOCX, up to 10 MiB. Uploading another valid document for the same year replaces that year's document; validation failures preserve the previous file. Years can be prepared before leave requests are opened for them.

Choosing childcare sickness benefit in the leave form shows download links for each year in the entered date range. Dates selected through the calendar update the links too. Missing forms are identified by year with guidance to contact HR. The submitted request card also carries the links, including when viewed in the next year's calendar. A missing form does not prevent an otherwise valid absence request.

Downloads require sign-in and are served as attachments with private, no-store caching and nosniff. Uploaded content is held in the database, so it survives application restarts and deployments with ephemeral application disks. The new `gyap_forms` table is created on startup; its uploader foreign key follows the existing MySQL signed/unsigned identifier handling. The SQL installation script includes the same table. Metadata queries do not fetch document bytes.

## Verification

- 66 automated tests pass, including closed endpoint/middle years, separate annual balances, shared custom capacity, validity errors, upload permissions, CSRF, damaged/oversized documents, replacement, exact download bytes, application restart and MySQL DDL compatibility.
- 68 browser assertions cover upload/replacement/rejection, employee permissions, closed/open year submission, correct links before and after GYÁP submission, both December and January views, missing years, calendar clicks and the JavaScript-free fallback.
- Hungarian and English layouts checked at 320, 390, 768, 1041 and 1440 pixels with no document overflow or JavaScript errors. Desktop and mobile screenshots were inspected.
- The Hungarian catalogue is translated and compiled. Diff whitespace checks pass.

Verification used fictional records in disposable SQLite databases and Chromium. Production MySQL and deployment were not exercised.
