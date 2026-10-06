"""One page policy for HTTP entry points, navigation and dashboard links.

Page grants never override record ownership or the leave approval workflow.
Only the developer access-matrix page is permanently tied to a base role.
"""

from datetime import datetime, timezone
from secrets import compare_digest, token_urlsafe
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, flash, g, has_request_context, redirect, render_template, request, session, url_for
from flask_babel import gettext as _, lazy_gettext
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from . import db
from .models import UserPrivilege
from .page_access_models import PageAccessSettings, PageRolePermission


page_access = Blueprint("page_access", __name__)
ALL_ROLES = ("employee", "hr", "ceo", "developer")
MANAGER_ROLES = ("hr", "ceo")
ROLE_LABELS = {
    "employee": lazy_gettext("Employee"),
    "hr": lazy_gettext("HR"),
    "ceo": lazy_gettext("Director"),
    "developer": lazy_gettext("Developer"),
}


def _page(key, label, section, roles=ALL_ROLES, note=None):
    return {"key": key, "endpoint": key, "label": label, "section": section, "default_roles": roles, "note": note}


PAGE_DEFINITIONS = (
    _page("dashboard", lazy_gettext("Dashboard"), lazy_gettext("My workspace")),
    _page("edit_profile", lazy_gettext("My profile"), lazy_gettext("My workspace")),
    _page("manage_dependents", lazy_gettext("Dependents"), lazy_gettext("My workspace")),
    _page("add_qualification", lazy_gettext("Qualifications"), lazy_gettext("My workspace")),
    _page("professional_exam", lazy_gettext("Professional exam"), lazy_gettext("My workspace")),
    _page("change_password", lazy_gettext("Change password"), lazy_gettext("My workspace")),
    _page("leaves", lazy_gettext("Leave calendar"), lazy_gettext("Records"), note=lazy_gettext("An employment contract is also required.")),
    _page("worktime.index", lazy_gettext("Working-time register"), lazy_gettext("Records"), note=lazy_gettext("An employment contract is also required.")),
    _page("manage_user_profiles", lazy_gettext("User profiles"), lazy_gettext("Management"), MANAGER_ROLES),
    _page("manage_contracts", lazy_gettext("Contracts"), lazy_gettext("Management"), MANAGER_ROLES),
    _page("worktime.groups", lazy_gettext("Groups"), lazy_gettext("Management"), MANAGER_ROLES),
    _page("worktime.management", lazy_gettext("Working-time management"), lazy_gettext("Management"), MANAGER_ROLES),
    _page("manage_working_days", lazy_gettext("Working days"), lazy_gettext("Management"), MANAGER_ROLES),
    _page("manage_leave_limits", lazy_gettext("Leave limits"), lazy_gettext("Management"), MANAGER_ROLES),
    _page("manage_leaves", lazy_gettext("Leave approvals"), lazy_gettext("Management"), note=lazy_gettext("Director privilege or an active nursery leadership appointment is also required. Approval rules and legal-entity scope still apply.")),
    _page("gyap.manage_forms", lazy_gettext("GYÁP forms"), lazy_gettext("Management"), MANAGER_ROLES),
    _page("leave_approval_settings", lazy_gettext("Approval settings"), lazy_gettext("Management"), ("ceo",)),
    _page("manage_leadership", lazy_gettext("Leadership"), lazy_gettext("Management"), MANAGER_ROLES),
    _page("manage_legal_entities", lazy_gettext("Legal entities"), lazy_gettext("Management"), MANAGER_ROLES),
    _page("manage_places_of_work", lazy_gettext("Workplaces"), lazy_gettext("Management"), MANAGER_ROLES),
    _page("manage_privileges", lazy_gettext("Privileges"), lazy_gettext("Settings"), ("hr", "ceo", "developer")),
    _page("page_access.settings", lazy_gettext("Page access"), lazy_gettext("Settings"), ("developer",), lazy_gettext("Developers always have access to this page. Access cannot be granted to other roles.")),
    _page("mail_settings.settings", lazy_gettext("Email settings"), lazy_gettext("Settings"), ("developer",)),
)
_PAGES = {row["key"]: row for row in PAGE_DEFINITIONS}
_ENDPOINT_PAGES = {key: key for key in _PAGES}
for _parent, _children in {
    "edit_profile": ("change_account_email", "profile_photos.upload_photo", "profile_photos.photo_editor", "profile_photos.photo_source"),
    "manage_user_profiles": ("edit_user_profile", "delete_user_profile"),
    "manage_dependents": ("add_dependent", "edit_dependent"),
    "add_qualification": ("edit_qualification",),
    "manage_contracts": ("select_contract_employee", "create_contract", "edit_contract"),
    "manage_legal_entities": ("create_legal_entity", "edit_legal_entity"),
    "manage_places_of_work": ("create_place_of_work", "edit_place_of_work"),
    "worktime.groups": ("worktime.group_new", "worktime.group_edit", "worktime.legacy_groups"),
    "worktime.management": ("worktime.generate", "worktime.confirm", "worktime.edit_entry", "worktime.merge"),
    "mail_settings.settings": ("mail_settings.initialise_key",),
}.items():
    _ENDPOINT_PAGES.update({endpoint: _parent for endpoint in _children})

