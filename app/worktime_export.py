"""Printable Hungarian working-time registers, independent of ORM/session state.

Both public functions return bytes. ``register`` is a mapping with
``employee_name``, ``year``, ``month``, ``status``, optional ``job_title`` and
``is_teacher``, and ``rows``. A row has ``date`` (date or ISO date), ``start`` /
``end`` (HH:MM or None), integer ``worked_minutes``, optional ``break_minutes``
and ``teaching_minutes``, plus ``job_title``, ``workplace``, ``group_name``,
``note`` and ``absence_label``. Multiple contract rows on one day are retained.
Only working days belong in this mapping; the service owns calendar rules.
"""

import calendar
import csv
from collections import OrderedDict
from datetime import date, datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path
import re
from threading import Lock


MONTHS = (
    "", "január", "február", "március", "április", "május", "június",
    "július", "augusztus", "szeptember", "október", "november", "december",
)
WEEKDAYS = ("hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap")
TEACHER_NOTE = (
    "Pedagógusoknál az összesítés a kötött munkaidőt tartalmazza; "
    "a neveléssel-oktatással lekötött idő külön szerepel."
)
DRAFT_LABEL = "TERVEZET - még nem jóváhagyott beosztás"
PARTIAL_WEEK_NOTE = (
    "A hónaphatáron átnyúló hetek összesítése csak az ebben a hónapban szereplő napokat tartalmazza."
)
TIME_NOTE = "Az időtartamok óra:perc formátumúak; a ledolgozott idő a munkaközi szünetet nem tartalmazza."
_FONT_LOCK = Lock()


