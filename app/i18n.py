"""Translated display labels; database enum values remain unchanged."""

from flask_babel import lazy_gettext


LOGIN_MESSAGE = lazy_gettext("Please log in to access this page.")

ENUM_LABELS = {
    'employee': lazy_gettext('Employee'),
    'hr': lazy_gettext('HR'),
    'ceo': lazy_gettext('CEO'),
    'developer': lazy_gettext('Developer'),
    'male': lazy_gettext('Male'),
    'female': lazy_gettext('Female'),
    'other': lazy_gettext('Other'),
    'Teacher': lazy_gettext('Teacher'),
    'Teaching Assistant': lazy_gettext('Teaching Assistant'),
    'Nursery Assistant': lazy_gettext('Nursery Assistant'),
    'Secretary': lazy_gettext('Secretary'),
    'Employee under the Labour Code': lazy_gettext('Employee under the Labour Code'),
    'basic leave': lazy_gettext('Basic leave'),
    'supplementary leave based on age': lazy_gettext('Supplementary leave based on age'),
    'supplementary leave for children': lazy_gettext('Supplementary leave for children'),
    'supplementary leave for children with disability': lazy_gettext('Supplementary leave for children with disability'),
    'supplementary leave for young employees': lazy_gettext('Supplementary leave for young employees'),
    'supplementary leave for employees with reduced working capacity / eligible for disability benefits': lazy_gettext('Supplementary leave for employees with reduced working capacity / eligible for disability benefits'),
    'sick leave': lazy_gettext('Sick leave'),
    'leave carried over from previous year': lazy_gettext('Leave carried over from previous year'),
    'maternity leave': lazy_gettext('Maternity leave'),
    'paternity leave': lazy_gettext('Paternity leave'),
    'parental leave': lazy_gettext('Parental leave'),
    'childcare fee': lazy_gettext('Childcare fee'),
    'childcare allowance': lazy_gettext('Childcare allowance'),
    'supplementary leave for the birth of a grandchild': lazy_gettext('Supplementary leave for the birth of a grandchild'),
    'supplementary leave for first marriage': lazy_gettext('Supplementary leave for first marriage'),
    'exemption from obligation to work': lazy_gettext('Exemption from obligation to work'),
    'paid leave': lazy_gettext('Paid leave'),
    'health leave': lazy_gettext('Health leave'),
    'childcare sickness benefit': lazy_gettext('Childcare sickness benefit'),
    'childbirth leave': lazy_gettext('Childbirth leave'),
    'unpaid leave': lazy_gettext('Unpaid leave'),
    'pending approval': lazy_gettext('Pending approval'),
    'approved': lazy_gettext('Approved'),
    'rejected': lazy_gettext('Rejected'),
    'pending cancellation': lazy_gettext('Pending cancellation'),
    'cancelled': lazy_gettext('Cancelled'),
    'single': lazy_gettext('Single'),
    'married': lazy_gettext('Married'),
    'divorced': lazy_gettext('Divorced'),
    'widowed': lazy_gettext('Widowed'),
    'civil partnership': lazy_gettext('Civil partnership'),
    'child': lazy_gettext('Child'),
    'other dependent': lazy_gettext('Other dependent'),
    'Trainee': lazy_gettext('Trainee'),
    'Teacher I': lazy_gettext('Teacher I'),
    'Teacher II': lazy_gettext('Teacher II'),
    'Master Teacher': lazy_gettext('Master Teacher'),
    'Research Teacher': lazy_gettext('Research Teacher'),
    'principal': lazy_gettext('Principal'),
    'deputy principal': lazy_gettext('Deputy principal'),
    'sickness benefit': lazy_gettext('Sickness benefit'),
}


def enum_label(value) -> str:
    raw = getattr(value, "value", value)
    return str(ENUM_LABELS.get(raw, raw))