# These pages do not reveal protected application data. They must remain usable
# when every configurable page is denied, so login/logout never form a loop.
UTILITY_ENDPOINTS = frozenset({
    "static", "index", "login", "logout", "register", "set_language", "page_access.unavailable",
    "password_reset.request_reset", "password_reset.reset_password",
})
SPECIAL_ENDPOINTS = frozenset({"profile_photos.show_photo", "gyap.download_form", "worktime.export"})


def invalidate_access_cache():
    """Discard only this Flask context's rules, never a process-wide cache."""
    g.pop("_page_access_rules", None)


def _permission_rules():
    # Workers may retain an app context across many jobs. Cache only within an
    # HTTP request; each worker check reads the committed matrix afresh.
    cached = g.get("_page_access_rules") if has_request_context() else None
    if cached is None:
        cached = {(row.page_key, row.role): row.allowed for row in PageRolePermission.query.populate_existing().all()}
        if has_request_context():
            g._page_access_rules = cached
    return cached


def page_for_endpoint(endpoint):
    return _ENDPOINT_PAGES.get(endpoint)


def _role(user):
    privilege = getattr(user, "privilege", None)
    return getattr(privilege, "value", privilege)


def can_access_page(page_key, user=None, *, day=None):
    user = current_user if user is None else user
    if not getattr(user, "is_authenticated", False) or page_key not in _PAGES:
        return False
    role = _role(user)
    if page_key == "page_access.settings":
        return role == "developer"
    if role not in ALL_ROLES:
        return False
    allowed = _permission_rules().get((page_key, role), role in _PAGES[page_key]["default_roles"])
    if not allowed:
        return False
    if page_key in {"leaves", "worktime.index"}:
        return bool(user.contracts)
    if page_key == "manage_leaves" and role != "ceo":
        today = day or datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Budapest")).date()
        return any(
            leadership.start_date <= today and (leadership.end_date is None or leadership.end_date >= today)
            for contract in user.contracts for leadership in contract.leadership_positions
        )
    return True


def can_access_endpoint(endpoint, user=None, **view_args):
    user = current_user if user is None else user
    if endpoint in UTILITY_ENDPOINTS:
        return True
    if not getattr(user, "is_authenticated", False):
        return False
    if endpoint == "profile_photos.show_photo":
        return view_args.get("user_id") == user.id or can_access_page("manage_user_profiles", user)
    if endpoint == "gyap.download_form":
        return any(can_access_page(key, user) for key in ("leaves", "manage_leaves", "gyap.manage_forms"))
    if endpoint == "worktime.export":
        return can_access_page("worktime.management", user) or (
            view_args.get("user_id") == user.id and can_access_page("worktime.index", user)
        )
    return can_access_page(page_for_endpoint(endpoint), user)


def has_access_to_any(endpoints, user=None):
    return any(can_access_endpoint(endpoint, user) for endpoint in endpoints)


def landing_url(user=None):
    user = current_user if user is None else user
    if not getattr(user, "is_authenticated", False):
        return url_for("login")
    for page in PAGE_DEFINITIONS:
        if can_access_page(page["key"], user):
            return url_for(page["endpoint"])
    return url_for("page_access.unavailable")


def initialise_page_access_settings():
    """Create only a revision row; absent permission rows preserve defaults."""
    if db.session.get(PageAccessSettings, 1) is None:
        db.session.add(PageAccessSettings(id=1))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            if db.session.get(PageAccessSettings, 1) is None:
                raise


def _settings():
    return PageAccessSettings.query.filter_by(id=1).populate_existing().one()


def _raw_matrix():
    rules = _permission_rules()
    return {
        (page["key"], role): role == "developer" if page["key"] == "page_access.settings" else rules.get((page["key"], role), role in page["default_roles"])
        for page in PAGE_DEFINITIONS for role in ALL_ROLES
    }


