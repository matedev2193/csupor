# Qualifications and professional-exam reports

Authorised managers open **Management → Qualification reports** from the menu
or dashboard. HR and CEO accounts have access by default. Developers can change
access in **Settings → Page access**; the same permission protects the overview,
employee directory and individual records, including direct URLs. This permission
does not grant editing rights or access to another person's uploaded documents.
HR/CEO accounts with the separate **Qualification processing** permission can
open an editor from an employee's record. See [document processing](qualification-workflow.md).

The **Employees** view lists accounts and their recorded qualifications,
professional exams, teacher training and other courses, including uploaded records
awaiting processing and ongoing studies. Opening an employee shows stored metadata
and completion dates. Records remain visible even if an account has no current
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

Completion summaries include only processed, completed records. KSH participation
summaries separately use recorded study periods overlapping the selected school
year, including ongoing studies. Records with an incomplete study period are
identified rather than assigned an invented period. Further breakdowns cover the
supplied study and award classifications, training topic, organiser, funding,
hours, attendance mode and digital pedagogy. Overlapping classifications display
distinct employees and record counts and must not be added as if they were
disjoint groups.

Reports now read only `qualification_records` and account/profile names. Startup
imports the retained legacy qualification and exam tables into that register
once; reading only the unified register avoids double counting. No qualification,
study period or missing completion date is inferred from an uploaded file.
