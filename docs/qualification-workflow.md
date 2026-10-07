# Qualification documents and HR processing

The personal **Qualifications** page combines qualifications, professional exams,
teacher continuing professional development and other courses. Employees upload
the supporting document here and can see their own records and processing status.
They cannot enter or change qualification metadata, including through the former
qualification and professional-exam POST routes.

**Management → Qualification processing** is for HR and CEO accounts. Its table
can be filtered by **Uploaded / Processed**, employee and employee-name search.
Each row opens a separate editor with a protected link to the original document.
Managers can save incomplete work as a draft, mark a validated record as processed,
and subsequently correct the data. Concurrent edits are checked using a revision
number. Managers can also record historical qualifications without a document.

### Teacher assessments

The **Teacher assessment** record type stores assessment documents such as
**Pedagógus I. fokozat** and **Pedagógus II. fokozat**. HR records the name,
optional level, issuing institution, document number, acquisition year or exact
date, completion status and notes using the same protected document workflow.
These records appear separately in reports and do not enter the KSH qualification
or study-participation counts. Study/course details, KSH categories and the
highest-qualification marker are unavailable for this type and rejected by the
server if submitted. Changing the record type in the editor restores the other
fields without discarding unsaved values.

An assessment document does not change employment details: the employee's
**Teacher classification** and its effective start date remain on the contract.
The type uses the existing text-based `kind` column, so this addition requires
no database schema change or manual migration.

## Recorded information

The supplied KSH screenshots (`9977.jpg`–`9980.jpg`) inform the categories. These
are stored as reusable classifications rather than questions tied to one year:

| Area | Recorded information |
| --- | --- |
| General details | Record kind, qualification/course name, level, institution, document number and highest qualification |
| Completion | Completed, in progress or discontinued; acquisition year and optional exact date |
| Participation | Actual study start/end dates and the applicable further-study categories |
| Awards | Multiple applicable categories, including degree levels, specialist qualifications, professional exams, IT/ECDL, vocational, language and leadership qualifications |
| Teacher training | Topic, organiser type, funding, hours, credits, attendance mode and digital pedagogy |
| Notes | Additional information recorded by HR |

The study and award classifications may overlap. For example, ECDL is also an IT
qualification, and a leadership subtype also belongs to leadership training.
Classification choices do not determine a person's role or access rights.
Hours are recorded as a number, allowing courses other than the illustrated
30-, 60-, 90- and 120-hour courses.

Processing status is separate from completion: HR can process a record about
ongoing study without claiming that a qualification has already been obtained.
An acquisition year does not imply an exact day or a September–August school year.

## Existing data and deployment

Startup creates `qualification_records` and `qualification_documents`, then
imports every legacy qualification and professional exam. The import preserves
the employee, original text, document number, acquisition year, optional exact
date and highest-qualification flag. Imported records are processed historical
records and do not require a newly uploaded file. Unknown KSH classifications
remain unclassified until HR records them.

The original `educational_qualifications` and `professional_exams` tables remain
intact. The unique source-table/source-ID pair prevents repeat imports and ensures
that restarting the application never overwrites later HR changes. Both source
tables are imported in one transaction. New entries allow multiple professional
exams for the same person. Reports use the unified records, avoiding duplicate
counts of the retained source rows.

The fresh-install SQL schema and an incremental SQL migration accompany the
automatic startup migration. No terminal command is required for the normal
application upgrade. The MySQL bootstrap lock serialises startup migrations and
the existing table-creation helper respects legacy signed/unsigned foreign keys.

## Documents and access

PDF, JPEG, PNG and WebP files up to 10 MiB are stored in the database, not the
hosting instance's temporary filesystem. Lists do not load the binary contents.
Uploads are checked for their actual file type, and image decoding is bounded.
Downloads require the record owner with access to the personal page, or an HR/CEO
account with access to qualification processing. They are served as attachments
with private, non-cached responses.

The existing personal qualification page permission is retained. The separate
professional-exam navigation item is removed. HR processing has its own page
permission in addition to the HR/CEO role requirement. The reporting permission
remains separate from permission to edit records or download their documents.