def duration(minutes):
    """Format a duration without wrapping after 24 hours."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _text(value):
    return "" if value is None else str(value)


def _minutes(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer number of minutes")
    return value


def _time(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("start and end must use HH:MM")
    return value


def _prepare(register):
    year, month = int(register["year"]), int(register["month"])
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    result = dict(register, year=year, month=month)
    rows = []
    for item in register.get("rows", []):
        row = dict(item)
        day = row["date"]
        if isinstance(day, datetime):
            day = day.date()
        if isinstance(day, str):
            day = date.fromisoformat(day)
        if not isinstance(day, date) or not first <= day <= last:
            raise ValueError("Every exported date must belong to the selected month")
        row["date"] = day
        for name in ("worked_minutes", "break_minutes", "teaching_minutes"):
            row[name] = _minutes(row.get(name, 0), name)
        if row["teaching_minutes"] > row["worked_minutes"]:
            raise ValueError("Teaching time cannot exceed worked time")
        row["start"], row["end"] = _time(row.get("start")), _time(row.get("end"))
        if row["worked_minutes"] == 0:
            # Absences never display a phantom arrival/departure time.
            row["start"] = row["end"] = ""
        row["job_title"] = _text(row.get("job_title") or register.get("job_title"))
        for name in ("workplace", "group_name", "note", "absence_label"):
            row[name] = _text(row.get(name))
        rows.append(row)
    rows.sort(key=lambda row: row["date"])
    result["rows"] = rows
    result["employee_name"] = _text(register.get("employee_name"))
    result["job_title"] = _text(register.get("job_title")) or "; ".join(
        dict.fromkeys(row["job_title"] for row in rows if row["job_title"])
    )
    result["is_teacher"] = bool(register.get("is_teacher")) or any(row["teaching_minutes"] for row in rows)
    result["status_label"] = "Jóváhagyott beosztás" if register.get("status") == "confirmed" else DRAFT_LABEL
    weeks = OrderedDict()
    for row in rows:
        monday = row["date"] - timedelta(days=row["date"].weekday())
        weeks.setdefault(monday, []).append(row)
    result["weeks"] = []
    for monday, week_rows in weeks.items():
        sunday = monday + timedelta(days=6)
        iso = monday.isocalendar()
        partial = monday < first or sunday > last
        label = f"{iso.year} / {iso.week}. hét"
        period = f"{max(first, monday):%m.%d.} - {min(last, sunday):%m.%d.}"
        if partial:
            period += " (havi részlet)"
        result["weeks"].append({"label": label, "period": period, "partial": partial, "rows": week_rows})
    return result


def _csv_cell(value):
    text = _text(value)
    # All editable values, including metadata and notes, pass here. A leading
    # whitespace/control character must not hide an Excel formula introducer.
    prefix = re.sub(r"^[\s\x00-\x1f\ufeff]+", "", text)
    if prefix.startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def _row_note(row):
    return "; ".join(part for part in (row["absence_label"], row["note"]) if part)


def _assignments(data):
    """Keep contract/site/group changes addressable from the header."""
    assignments = OrderedDict()
    for row in data["rows"]:
        key = (row.get("contract_id"), row["job_title"], row["workplace"], row["group_name"])
        if key not in assignments:
            assignments[key] = {
                "code": f"B{len(assignments) + 1}", "job_title": row["job_title"],
                "workplace": row["workplace"], "group_name": row["group_name"], "rows": [],
            }
        assignments[key]["rows"].append(row)
        row["assignment_code"] = assignments[key]["code"]
    return list(assignments.values())


def _days_label(rows):
    days = sorted({row["date"].day for row in rows})
    runs = []
    for day in days:
        if runs and day == runs[-1][-1] + 1:
            runs[-1].append(day)
        else:
            runs.append([day])
    return ", ".join(f"{run[0]:02d}-{run[-1]:02d}" if len(run) > 1 else f"{run[0]:02d}" for run in runs)


def export_worktime_csv(register):
    """Complete UTF-8 CSV; header assignments replace daily metadata columns."""
    data = _prepare(register)
    assignments = _assignments(data)
    multiple = len(assignments) > 1
    output = StringIO(newline="")
    writer = csv.writer(output, delimiter=";", lineterminator="\r\n")

    def write(values):
        writer.writerow([_csv_cell(value) for value in values])

    write(["Munkaidő-nyilvántartás"])
    write(["Munkavállaló", data["employee_name"]])
    write(["Év", data["year"], "Hónap", MONTHS[data["month"]]])
    write(["Munkakör", data["job_title"]])
    for name, key in (("Munkavégzési hely", "workplace"), ("Csoport", "group_name")):
        write([name, "; ".join(dict.fromkeys(row[key] for row in data["rows"] if row[key]))])
    write(["Állapot", data["status_label"]])
    if multiple:
        write(["Beosztási jelölés", "Munkakör", "Munkavégzési hely", "Csoport", "A hónap napjai", "Nap és idősáv"])
        for assignment in assignments:
            periods = "; ".join(
                f"{row['date']:%Y.%m.%d.} {row['start'] or '-'} - {row['end'] or '-'}"
                for row in assignment["rows"]
            )
            write([assignment["code"], assignment["job_title"], assignment["workplace"],
                   assignment["group_name"], _days_label(assignment["rows"]), periods])
    write([TIME_NOTE])
    if data["is_teacher"]:
        write([TEACHER_NOTE])
    write([PARTIAL_WEEK_NOTE])
    write([])
    write([
        "Dátum", "Nap", "Munkaidő kezdete", "Munkaidő vége", "Munkaközi szünet (óra:perc)",
        "Ledolgozott idő (óra:perc)", "Neveléssel-oktatással lekötött idő (óra:perc)",
        "Megjegyzés", "Aláírás",
    ])
    for week in data["weeks"]:
        for row in week["rows"]:
            note = _row_note(row)
            if multiple:
                note = f"[{row['assignment_code']}]" + (" " + note if note else "")
            write([
                row["date"].strftime("%Y.%m.%d."), WEEKDAYS[row["date"].weekday()],
                row["start"], row["end"], duration(row["break_minutes"]), duration(row["worked_minutes"]),
                duration(row["teaching_minutes"]) if data["is_teacher"] else "", note, "",
            ])
        write([
            f"{week['label']} összesen", "", "", "",
            duration(sum(row["break_minutes"] for row in week["rows"])),
            duration(sum(row["worked_minutes"] for row in week["rows"])),
            duration(sum(row["teaching_minutes"] for row in week["rows"])) if data["is_teacher"] else "",
            week["period"], "",
        ])
    write([
        "Havi összesen", "", "", "", duration(sum(row["break_minutes"] for row in data["rows"])),
        duration(sum(row["worked_minutes"] for row in data["rows"])),
        duration(sum(row["teaching_minutes"] for row in data["rows"])) if data["is_teacher"] else "", "", "",
    ])
    return output.getvalue().encode("utf-8-sig")


def _register_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # These licensed fonts are deployed with the application. Do not depend on
    # host operating-system fonts or a remote service for Hungarian glyphs.
    with _FONT_LOCK:
        if "CSUPOR" not in pdfmetrics.getRegisteredFontNames():
            root = Path(__file__).parent / "static" / "fonts"
            pdfmetrics.registerFont(TTFont("CSUPOR", str(root / "DejaVuSans.ttf")))
            pdfmetrics.registerFont(TTFont("CSUPOR-Bold", str(root / "DejaVuSans-Bold.ttf")))
            pdfmetrics.registerFontFamily("CSUPOR", normal="CSUPOR", bold="CSUPOR-Bold")


def export_worktime_pdf(register):
    """One portrait A4 page with bounded text and complete CSV cross-references.

    A date has one signature row even when several contracts apply. In that
    case the PDF states the first arrival and last departure, adds an explicit
    multiple-interval marker, and totals only the actual worked minutes. CSV
    keeps every individual interval and every unabridged note.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas as canvas_module

    data = _prepare(register)
    assignments = _assignments(data)
    multiple = len(assignments) > 1
    _register_fonts()
    output = BytesIO()
    page_width, page_height = A4
    canvas = canvas_module.Canvas(output, pagesize=A4)
    canvas.setTitle("Munkaidő-nyilvántartás")
    canvas.setAuthor("CSUPOR")
    canvas.setSubject(f"{data['year']}. {MONTHS[data['month']]}")
    margin = 25
    width = page_width - 2 * margin
    ink = colors.HexColor("#243a44")
    line = colors.HexColor("#b9c9c9")
    pale = colors.HexColor("#edf4f3")
    canvas.setFillColor(ink)
    abbreviated = False

    def compact(text, max_width, size=7.5, font="CSUPOR"):
        nonlocal abbreviated
        text = " ".join(_text(text).split())
        if stringWidth(text, font, size) <= max_width:
            return text
        abbreviated = True
        suffix = "…"
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if stringWidth(text[:middle] + suffix, font, size) <= max_width:
                low = middle
            else:
                high = middle - 1
        return text[:low].rstrip() + suffix

    def text_at(text, x, y, max_width, size=7.5, *, bold=False, centered=False):
        font = "CSUPOR-Bold" if bold else "CSUPOR"
        canvas.setFont(font, size)
        canvas.setFillColor(ink)
        value = compact(text, max_width, size, font)
        if centered:
            canvas.drawCentredString(x + max_width / 2, y, value)
        else:
            canvas.drawString(x, y, value)

    def header_line(label, value, y):
        text_at(label, margin, y, 89, 7.8, bold=True)
        text_at(value or "Nincs megadva", margin + 92, y, width - 92, 7.8)
        return y - 12

    y = page_height - 34
    text_at("Munkaidő-nyilvántartás", margin, y, width - 133, 14, bold=True)
    text_at(f"{data['year']}. {MONTHS[data['month']]}", page_width - margin - 130, y + 1, 130, 10, bold=True)
    y -= 21
    y = header_line("Munkavállaló", data["employee_name"], y)
    y = header_line("Munkakör", data["job_title"], y)
    for label, key in (("Munkavégzési hely", "workplace"), ("Csoport", "group_name")):
        value = "; ".join(dict.fromkeys(row[key] for row in data["rows"] if row[key]))
        y = header_line(label, value, y)
    text_at(data["status_label"], margin, y, width, 7.6, bold=data.get("status") != "confirmed")
    y -= 13

    if multiple:
        # Codes in the daily notes refer to these dated header assignments.
        # The complete mapping, including clock times, always exists in CSV.
        max_assignments = 4
        for assignment in assignments[:max_assignments]:
            description = " | ".join(part or "-" for part in (
                assignment["job_title"], assignment["workplace"], assignment["group_name"]))
            text_at(f"{assignment['code']} ({_days_label(assignment['rows'])}. nap): {description}",
                    margin, y, width, 6.8)
            y -= 10
        if len(assignments) > max_assignments:
            abbreviated = True
            text_at(f"További {len(assignments) - max_assignments} beosztás és a teljes hozzárendelés: CSV.",
                    margin, y, width, 6.8)
            y -= 10
        y -= 3

    teacher = data["is_teacher"]
    # Deduplicated legend codes retain both absence and merge meaning, even if
    # the source text is much longer than the space on the paper register.
    legend = OrderedDict()

    def annotation(row):
        pieces = []
        if row["absence_label"]:
            key = ("T", row["absence_label"])
            if key not in legend:
                legend[key] = f"T{1 + sum(k[0] == 'T' for k in legend)}"
            pieces.append(legend[key])
        if row["note"]:
            kind = "Ö" if re.search(r"összevon|merged? group", row["note"], re.IGNORECASE) else "M"
            key = (kind, row["note"])
            if key not in legend:
                legend[key] = f"{kind}{1 + sum(k[0] == kind for k in legend)}"
            pieces.append(legend[key])
        return pieces

    display_weeks = []
    multiple_intervals = False
    for week in data["weeks"]:
        days = OrderedDict()
        for row in week["rows"]:
            days.setdefault(row["date"], []).append(row)
        day_rows = []
        for day, rows in days.items():
            active = [row for row in rows if row["worked_minutes"] > 0]
            intervals = [(row["start"], row["end"]) for row in active]
            separated = len(intervals) > 1
            multiple_intervals |= separated
            codes = list(dict.fromkeys(code for row in rows for code in annotation(row)))
            flags = []
            if any(row["absence_label"] for row in rows):
                flags.append("Távollét")
            if any(code.startswith("Ö") for code in codes):
                flags.append("Összevonás")
            if multiple:
                codes = list(dict.fromkeys(row["assignment_code"] for row in rows)) + codes
            # Flags come before codes so long lists can never hide a critical
            # absence or merge. The truncation symbol points to the full CSV.
            note = "; ".join(flags + ([", ".join(codes)] if codes else []))
            if separated:
                note = "Több idősáv; " + note
            start = min((item[0] for item in intervals if item[0]), default="-")
            end = max((item[1] for item in intervals if item[1]), default="-")
            day_rows.append({
                "date": day, "start": start, "end": end, "note": note,
                **{field: sum(row[field] for row in rows) for field in ("break_minutes", "worked_minutes", "teaching_minutes")},
            })
        display_weeks.append((week, day_rows))

    headers = ["Nap", "Kezdete", "Vége", "Szünet", "Ledolgozott"]
    columns = [47, 40, 40, 40, 54]
    if teacher:
        headers.append("Nevelési idő")
        columns.append(60)
    columns.extend([width - sum(columns) - 94, 94])
    headers.extend(["Jelölés", "Aláírás"])
    col_x = [margin]
    for size in columns:
        col_x.append(col_x[-1] + size)
    table_top = y
    header_height, weekly_height, monthly_height = 23, 12.5, 15
    number_of_days = sum(len(rows) for _, rows in display_weeks)
    # Reserve the lower block before sizing rows; 31 dates + six weeks fit at
    # normal print sizes, without shrinking the entire page or adding a page.
    lower_block = 130 if legend or multiple_intervals else 94
    row_height = min(20, (table_top - lower_block - header_height - len(display_weeks) * weekly_height - monthly_height) / max(1, number_of_days))
    row_height = max(14, row_height)

    def background(top, height, shaded=False):
        if shaded:
            canvas.setFillColor(pale)
            canvas.rect(margin, top - height, width, height, fill=1, stroke=0)
        canvas.setStrokeColor(line)
        canvas.setLineWidth(0.35)
        canvas.line(margin, top - height, margin + width, top - height)

    background(y, header_height, True)
    for i, label in enumerate(headers):
        text_at(label, col_x[i] + 2, y - 10, columns[i] - 4, 6.5, bold=True, centered=True)
        if label in ("Szünet", "Ledolgozott", "Nevelési idő"):
            text_at("óra:perc", col_x[i] + 2, y - 19, columns[i] - 4, 6.2, centered=True)
    y -= header_height
    short_days = ("H", "K", "Sze", "Cs", "P", "Szo", "V")

    def values_at(values, top, height, *, summary=False):
        baseline = top - height / 2 - 2.5
        for index, value in enumerate(values):
            if index == 0 and summary:
                text_at(value, col_x[0] + 4, baseline, sum(columns[:3]) - 8, 7, bold=True)
            elif summary and index in (1, 2):
                continue
            else:
                text_at(value, col_x[index] + 3, baseline, columns[index] - 6,
                        6.8 if index == len(columns) - 2 else 7.6,
                        bold=summary, centered=index < len(columns) - 2 and index != 0)

    def totals(rows, label, note=""):
        values = [label, "", "", duration(sum(row["break_minutes"] for row in rows)),
                  duration(sum(row["worked_minutes"] for row in rows))]
        if teacher:
            values.append(duration(sum(row["teaching_minutes"] for row in rows)))
        return values + [note, ""]

    for week, rows in display_weeks:
        for row in rows:
            background(y, row_height)
            values = [f"{row['date'].day:02d}. {short_days[row['date'].weekday()]}", row["start"], row["end"],
                      duration(row["break_minutes"]), duration(row["worked_minutes"])]
            if teacher:
                values.append(duration(row["teaching_minutes"]))
            values.extend([row["note"], ""])
            values_at(values, y, row_height)
            y -= row_height
        background(y, weekly_height, True)
        label = week["label"] + ("*" if week["partial"] else "") + " össz."
        values_at(totals(week["rows"], label), y, weekly_height, summary=True)
        y -= weekly_height
    background(y, monthly_height, True)
    values_at(totals(data["rows"], "Havi összesen"), y, monthly_height, summary=True)
    y -= monthly_height
    canvas.setStrokeColor(line)
    canvas.rect(margin, y, width, table_top - y, fill=0, stroke=1)
    # Vertical borders across summary rows are intentionally limited to the
    # numeric columns; the first three columns form the summary label.
    for x in col_x[3:-1]:
        canvas.line(x, y, x, table_top)
    for x in col_x[1:3]:
        cursor = table_top - header_height
        canvas.line(x, table_top, x, cursor)
        for _, rows in display_weeks:
            bottom = cursor - len(rows) * row_height
            canvas.line(x, cursor, x, bottom)
            cursor = bottom - weekly_height
    y -= 11

    notes = ["Az időtartamok óra:percben értendők. A ledolgozott idő nem tartalmazza a munkaközi szünetet."]
    if teacher:
        notes.append("Pedagógus: a ledolgozott idő a kötött munkaidő; a neveléssel-oktatással lekötött idő külön szerepel.")
    if any(week["partial"] for week, _ in display_weeks):
        notes.append("* Havi részlet: a hét összesítésében csak a kiválasztott hónap napjai szerepelnek.")
    if multiple_intervals:
        notes.append("Több idősáv: az első kezdés és az utolsó végzés szerepel; az egyes időszakok a CSV-ben láthatók.")
    # At most four legend lines are printed. All individual notes remain
    # complete in CSV, and omitted codes are explicitly identified below.
    legend_items = sorted(legend.items(), key=lambda item: {"T": 0, "Ö": 1, "M": 2}[item[0][0]])
    for (kind, note), code in legend_items[:4]:
        notes.append(f"{code}: {note}")
    if len(legend) > 4:
        abbreviated = True
        notes.append(f"További {len(legend) - 4} jelölés teljes szövege: CSV. T = távollét; Ö = összevonás; M = megjegyzés.")
    if not data["rows"]:
        notes.append("A kiválasztott hónapban nincs nyilvántartott munkanap.")
    # Reserve a line for an explicit abridgement notice and the signatures.
    for note in notes:
        if y < 49:
            abbreviated = True
            break
        text_at(note, margin, y, width, 6.6)
        y -= 9
    if abbreviated:
        text_at("… = rövidített szöveg. A teljes fejléc, beosztási hozzárendelés és minden megjegyzés a CSV-exportban szerepel.",
                margin, max(39, y), width, 6.5, bold=True)
        y -= 10
    signature_y = max(25, y - 6)
    text_at("Kelt: ____________________       Ellenőrizte: ____________________", margin, signature_y, width - 35, 7)
    text_at("1 / 1", margin + width - 30, signature_y, 30, 7)
    canvas.showPage()
    canvas.save()
    return output.getvalue()
