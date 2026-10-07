# Qualifications and professional-exam reports

Authorised managers open **Management → Qualifications and exams** from the menu
or dashboard. HR and CEO accounts have access by default. Developers can change
access in **Settings → Page access**; the same permission protects the overview,
employee directory and individual records, including direct URLs. This permission
does not grant editing rights over another person's own qualification forms.

The **Employees** view lists accounts and their recorded qualifications and
professional exams. Opening an employee shows the stored document details and
completion dates. Records remain visible even if an account has no current
contract, so a change of employment status does not erase its qualification
history. The page displays existing records without changing them.

The overview can be filtered by school year, employee, record category,
qualification type and search text. A school year starts on **1 September** and
ends on **31 August** of the following calendar year. For example, 31 August 2026
belongs to 2025/2026, and 1 September 2026 belongs to 2026/2027. The current year
uses the application's Budapest date.

All date-based statistics use the **date obtained**, not a submission date. Legacy
records containing only a calendar year remain available under the option for
records without an exact date. They are never assigned an invented month or
school year. The notice about such records respects the other selected filters.

Each summary shows both the number of recorded completions and the number of
distinct employees represented. One person with three qualifications contributes
three completions but only one employee to the overall total. Employee counts in
separate groups must not be added together, since a person can appear in several
groups. Breakdowns cover school years, types, qualification names and months.
A selected school year includes all twelve months, including months with no
recorded completions. Type/name grouping ignores differences in letter case and
repeated whitespace while preserving readable labels.

No schema migration is required. The report reads `educational_qualifications`,
`professional_exams` and account/profile names. It reports the existing records;
it does not infer missing qualifications or restore records replaced or removed
by their owners.
