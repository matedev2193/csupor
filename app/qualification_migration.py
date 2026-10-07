"""Lossless, repeatable import of legacy qualifications into the unified register."""

from sqlalchemy import JSON, and_, insert, literal, select

from .models import EducationalQualification, ProfessionalExam
from .qualification_models import QualificationRecord, utc_now


def migrate_legacy_qualifications(engine):
    """Import each old record once, keeping the source tables as an archive.

    Both imports share a transaction. The unique source/ID pair prevents a
    second import, and existing records are never updated: subsequent HR edits
    therefore survive every restart. MySQL callers hold the bootstrap lock.
    A year-only record remains year-only, without guessing a day or category.
    """
    target = QualificationRecord.__table__
    now = utc_now()
    with engine.begin() as connection:
        for source, kind in (
            (EducationalQualification.__table__, "qualification"),
            (ProfessionalExam.__table__, "exam"),
        ):
            values = {
                "user_id": source.c.user_id,
                "status": literal("processed"),
                "kind": literal(kind),
                "qualification_name": source.c.qualification_name,
                "degree_number": source.c.degree_number,
                "year_obtained": source.c.year_obtained,
                "date_obtained": source.c.date_obtained,
                "completion_state": literal("completed"),
                "created_at": literal(now),
                "updated_at": literal(now),
                "processed_at": literal(now),
                "legacy_source": literal(source.name),
                "legacy_id": source.c.id,
            }
            if kind == "qualification":
                values.update(
                    level_or_type=source.c.level_or_type,
                    institution_name=source.c.institution_name,
                    highest=source.c.highest,
                )
            else:
                values["award_categories"] = literal(["professional_exam"], type_=JSON)
            # MySQL allows the INSERT target in the main SELECT's FROM clause,
            # but rejects it in a NOT EXISTS subquery (error 1093).
            imported = target.alias("imported")
            pending = select(*values.values()).select_from(source.outerjoin(imported, and_(
                imported.c.legacy_source == source.name,
                imported.c.legacy_id == source.c.id,
            ))).where(imported.c.id.is_(None))
            connection.execute(insert(target).from_select(list(values), pending))