def _check_form():
    expected = session.get("page_access_csrf_token", "")
    supplied = request.form.get("csrf_token", "")
    if not expected or not compare_digest(expected.encode(), supplied.encode()):
        abort(400)
    if any(key not in {"csrf_token", "revision", "permissions"} for key in request.form):
        abort(400)
    if len(request.form.getlist("csrf_token")) != 1 or len(request.form.getlist("revision")) != 1:
        abort(400)
    selected = request.form.getlist("permissions")
    known = {f"{page['key']}:{role}" for page in PAGE_DEFINITIONS for role in ALL_ROLES}
    if len(selected) != len(set(selected)) or any(cell not in known for cell in selected):
        abort(400)
    # Ignore attempts to remove developer access. Explicit attempts to grant
    # this page to another role are invalid rather than silently misleading.
    if any(f"page_access.settings:{role}" in selected for role in ALL_ROLES if role != "developer"):
        abort(400)
    return set(selected)


@page_access.route("/page-access", methods=["GET", "POST"])
@login_required
def settings():
    if current_user.privilege != UserPrivilege.developer:
        abort(403)
    saved = _settings()
    if request.method == "POST":
        selected = _check_form()
        if not compare_digest(saved.revision.encode(), request.form.get("revision", "").encode()):
            flash(_("Page access was changed in another session. Review the current settings and try again."), "error")
            return redirect(url_for("page_access.settings"))
        from .notification_events import record_reviewer_changes, snapshot_pending_leave_tasks

        previous_tasks = snapshot_pending_leave_tasks()
        existing = {(row.page_key, row.role): row for row in PageRolePermission.query.all()}
        for page in PAGE_DEFINITIONS:
            for role in ALL_ROLES:
                allowed = f"{page['key']}:{role}" in selected
                if page["key"] == "page_access.settings":
                    allowed = role == "developer"
                row = existing.get((page["key"], role))
                if row is None:
                    row = PageRolePermission(page_key=page["key"], role=role)
                    db.session.add(row)
                row.allowed = allowed
        saved.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        saved.updated_by_id = current_user.id
        # Force a versioned singleton UPDATE even if the matrix itself did not
        # change, so concurrent saves cannot merge incompatible selections.
        saved.revision = uuid4().hex
        try:
            db.session.flush()
            invalidate_access_cache()
            record_reviewer_changes(previous_tasks, current_user)
            db.session.commit()
        except (StaleDataError, IntegrityError):
            db.session.rollback()
            invalidate_access_cache()
            flash(_("Page access was changed in another session. Review the current settings and try again."), "error")
            return redirect(url_for("page_access.settings"))
        invalidate_access_cache()
        from .notification_delivery import wake_notifications

        wake_notifications()
        flash(_("Page access saved."), "success")
        return redirect(url_for("page_access.settings"))
    session.setdefault("page_access_csrf_token", token_urlsafe(32))
    return render_template(
        "page_access.html", pages=PAGE_DEFINITIONS, roles=ALL_ROLES,
        role_labels=ROLE_LABELS, matrix=_raw_matrix(), settings=saved,
        csrf_token=session["page_access_csrf_token"],
    )


@page_access.get("/access-unavailable")
@login_required
def unavailable():
    return render_template("access_unavailable.html")


def init_page_access(app):
    app.register_blueprint(page_access)
    app.jinja_env.globals.update(
        can_access_page=can_access_page, can_access_endpoint=can_access_endpoint,
        has_access_to_any=has_access_to_any, landing_url=landing_url,
    )

    @app.before_request
    def enforce_page_access():
        invalidate_access_cache()
        # Unknown new application endpoints fail closed even if their author
        # omitted a login decorator. Preserve Flask's own 404/405 responses.
        if request.endpoint is None:
            return None
        if request.endpoint not in UTILITY_ENDPOINTS and request.endpoint not in SPECIAL_ENDPOINTS and page_for_endpoint(request.endpoint) is None:
            abort(403)
        # Known protected routes already provide the normal login redirect.
        if not current_user.is_authenticated:
            return None
        if not can_access_endpoint(request.endpoint, **(request.view_args or {})):
            abort(403)

    @app.after_request
    def private_page_access_response(response):
        if current_user.is_authenticated:
            # Cached pages must not reveal a revoked menu or dashboard after
            # a policy update. Downloads already follow the same private rule.
            response.cache_control.no_store = True
        return response
