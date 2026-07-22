from __future__ import annotations

import enum
from datetime import date

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, login_manager


def _enum_values(enum_type: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_type]


class UserPrivilege(enum.Enum):
    employee = "employee"
    hr = "hr"
    ceo = "ceo"
    developer = "developer"


class Gender(enum.Enum):
    male = "male"
    female = "female"
    other = "other"


class ContractType(enum.Enum):
    teacher = "Teacher"
    teaching_assistant = "Teaching Assistant"
    nursery_assistant = "Nursery Assistant"
    secretary = "Secretary"
    employee_under_the_labour_code = "Employee under the Labour Code"


class LeaveType(enum.Enum):
    basic_leave = "basic leave"
    supplementary_leave_based_on_age = "supplementary leave based on age"
    supplementary_leave_for_children = "supplementary leave for children"
    supplementary_leave_for_children_with_disability = "supplementary leave for children with disability"
    supplementary_leave_for_young_employees = "supplementary leave for young employees"
    supplementary_leave_for_reduced_working_capacity = (
        "supplementary leave for employees with reduced working capacity / eligible for disability benefits"
    )
    sick_leave = "sick leave"
    leave_carried_over_from_previous_year = "leave carried over from previous year"
    maternity_leave = "maternity leave"
    paternity_leave = "paternity leave"
    parental_leave = "parental leave"
    childcare_fee = "childcare fee"
    childcare_allowance = "childcare allowance"
    supplementary_leave_for_birth_of_grandchild = "supplementary leave for the birth of a grandchild"
    supplementary_leave_for_first_marriage = "supplementary leave for first marriage"
    exemption_from_obligation_to_work = "exemption from obligation to work"


class LeaveRequestCategory(enum.Enum):
    paid_leave = "paid leave"
    health_leave = "health leave"
    childcare_sickness_benefit = "childcare sickness benefit"
    childbirth_leave = "childbirth leave"
    exemption_from_obligation_to_work = "exemption from obligation to work"
    unpaid_leave = "unpaid leave"


class LeaveRequestStatus(enum.Enum):
    pending_approval = "pending approval"
    approved = "approved"
    rejected = "rejected"
    pending_cancellation = "pending cancellation"
    cancelled = "cancelled"


class MaritalStatus(enum.Enum):
    single = "single"
    married = "married"
    divorced = "divorced"
    widowed = "widowed"
    civil_partnership = "civil partnership"


class DependentType(enum.Enum):
    child = "child"
    other_dependent = "other dependent"


class TeacherClassification(enum.Enum):
    trainee = "Trainee"
    teacher_i = "Teacher I"
    teacher_ii = "Teacher II"
    master_teacher = "Master Teacher"
    research_teacher = "Research Teacher"


