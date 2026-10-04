"""Current account details shared by the navigation and profile editor."""

from flask_login import current_user

from .i18n import enum_label
from .people import local_today


def account_display(user, day=None):
    """Prefer the newest contract active on the current Budapest calendar day."""
    day = day or local_today()
    full_name = (user.profile.full_name or "").strip() if user.profile else ""
    name = full_name or user.username
    active_contract = max(
        (
            contract for contract in user.contracts
            if contract.start_date <= day
            and (contract.end_date is None or contract.end_date >= day)
        ),
        key=lambda contract: (contract.start_date, contract.id or 0),
        default=None,
    )
    job_title = (active_contract.job_title or "").strip() if active_contract else ""
    return {
        "name": name,
        "initial": name[:1].upper(),
        "job_title": job_title or enum_label(user.privilege),
    }


def account_template_context():
    return {
        "current_account": account_display(current_user) if current_user.is_authenticated else None,
    }
