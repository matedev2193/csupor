"""Read-only explanations of leave rules, checked against NJT on 2026-09-28.

The editable allowance is kept separate from the statutory annual entitlement.
Stored values are never changed by rendering these explanations.
"""
from calendar import monthrange
from datetime import date

from flask_babel import _, format_date

from .models import ContractType, DependentType, LeaveType

PUE_URL = "https://njt.jog.gov.hu/jogszabaly/2023-52-00-00"
MT_URL = "https://njt.jog.gov.hu/jogszabaly/2012-1-00-00"


def age_supplement_days(age):
    if age is None:
        return 0
    thresholds = [(45, 10), (43, 9), (41, 8), (39, 7), (37, 6),
                  (35, 5), (33, 4), (31, 3), (28, 2), (25, 1)]
    return next((days for threshold, days in thresholds if age >= threshold), 0)


def eligible_children(user, year):
    return [child for child in user.dependents
            if child.dependent_type == DependentType.child and child.date_of_birth
            and 0 <= year - child.date_of_birth.year <= 16
            and child.dependency_start <= date(year, 12, 31)]


def paternity_deadline(birthday):
    month_index = birthday.year * 12 + birthday.month - 1 + 4
    year, month = divmod(month_index, 12)
    return date(year, month + 1, monthrange(year, month + 1)[1])


