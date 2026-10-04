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
from xml.sax.saxutils import escape


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


def export_worktime_csv(register):
    """UTF-8 BOM, semicolon-separated CSV suitable for Hungarian Excel."""
    data = _prepare(register)
    output = StringIO(newline="")
    writer = csv.writer(output, delimiter=";", lineterminator="\r\n")

    def write(values):
        writer.writerow([_csv_cell(value) for value in values])

    write(["Munkaidő-nyilvántartás"])
    write(["Munkavállaló", data["employee_name"]])
    write(["Év", data["year"], "Hónap", MONTHS[data["month"]]])
    write(["Munkakör", data["job_title"]])
    write(["Állapot", data["status_label"]])
    write([TIME_NOTE])
    if data["is_teacher"]:
        write([TEACHER_NOTE])
    write([PARTIAL_WEEK_NOTE])
    write([])
    write([
        "Dátum", "Nap", "Munkakör", "Munkavégzési hely", "Csoport",
        "Munkaidő kezdete", "Munkaidő vége", "Munkaközi szünet (óra:perc)",
        "Ledolgozott idő (óra:perc)", "Neveléssel-oktatással lekötött idő (óra:perc)",
        "Megjegyzés", "Aláírás",
    ])
    for week in data["weeks"]:
        for row in week["rows"]:
            write([
                row["date"].strftime("%Y.%m.%d."), WEEKDAYS[row["date"].weekday()],
                row["job_title"], row["workplace"], row["group_name"], row["start"], row["end"],
                duration(row["break_minutes"]), duration(row["worked_minutes"]),
                duration(row["teaching_minutes"]) if data["is_teacher"] else "",
                _row_note(row), "",
            ])
        write([
            f"{week['label']} összesen", "", "", "", "", "", "",
            duration(sum(row["break_minutes"] for row in week["rows"])),
            duration(sum(row["worked_minutes"] for row in week["rows"])),
            duration(sum(row["teaching_minutes"] for row in week["rows"])) if data["is_teacher"] else "",
            week["period"], "",
        ])
    write([
        "Havi összesen", "", "", "", "", "", "",
        duration(sum(row["break_minutes"] for row in data["rows"])),
        duration(sum(row["worked_minutes"] for row in data["rows"])),
        duration(sum(row["teaching_minutes"] for row in data["rows"])) if data["is_teacher"] else "",
        "", "",
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
    """Paginated A4 landscape PDF with embedded fonts and signature spaces."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    data = _prepare(register)
    _register_fonts()
    output = BytesIO()
    page_width, page_height = landscape(A4)
    margin = 32
    width = page_width - margin * 2
    ink = colors.HexColor("#243a44")
    pale = colors.HexColor("#edf4f3")
    muted = colors.HexColor("#5f6f74")
    style = ParagraphStyle("Register", fontName="CSUPOR", fontSize=8, leading=11, textColor=ink, wordWrap="LTR")
    small = ParagraphStyle("Small", parent=style, fontSize=7, leading=9)
    bold = ParagraphStyle("Bold", parent=style, fontName="CSUPOR-Bold")
    center = ParagraphStyle("Center", parent=style, alignment=TA_CENTER)
    head = ParagraphStyle("Head", parent=small, fontName="CSUPOR-Bold", alignment=TA_CENTER)
    title = ParagraphStyle("Title", parent=bold, fontSize=17, leading=22, spaceAfter=7)

    def p(value, selected=style):
        # User-supplied text is not ReportLab markup. Newlines remain visible.
        text = escape(_text(value)).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
        return Paragraph(text, selected)

    document = SimpleDocTemplate(
        output, pagesize=(page_width, page_height), rightMargin=margin, leftMargin=margin,
        topMargin=32, bottomMargin=42, title="Munkaidő-nyilvántartás",
        author="CSUPOR", subject=f"{data['year']}. {MONTHS[data['month']]}",
    )
    story = [p("Munkaidő-nyilvántartás", title)]
    metadata = [
        [p("Munkavállaló", bold), p(data["employee_name"]), p("Időszak", bold), p(f"{data['year']}. {MONTHS[data['month']]}")],
        [p("Munkakör", bold), p(data["job_title"] or "Nincs megadva"), p("Állapot", bold), p(data["status_label"], small)],
    ]
    metadata_table = LongTable(metadata, colWidths=[82, width - 367, 48, 237])
    metadata_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([metadata_table, Spacer(1, 6)])
    teacher = data["is_teacher"]
    headers = ["Dátum", "Kezdete", "Vége", "Szünet\nó:pp", "Ledolgozott\nó:pp"]
    if teacher:
        headers.append("Nevelés-\noktatás\nó:pp")
    headers.extend(["Munkakör, munkavégzési hely, csoport / megjegyzés", "Aláírás"])
    columns = [55, 43, 43, 42, 60] + ([63] if teacher else [])
    columns.extend([width - sum(columns) - 92, 92])
    table_rows = [[p(value, head) for value in headers]]
    summary_indices = []
    detailed_notes = []
    for week in data["weeks"]:
        for row in week["rows"]:
            details = " | ".join(part for part in (row["job_title"], row["workplace"], row["group_name"]) if part)
            note = _row_note(row)
            if len(note) > 360 or note.count("\n") > 4:
                # Keep the attendance/signature row usable. The entire note is
                # printed below the table, where paragraphs paginate naturally.
                detailed_notes.append((row["date"], note))
                note = "; ".join(part for part in (
                    row["absence_label"], f"Részletes megjegyzés: {len(detailed_notes)}."
                ) if part)
            if note:
                details += ("\n" if details else "") + note
            values = [
                p(f"{row['date']:%m.%d.}\n{WEEKDAYS[row['date'].weekday()]}", small),
                p(row["start"] or "-", center), p(row["end"] or "-", center),
                p(duration(row["break_minutes"]), center), p(duration(row["worked_minutes"]), center),
            ]
            if teacher:
                values.append(p(duration(row["teaching_minutes"]), center))
            values.extend([p(details, small), ""])
            table_rows.append(values)
        totals = [p(f"{week['label']} összesen", bold), "", "", p(duration(sum(row["break_minutes"] for row in week["rows"])), center), p(duration(sum(row["worked_minutes"] for row in week["rows"])), center)]
        if teacher:
            totals.append(p(duration(sum(row["teaching_minutes"] for row in week["rows"])), center))
        totals.extend([p(week["period"], small), ""])
        summary_indices.append(len(table_rows))
        table_rows.append(totals)
    totals = [p("Havi összesen", bold), "", "", p(duration(sum(row["break_minutes"] for row in data["rows"])), center), p(duration(sum(row["worked_minutes"] for row in data["rows"])), center)]
    if teacher:
        totals.append(p(duration(sum(row["teaching_minutes"] for row in data["rows"])), center))
    totals.extend(["", ""])
    summary_indices.append(len(table_rows))
    table_rows.append(totals)
    table = LongTable(table_rows, colWidths=columns, repeatRows=1, splitByRow=1, splitInRow=1, hAlign=TA_LEFT)
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, 0), pale),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#b8c7c7")),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd6d6")),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for index in summary_indices:
        commands.extend([
            ("SPAN", (0, index), (2, index)),
            ("NOSPLIT", (0, index - 1), (-1, index)),
            ("BACKGROUND", (0, index), (-1, index), pale),
            ("LINEABOVE", (0, index), (-1, index), 0.6, colors.HexColor("#8aa6a5")),
        ])
    table.setStyle(TableStyle(commands))
    story.extend([table, Spacer(1, 9), p(TIME_NOTE, small)])
    if teacher:
        story.append(p(TEACHER_NOTE, small))
    if any(week["partial"] for week in data["weeks"]):
        story.append(p(PARTIAL_WEEK_NOTE, small))
    if not data["rows"]:
        story.append(p("A kiválasztott hónapban nincs nyilvántartott munkanap.", small))
    for number, (day, note) in enumerate(detailed_notes, 1):
        story.extend([Spacer(1, 9), p(f"{number}. részletes megjegyzés - {day:%Y.%m.%d.}", bold), p(note, small)])
    story.extend([Spacer(1, 16), p("Kelt: ________________________       Ellenőrizte: ________________________", small)])

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#cbd6d6"))
        canvas.line(margin, 29, page_width - margin, 29)
        canvas.setFont("CSUPOR", 7)
        canvas.setFillColor(muted)
        # The draft label is repeated on every page, including long registers.
        caption = "CSUPOR | " + data["status_label"] + f" | {data['year']}.{data['month']:02d}."
        canvas.drawString(margin, 17, caption)
        canvas.drawRightString(page_width - margin, 17, f"{doc.page}. oldal")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
