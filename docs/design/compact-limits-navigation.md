# Compact leave limits and grouped navigation

The leave-limit editor fits its available card width. Annual allowances use three columns; custom allowances use four, with start and end dates grouped together. Legal references and entitlement explanations remain available beneath each category in expandable sections. On narrow screens, rows become labelled cards. Existing field names and the add/remove/save workflow are preserved.

My workspace, My records and Management are all collapsible navigation groups. On desktop, clicking a group opens its submenu beside the sidebar. Only one group opens at a time; outside clicks, leaving the group with keyboard focus, Escape and Left close it. Right opens a group and focuses its first link. The current page and its group remain highlighted. On mobile, groups expand inside the existing navigation drawer. Native disclosures remain usable without JavaScript, and role restrictions are unchanged.

Dependent creation and editing now limit the birth-date picker to today's date in Europe/Budapest. Server validation uses the same date before writing any fields. A birth today is accepted; future births are rejected, including direct POST requests. Invalid submissions retain the entered values. A future dependency start remains allowed.

## Verification

- 43 automated tests pass, including future-birth creation/editing, no partial writes, today's boundary, and Budapest/UTC date rollover.
- 119 browser assertions cover Hungarian and English at 320, 390, 768, 1041, 1280 and 1440 pixels, table and document overflow, expanded legal details, saving annual/custom limits, adding/removing custom rows, date-picker validation, navigation keyboard controls, mobile behaviour, no-JavaScript access and role visibility.
- Additional visual and hit-test checks confirm the desktop flyout is visible and clickable after its opening animation and remains within a 700px-high viewport.
- No JavaScript errors were observed. JavaScript syntax and diff whitespace checks pass.

Checks used fictional records in a disposable local SQLite database and Chromium. No schema migration is required. Production MySQL and deployment were not exercised.
