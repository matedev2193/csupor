"""Describe the existing approval permissions using distinct people's names."""
from collections import Counter

from flask_babel import _
from sqlalchemy.orm import joinedload

from . import db
from .models import Contract, Leadership, LeadershipPosition, LeaveApprovalPolicy, User, UserPrivilege
from .people import display_name, local_today
from .page_access import can_access_page


def approval_description(contract, applicant, policy, today=None):
    if contract is None:
        return _("Select an active contract to see who needs to approve your leave.")
    today = today or local_today()
    directors = User.query.filter_by(privilege=UserPrivilege.ceo).options(joinedload(User.profile)).all()
    records = Leadership.query.filter(
        Leadership.legal_entity_id == contract.legal_entity_id,
        Leadership.start_date <= today,
        db.or_(Leadership.end_date.is_(None), Leadership.end_date >= today),
    ).options(joinedload(Leadership.contract).joinedload(Contract.user).joinedload(User.profile)).all()
    # Match the approval endpoint: a deputy cannot approve their own application.
    leaders = {
        record.contract.user.id: record.contract.user for record in records
        if (record.contract.user.id != applicant.id or record.position == LeadershipPosition.principal)
        and can_access_page("manage_leaves", record.contract.user, day=today)
    }
    directors = {person.id: person for person in directors if can_access_page("manage_leaves", person, day=today)}
    people = directors | leaders
    name_counts = Counter(display_name(person) for person in people.values())

    def names(ids):
        ordered = sorted((people[identifier] for identifier in ids), key=lambda person: (display_name(person).casefold(), person.id))
        return " / ".join(
            f"{display_name(person)} ({person.username})" if name_counts[display_name(person)] > 1 else display_name(person)
            for person in ordered
        )

    def one_of(ids):
        if len(ids) == 1:
            return _("Approval from %(names)s is required.", names=names(ids))
        return _("Approval from any one of %(names)s is sufficient.", names=names(ids))

    director_ids, leader_ids = set(directors), set(leaders)
    if policy == LeaveApprovalPolicy.ceo_only:
        return one_of(director_ids) if director_ids else _("No director is assigned. Contact HR to arrange approval.")
    if policy == LeaveApprovalPolicy.leadership_only:
        return one_of(leader_ids) if leader_ids else _("No eligible nursery head or deputy is assigned. Contact HR to arrange approval.")
    if policy == LeaveApprovalPolicy.either:
        ids = director_ids | leader_ids
        return one_of(ids) if ids else _("No eligible approver is assigned. Contact HR to arrange approval.")
    if not director_ids:
        return _("No director is assigned. Contact HR to arrange approval.")
    if not leader_ids:
        return _("No eligible nursery head or deputy is assigned. Contact HR to arrange approval.")
    shared = director_ids & leader_ids
    if shared:
        # One dual-role decision already satisfies both parts. Do not list the
        # same person twice or incorrectly require their deputy as well.
        director_only, leader_only = director_ids - shared, leader_ids - shared
        if not director_only or not leader_only:
            return one_of(shared)
        return _("Approval from %(shared)s alone, or one approval from %(directors)s and one from %(leaders)s, is required.",
                 shared=names(shared), directors=names(director_only), leaders=names(leader_only))
    return _("Both %(directors)s and %(leaders)s must approve.",
             directors=names(director_ids), leaders=names(leader_ids))
