# CSUPOR portal manual test guide

This checklist covers every user-facing portal feature. Run it against a disposable database: several steps create and update personnel records, change passwords, and alter leave balances.

## 1. Prepare the test environment

1. Follow the setup steps in `README.md`, start the application with `python run.py`, and open the URL printed by Flask (normally `http://127.0.0.1:5000`).
2. Use a fresh database or take a backup. Keep the server terminal open and treat any traceback or HTTP 500 response as a failure.
3. Test in a desktop browser and once at a narrow/mobile width. On every page, check that text is readable, forms do not overflow, keyboard focus is visible, and links and buttons work.
4. Create these accounts through **Register** (use unique e-mail addresses):

   | Account | Initial role | Purpose |
   | --- | --- | --- |
   | `employee1` | employee | primary employee workflow |
   | `employee2` | employee | overlap, filtering, and access tests |
   | `hr1` | employee, then set to HR | administration |
   | `ceo1` | employee, then set to CEO | leave approval |
   | `leader1` | employee | principal/deputy approval |
   | `developer1` | employee, then set to developer | privilege-only access |

5. Bootstrap the roles using an existing HR/CEO account. If this is a completely fresh database, temporarily update one account in MySQL, for example:

   ```sql
   UPDATE users SET privilege = 'ceo' WHERE username = 'ceo1';
   ```

6. In **Management → Privileges**, assign `hr1` to **hr**, `ceo1` to **ceo**, and `developer1` to **developer**. Leave the other accounts as employees.

Record the actual result and evidence for each numbered section. A feature passes only when the expected data remains correct after refreshing the page and signing out and back in.

## 2. Public pages, registration, and authentication

1. While signed out, visit `/`. Confirm it redirects to **Login**, and that direct visits to `/dashboard`, `/profile`, and `/leaves` redirect to login.
2. On **Register**, submit each missing required field, then register a valid account. Confirm the new account is logged in, has the **employee** role, and receives a numeric user ID.
3. Sign out and try registering the same e-mail and the same username again. Confirm each duplicate is rejected without creating another user.
4. On **Login**, try a wrong password and an unknown identifier. Confirm a generic error appears and no session is created.
5. Log in once with the username and once with the e-mail address. Confirm both reach the dashboard.
6. Use **Logout**. Confirm protected pages are no longer accessible through the browser history without logging in again.
7. Use the language selector on both a signed-out and signed-in page. Switch between **English** and **Magyar**, confirm the page returns to the same location, translated strings appear, and the choice persists while navigating.

## 3. Employee dashboard and personal records

Log in as `employee1`.

### Dashboard

1. Confirm the username, numeric user ID, employee badge, profile-completion percentage, dependent count, and qualification count are correct.
2. Confirm the profile, leave calendar, password, dependent, qualification, and professional-exam actions open the expected pages.
3. Before a contract exists, confirm the leave area explains that no active contract is available.

### Profile

1. Save a complete profile, including dates, gender, addresses, marital status, and optional disability information.
2. Specifically use 9 digits for the social-security number, 10 digits for the tax number, and 11 digits for the education number. Confirm the values appear on the dashboard after saving and the completion indicator reaches 100% when all counted fields are populated.
3. Repeat with letters, too few digits, and too many digits in each numeric identifier. Confirm every invalid save is rejected and previously saved values are not overwritten.
4. Clear optional fields, save, refresh, and confirm they remain empty rather than displaying whitespace.

### Dependents

1. Add a **child** with a name, birth date, 9-digit social-security number, dependency start date, and optional disability note. Add a second record using **other dependent**.
2. Confirm both records and the updated count appear on the dashboard.
3. Try a blank name, missing dependent type, and invalid social-security number. Confirm each is rejected.

### Qualifications and professional exam

1. Add two educational qualifications. Mark the first as highest, then mark the second as highest; confirm only the second retains the highest designation.
2. Try a nonnumeric year and confirm it is rejected.
3. Add a teacher professional exam and confirm it appears on the dashboard. Edit it and confirm the new values persist.
4. Enter an exam year before 1900 and later than next year; confirm both are rejected. Clear all three exam fields and save; confirm the exam is removed.