class LeadershipPosition(enum.Enum):
    principal = "principal"
    deputy_principal = "deputy principal"


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    username = db.Column(db.String(50), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    privilege = db.Column(
        db.Enum(UserPrivilege, values_callable=_enum_values),
        nullable=False,
        default=UserPrivilege.employee,
    )

    profile = db.relationship("UserProfile", back_populates="user", uselist=False, cascade="all, delete-orphan")
    dependents = db.relationship("Dependent", back_populates="user", cascade="all, delete-orphan")
    qualifications = db.relationship(
        "EducationalQualification", back_populates="user", cascade="all, delete-orphan"
    )
    professional_exam = db.relationship(
        "ProfessionalExam", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    contracts = db.relationship("Contract", back_populates="user", cascade="all, delete-orphan")
    leave_requests = db.relationship(
        "LeaveRequest",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="LeaveRequest.user_id",
    )

    def set_password(self, raw_password: str) -> None:
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return check_password_hash(self.password_hash, raw_password)


class UserProfile(db.Model):
    __tablename__ = "user_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)

    full_name = db.Column(db.String(120), nullable=True)
    name_at_birth = db.Column(db.String(120), nullable=True)
    date_of_birth = db.Column(db.Date, nullable=True)
    place_of_birth = db.Column(db.String(120), nullable=True)
    gender = db.Column(db.Enum(Gender, values_callable=_enum_values), nullable=True)
    mothers_maiden_name = db.Column(db.String(120), nullable=True)
    citizenships = db.Column(db.String(255), nullable=True)
    social_security_number = db.Column(db.String(9), nullable=True)
    tax_number = db.Column(db.String(10), nullable=True)
    education_number = db.Column(db.String(11), nullable=True)
    teacher_id_card_number = db.Column(db.String(64), nullable=True)
    permanent_residence = db.Column(db.String(255), nullable=True)
    temporary_address = db.Column(db.String(255), nullable=True)
    phone_number = db.Column(db.String(40), nullable=True)
    bank_account_number = db.Column(db.String(64), nullable=True)
    marital_status = db.Column(db.Enum(MaritalStatus, values_callable=_enum_values), nullable=True)
    disability = db.Column(db.String(255), nullable=True)

    user = db.relationship("User", back_populates="profile")


class Dependent(db.Model):
    __tablename__ = "dependents"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    name = db.Column(db.String(120), nullable=False)
    dependent_type = db.Column(
        db.Enum(DependentType, values_callable=_enum_values),
        nullable=False,
        default=DependentType.child,
    )
    date_of_birth = db.Column(db.Date, nullable=False)
    social_security_number = db.Column(db.String(9), nullable=False)
    dependency_start = db.Column(db.Date, nullable=False)
    disability = db.Column(db.String(255), nullable=True)

    user = db.relationship("User", back_populates="dependents")


class EducationalQualification(db.Model):
    __tablename__ = "educational_qualifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    level_or_type = db.Column(db.String(120), nullable=False)
    qualification_name = db.Column(db.String(120), nullable=False)
    institution_name = db.Column(db.String(120), nullable=False)
    degree_number = db.Column(db.String(80), nullable=False)
    year_obtained = db.Column(db.Integer, nullable=False)
    highest = db.Column(db.Boolean, nullable=False, default=False)

    user = db.relationship("User", back_populates="qualifications")


class ProfessionalExam(db.Model):
    __tablename__ = "professional_exams"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)

    qualification_name = db.Column(db.String(120), nullable=False)
    year_obtained = db.Column(db.Integer, nullable=False)
    degree_number = db.Column(db.String(80), nullable=False)

    user = db.relationship("User", back_populates="professional_exam")


class LegalEntity(db.Model):
    __tablename__ = "legal_entities"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    address = db.Column(db.String(255), nullable=False)
    om_id = db.Column(db.String(6), nullable=False)
    tax_number = db.Column(db.String(11), nullable=False)

    places_of_work = db.relationship("PlaceOfWork", back_populates="legal_entity", cascade="all, delete-orphan")
    contracts = db.relationship("Contract", back_populates="employer")
    leadership_positions = db.relationship("Leadership", back_populates="legal_entity")


class PlaceOfWork(db.Model):
    __tablename__ = "places_of_work"

    id = db.Column(db.Integer, primary_key=True)
    legal_entity_id = db.Column(db.Integer, db.ForeignKey("legal_entities.id"), nullable=False)
    address = db.Column(db.String(255), nullable=False)

    legal_entity = db.relationship("LegalEntity", back_populates="places_of_work")
    contracts = db.relationship("Contract", back_populates="place_of_work")


class Contract(db.Model):
    __tablename__ = "contracts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    contract_type = db.Column(db.Enum(ContractType, values_callable=_enum_values), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=True)
    certificate_of_good_conduct_number = db.Column(db.String(64), nullable=True)
    certificate_of_good_conduct_date = db.Column(db.Date, nullable=True)
    job_title = db.Column(db.String(120), nullable=False)
    working_hours_per_week = db.Column(db.Integer, nullable=False)
    teacher_classification = db.Column(
        db.Enum(TeacherClassification, values_callable=_enum_values),
        nullable=True,
    )
    classification_start_date = db.Column(db.Date, nullable=True)
    legal_entity_id = db.Column(db.Integer, db.ForeignKey("legal_entities.id"), nullable=False)
    place_of_work_id = db.Column(db.Integer, db.ForeignKey("places_of_work.id"), nullable=False)

    user = db.relationship("User", back_populates="contracts")
    employer = db.relationship("LegalEntity", back_populates="contracts")
    place_of_work = db.relationship("PlaceOfWork", back_populates="contracts")
    leadership_positions = db.relationship("Leadership", back_populates="contract", cascade="all, delete-orphan")
    leave_limits = db.relationship("ContractLeaveLimit", back_populates="contract", cascade="all, delete-orphan")
    leave_requests = db.relationship("LeaveRequest", back_populates="contract", cascade="all, delete-orphan")


class LeaveYear(db.Model):
    __tablename__ = "leave_years"

    year = db.Column(db.Integer, primary_key=True)
    is_open = db.Column(db.Boolean, nullable=False, default=False)
    imported_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    imported_at = db.Column(db.DateTime, nullable=True)

    imported_by = db.relationship("User")


class ContractLeaveLimit(db.Model):
    __tablename__ = "contract_leave_limits"
    id = db.Column(db.Integer, primary_key=True)
    contract_id = db.Column(db.Integer, db.ForeignKey("contracts.id"), nullable=False)
    calendar_year = db.Column(db.Integer, nullable=False)
    leave_type = db.Column(db.Enum(LeaveType, values_callable=_enum_values), nullable=False)
    limit_days = db.Column(db.Integer, nullable=False)
    period_start = db.Column(db.Date, nullable=True)
    period_end = db.Column(db.Date, nullable=True)
    imported = db.Column(db.Boolean, nullable=False, default=False)
    previous_limit_days = db.Column(db.Integer, nullable=True)
    previous_period_start = db.Column(db.Date, nullable=True)
    previous_period_end = db.Column(db.Date, nullable=True)
    previous_imported = db.Column(db.Boolean, nullable=True)

    contract = db.relationship("Contract", back_populates="leave_limits")


class LeaveRequest(db.Model):
    __tablename__ = "leave_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    contract_id = db.Column(db.Integer, db.ForeignKey("contracts.id"), nullable=False)
    category = db.Column(db.Enum(LeaveRequestCategory, values_callable=_enum_values), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=True)
    status = db.Column(
        db.Enum(LeaveRequestStatus, values_callable=_enum_values),
        nullable=False,
        default=LeaveRequestStatus.pending_approval,
    )
    note = db.Column(db.Text, nullable=True)
    ceo_approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    leadership_approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    decided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    user = db.relationship("User", back_populates="leave_requests", foreign_keys=[user_id])
    contract = db.relationship("Contract", back_populates="leave_requests")
    ceo_approver = db.relationship("User", foreign_keys=[ceo_approved_by_id])
    leadership_approver = db.relationship("User", foreign_keys=[leadership_approved_by_id])
    decided_by = db.relationship("User", foreign_keys=[decided_by_id])


class WorkingDayOverride(db.Model):
    __tablename__ = "working_day_overrides"

    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.Date, unique=True, nullable=False)
    is_working_day = db.Column(db.Boolean, nullable=False)
    note = db.Column(db.String(255), nullable=True)


class Leadership(db.Model):
    __tablename__ = "leadership"

    id = db.Column(db.Integer, primary_key=True)
    legal_entity_id = db.Column(db.Integer, db.ForeignKey("legal_entities.id"), nullable=False)
    contract_id = db.Column(db.Integer, db.ForeignKey("contracts.id"), nullable=False)
    position = db.Column(
        db.Enum(LeadershipPosition, values_callable=_enum_values),
        nullable=False,
        default=LeadershipPosition.principal,
    )
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=True)

    legal_entity = db.relationship("LegalEntity", back_populates="leadership_positions")
    contract = db.relationship("Contract", back_populates="leadership_positions")


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)