def leave_basis(contract, year, leave_type):
    status_law = contract.contract_type != ContractType.employee_under_the_labour_code
    law, url = ("Púétv.", PUE_URL) if status_law else ("Mt.", MT_URL)
    references = {
        LeaveType.basic_leave: ("90. § (3)", "116. §"),
        LeaveType.supplementary_leave_based_on_age: ("90. §", "117. § (1)–(2)"),
        LeaveType.supplementary_leave_for_children: ("90. § (11), (13)", "118. § (1), (3)"),
        LeaveType.supplementary_leave_for_children_with_disability: ("90. § (12)–(13)", "118. § (2)–(3)"),
        LeaveType.supplementary_leave_for_young_employees: ("90. §", "119. § (1)"),
        LeaveType.supplementary_leave_for_reduced_working_capacity: ("90. § (8)", "120. §"),
        LeaveType.sick_leave: ("92. § (1)–(3)", "126. § (1)–(3)"),
        LeaveType.leave_carried_over_from_previous_year: ("91. § (4), (6)", "123. §"),
        LeaveType.maternity_leave: ("93. § (1)–(3)", "127. § (1)–(3)"),
        LeaveType.paternity_leave: ("90. § (5), 91. § (5) a)", "118. § (4)"),
        LeaveType.parental_leave: ("90. § (4), 91. § (5) b)", "118/A. §"),
        LeaveType.childcare_fee: ("94. § (1), (4)", "128. § (1), (3)"),
        LeaveType.childcare_allowance: ("94. § (2)", "130. §"),
        LeaveType.supplementary_leave_for_birth_of_grandchild: ("90. § (6)", None),
        LeaveType.supplementary_leave_for_first_marriage: ("90. § (7)", None),
        LeaveType.exemption_from_obligation_to_work: ("71. §", "55. §"),
    }
    section = references[leave_type][0 if status_law else 1]
    if section is None:
        return {"reference": "", "url": "", "paragraphs": [_("This category is not a statutory allowance under the Labour Code.")]}
    reference = f"{law} {section}"
    paragraphs = []
    result = {"reference": reference, "url": url, "paragraphs": paragraphs}
    if year < 2025:
        paragraphs.append(_("For years before 2025, check the legislation applicable to that year before setting this allowance."))
        return result
    user, profile = contract.user, contract.user.profile
    children = eligible_children(user, year)
    age = year - profile.date_of_birth.year if profile and profile.date_of_birth else None
    if leave_type == LeaveType.basic_leave:
        if status_law:
            paragraphs.append(_("Under %(reference)s, annual basic leave is 50 working days; the employer may use up to 15 for the purposes specified there. The portal records the available allowance separately.", reference=reference))
        else:
            paragraphs.append(_("Under %(reference)s, the employee has 20 working days of basic leave per year.", reference=reference))
    elif leave_type == LeaveType.supplementary_leave_based_on_age:
        if status_law:
            paragraphs.append(_("This employment regime does not provide a separate age-based supplementary allowance."))
        elif age is None:
            paragraphs.append(_("Add the employee's date of birth to calculate the age-based entitlement under %(reference)s.", reference=reference))
        else:
            paragraphs.append(_("Under %(reference)s, the employee turns %(age)s in %(year)s and is entitled to %(days)s working days of age-based supplementary leave per year.", reference=reference, age=age, year=year, days=age_supplement_days(age)))
    elif leave_type == LeaveType.supplementary_leave_for_children:
        count = len(children)
        days = 7 if count >= 3 else 4 if count == 2 else 2 if count == 1 else 0
        paragraphs.append(_("Under %(reference)s, the employee's %(count)s eligible children give an annual entitlement of %(days)s working days of supplementary leave. Children count from their birth year through the year they turn 16.", reference=reference, count=count, days=days))
    elif leave_type == LeaveType.supplementary_leave_for_children_with_disability:
        count = sum(bool(child.disability and child.disability.strip()) for child in children)
        paragraphs.append(_("Under %(reference)s, each qualifying child adds 2 working days per year. Health information is recorded for %(count)s eligible children; confirm the statutory disability condition before assigning the additional days.", reference=reference, count=count))
    elif leave_type == LeaveType.supplementary_leave_for_young_employees:
        if status_law:
            paragraphs.append(_("This employment regime does not provide a separate young-employee supplementary allowance."))
        else:
            paragraphs.append(_("Under %(reference)s, young employees receive 5 working days per year, including the year they turn 18.", reference=reference))
    elif leave_type == LeaveType.supplementary_leave_for_reduced_working_capacity:
        paragraphs.append(_("Under %(reference)s, qualifying reduced working capacity, disability support or a personal allowance for blind people gives 5 working days per year. A free-text illness note alone does not establish eligibility.", reference=reference))
    elif leave_type == LeaveType.sick_leave:
        paragraphs.append(_("Under %(reference)s, annual sick leave is 15 working days for qualifying incapacity due to illness. Occupational accidents, occupational diseases and high-risk pregnancy are excluded; a mid-year start requires proration.", reference=reference))
    elif leave_type == LeaveType.leave_carried_over_from_previous_year:
        paragraphs.append(_("Under %(reference)s, leave is normally granted in its due year. Carry-over needs a permitted exception and the corresponding deadline; the previous balance alone does not establish entitlement.", reference=reference))
    elif leave_type == LeaveType.paternity_leave:
        relevant = [child for child in user.dependents if child.dependent_type == DependentType.child
                    and child.date_of_birth and child.date_of_birth <= date(year, 12, 31)
                    and paternity_deadline(child.date_of_birth) >= date(year, 1, 1)]
        if relevant:
            for child in sorted(relevant, key=lambda child: (child.date_of_birth, child.id or 0)):
                paragraphs.append(_("Under %(reference)s, the child born on %(birth)s gives entitlement to 10 working days of paternity leave, usable by %(deadline)s, the end of the fourth month following birth.", reference=reference, birth=format_date(child.date_of_birth, "long"), deadline=format_date(paternity_deadline(child.date_of_birth), "long")))
        else:
            paragraphs.append(_("Under %(reference)s, paternity leave is 10 working days, usable by the end of the fourth month after birth or the final adoption decision. No recorded birth has a window overlapping this year; check the supporting record.", reference=reference))
    elif leave_type == LeaveType.parental_leave:
        paragraphs.append(_("Under %(reference)s, parental leave totals 44 working days until the child turns three, provided the employment relationship has lasted at least one year.", reference=reference))
    elif leave_type == LeaveType.maternity_leave:
        paragraphs.append(_("Under %(reference)s, maternity leave lasts 24 consecutive weeks, including at least two compulsory weeks. This is a period in weeks, not a 24-working-day allowance.", reference=reference))
    elif leave_type == LeaveType.childcare_fee:
        paragraphs.append(_("Under %(reference)s, childcare can justify unpaid leave, including during the specified childcare-benefit entitlement. Set the period from the supporting entitlement decision, not from a fixed annual day allowance.", reference=reference))
    elif leave_type == LeaveType.childcare_allowance:
        paragraphs.append(_("Under %(reference)s, unpaid leave for personal childcare can continue during childcare-allowance payments until the child turns ten.", reference=reference))
    elif leave_type == LeaveType.supplementary_leave_for_birth_of_grandchild:
        paragraphs.append(_("Under %(reference)s, a grandparent may take 5 working days by the end of the second month after the grandchild's birth. The birth date is needed to establish the exact deadline.", reference=reference))
    elif leave_type == LeaveType.supplementary_leave_for_first_marriage:
        paragraphs.append(_("Under %(reference)s, a first marriage gives 5 working days by the end of the second month after the wedding. The wedding date is needed to establish the exact deadline.", reference=reference))
    elif leave_type == LeaveType.exemption_from_obligation_to_work:
        paragraphs.append(_("Under %(reference)s, the reason for exemption determines its duration and conditions. Record the specific reason and supporting dates; there is no single annual allowance for all exemptions.", reference=reference))
    annual = {LeaveType.basic_leave, LeaveType.supplementary_leave_based_on_age,
              LeaveType.supplementary_leave_for_children, LeaveType.supplementary_leave_for_children_with_disability,
              LeaveType.supplementary_leave_for_young_employees, LeaveType.supplementary_leave_for_reduced_working_capacity}
    if leave_type in annual and (contract.start_date > date(year, 1, 1) or contract.end_date and contract.end_date < date(year, 12, 31)):
        prorating_reference = "Púétv. 90. § (9)–(10)" if status_law else "Mt. 121. § (1)–(2)"
        paragraphs.append(_("This contract covers only part of %(year)s; the annual allowance is subject to proration under %(reference)s.", year=year, reference=prorating_reference))
    return result