### Password

1. Try an incorrect current password, an empty new password, and mismatched confirmation; confirm each is rejected.
2. Change to a valid new password, sign out, confirm the old password fails and the new password succeeds, then restore the test password.

## 4. Authorization and privilege management

1. As `employee1`, directly visit `/users/profiles`, `/contracts`, `/legal-entities`, `/places-of-work`, `/leadership`, `/leave-limits`, and `/working-days`. Confirm every request returns **403 Forbidden** and management links are hidden.
2. As `developer1`, directly visit `/users/privileges`; confirm the privilege page is available and a role change can be saved. Confirm the other HR/CEO management URLs still return 403.
3. As `hr1`, open **Management → Privileges**, change `employee2` among employee, HR, CEO, and developer, and confirm each change persists. Restore it to employee.
4. As HR and CEO, confirm all management menu entries appear. Later, after assigning `leader1` a current leadership position, confirm that account sees **Leave approvals** but not HR-only administration.

## 5. HR master data and employee administration

Log in as `hr1`.

### User profiles

1. Open **Management → User profiles**, locate `employee1`, edit its profile, and confirm the change appears when `employee1` logs in.
2. Submit an invalid-length numeric identifier and confirm the manager edit is rejected just like self-service editing.

### Legal entities and workplaces

1. Create legal entity **Test School A** with an address, a 6-digit OM ID, and tax number `12345678-9-01`. Confirm the displayed formatting and saved record.
2. Try a missing required field, a non-6-digit OM ID, and a malformed/non-11-digit tax number. Confirm each is rejected.
3. Edit the entity name/address and confirm the list updates.
4. Create two workplaces for Test School A and one workplace for a second legal entity. Edit one address and confirm the entity association and new address persist.
5. Try saving a workplace without an entity or address and confirm it is rejected.

### Contracts

1. Create active contracts (start date before today, no end date) for `employee1`, `ceo1`, and `leader1` at Test School A. Give `employee1` a teacher-type contract. Use positive weekly hours and a workplace belonging to the selected employer.
2. Create an active contract for `employee2` at the second entity. Confirm all contracts appear under the correct users.
3. Edit a contract's job title, hours, optional good-conduct certificate fields, classification, and end date; confirm changes persist, then restore it to active.
4. Verify validation by trying: missing type/start date/job title/hours/employer/workplace, zero hours, an end date before the start, and a workplace belonging to a different employer. Confirm each save is rejected.

### Leadership

1. Assign `leader1` as **principal** of Test School A using its matching contract and an active date range. Confirm it appears in the existing positions list.
2. Edit it to **deputy principal** and back, confirming both updates.
3. Try a contract from the wrong legal entity, a missing required field, and an end date before the start. Confirm each is rejected.
4. Give the record a past end date and confirm leave-approval access disappears; restore it to active.

## 6. Working calendar and leave limits

Continue as `hr1`.

### Working days

1. Open **Management → Working days**, move to the previous and next months, and confirm month/year rollover works.
2. Pick a normal weekday, choose **Set holiday**, and confirm it is shown as an override after refresh. Choose **Reset** and confirm the default status returns.
3. Pick a weekend or holiday, choose **Set working**, verify the override, then reset it.
4. Add a note to an override if offered and confirm it persists. These overrides must affect paid-leave working-day totals in the leave tests below.

### Leave years and limits

1. Open **Management → Leave limits**, select `employee1`, its active contract, and the current year. Confirm contracts can be filtered by employee and inactive/wrong-year contracts cannot be edited for that year.
2. Open the current leave year. Confirm employees can now submit requests for it. Lock it and confirm submission is blocked, then reopen it.
3. Load year defaults. Confirm default limits appear for all active contracts; use **Undo import** and confirm imported values are removed/restored without deleting manually entered values. Reload defaults.
4. Load and undo defaults for one selected contract and confirm only that contract changes.
5. Save calendar-year limits (basic, age/child supplements, sick leave, and other categories shown for the contract). Confirm negative values are rejected and zero/nonnegative values persist.
6. Add multiple custom date-range limit rows, save, refresh, remove one row, and confirm the remaining rows are correct. Confirm incomplete rows, negative limits, and end-before-start ranges are rejected.
7. Confirm category visibility follows eligibility: child-related categories require a child dependent, and contract-specific categories appear only for eligible contract types.
8. Confirm unpaid leave, sickness benefit, and childcare sickness benefit are identified as categories without an assigned limit.

