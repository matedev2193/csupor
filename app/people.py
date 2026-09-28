"""Names and workplace birthday reminders without exposing birth years."""
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import joinedload

from . import db
from .models import Contract, User, UserProfile


def local_today():
    return datetime.now(ZoneInfo("Europe/Budapest")).date()


def display_name(user):
    return user.profile.full_name if user.profile and user.profile.full_name else user.username


def birthday_context(user, today=None):
    today = today or local_today()
    birthday = user.profile.date_of_birth if user.profile else None
    if birthday and birthday <= today and (birthday.month, birthday.day) == (today.month, today.day):
        return {"is_own_birthday": True, "birthday_colleagues": []}
    active = db.and_(Contract.start_date <= today, db.or_(Contract.end_date.is_(None), Contract.end_date >= today))
    workplaces = db.select(Contract.place_of_work_id).where(Contract.user_id == user.id, active).correlate(None)
    colleagues = (
        User.query.join(Contract).join(UserProfile)
        .filter(User.id != user.id, active, Contract.place_of_work_id.in_(workplaces),
                UserProfile.date_of_birth <= today,
                db.extract("month", UserProfile.date_of_birth) == today.month,
                db.extract("day", UserProfile.date_of_birth) == today.day)
        .options(joinedload(User.profile)).distinct().all()
    )
    return {"is_own_birthday": False,
            "birthday_colleagues": sorted(colleagues, key=lambda person: (display_name(person).casefold(), person.id))}