## 7. Employee leave request workflow

Log in as `employee1`; keep the current leave year open and ensure positive leave limits exist.

1. Open **Leave calendar**. Confirm the active contract is selected, multiple active contracts can be switched, previous/next month navigation works across year boundaries, and usage totals match the selected contract/year.
2. Click one calendar date and then another. Confirm start/end fields and range highlighting update. Change the fields manually and confirm highlighting follows.
3. Select each offered leave category and confirm its help text and end-date requirement update. Category availability should reflect the contract, dependents, and configured limits.
4. Submit paid leave over known working days. Confirm it starts as pending approval, appears on every covered calendar day, appears in **This month**, and reduces the displayed remaining balance by working days only.
5. Confirm a configured working-weekend override counts and a configured weekday-holiday override does not count; reset test overrides afterward.
6. Submit applicable health, childcare, childbirth, exemption, and unpaid categories. Confirm optional open-ended/single-day behavior where offered and correct calendar/usage display.
7. Confirm rejection of: missing start date, required-but-missing end date, end before start, dates outside the contract, a locked/unopened year, an unavailable category, an overlapping request, and paid leave exceeding its balance or validity range.
8. Cancel a **pending approval** request and confirm it becomes cancelled immediately and no longer consumes balance. For an **approved** request, choose Cancel and confirm it becomes pending cancellation; choose Undo and confirm it returns to approved.

## 8. Leave approval workflow and filtering

The normal Test School A request requires both a CEO approval and an active principal/deputy approval. Use separate browser profiles or sign out between roles.

1. As `ceo1`, confirm pending items appear both on the dashboard and under **Management → Leave approvals**. Test filters for legal entity, employee, contract, and every status, plus **Reset**.
2. Approve `employee1`'s request as CEO. Confirm it remains pending until leadership approval and records the CEO approval.
3. As `leader1`, confirm only requests for the leader's legal entity are visible. Approve the same request; confirm it becomes approved and both approvers are shown.
4. Create another request. Reject it from an eligible approver and confirm it becomes rejected and stops consuming the employee's balance.
5. Request cancellation of an approved leave as `employee1`. As an approver, accept the cancellation and confirm status becomes cancelled. Repeat and reject the cancellation; confirm it returns to approved.
6. Confirm `leader1` cannot see or act on `employee2` requests from the second legal entity. Confirm an unrelated employee receives 403 at `/leaves/manage`.
7. Check workplace concurrency information in the manager list by creating overlapping approved/pending requests for multiple employees at the same workplace; confirm the displayed maximum absent count is correct.
8. Test automatic approvals where applicable (for example, when the requester is themselves an eligible CEO or entity leader). Confirm the request records the automatic component and becomes fully approved only when all required components exist.

## 9. Cross-browser, persistence, and cleanup

1. Repeat registration, login, profile save, calendar selection, management dropdown, and leave approval in a second supported browser.
2. Navigate all primary screens at desktop and mobile widths. Confirm tables remain usable, menu items are reachable, calendar controls do not overlap, and forms expose labels to assistive technology.
3. Refresh after every kind of create/edit/action and restart the Flask server once. Confirm persisted records, role restrictions, language choice (within the same browser session), and calculated leave totals remain consistent.
4. Review the server output for exceptions and the browser console for JavaScript errors.
5. Restore changed passwords and working-day overrides. Remove the disposable database or clearly label all test accounts, entities, contracts, leadership assignments, limits, and requests so they cannot be mistaken for production data.

## Completion criteria

The portal is ready only when every section above has an expected result, no protected action is available to an unauthorized role, no save produces a server error, calculations agree with the configured working calendar and limits, approval/cancellation transitions are correct, and data remains correct after refresh and re-login.
