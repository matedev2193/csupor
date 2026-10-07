"""Stable classification codes based on the supplied KSH recording tables.

Study participation and awards are independent, overlapping classifications.
They describe the record itself; reporting periods are calculated from dates.
"""

from flask_babel import lazy_gettext as _

KINDS = {
    "qualification": _("Qualification"),
    "exam": _("Professional exam"),
    "teacher_training": _("Teacher continuing professional development"),
    "other_course": _("Other course"),
    "teacher_assessment": _("Teacher assessment"),
}
COMPLETION_STATES = {
    "completed": _("Completed"),
    "in_progress": _("In progress"),
    "discontinued": _("Discontinued"),
}
STUDY_CATEGORIES = {
    "level_raising": _("Basic or supplementary studies raising the qualification level"),
    "additional_same_level": _("Second or further qualification at the same level"),
    "university_specialist": _("University-level specialist postgraduate studies"),
    "college_specialist": _("College-level specialist postgraduate studies"),
    "vocational": _("Studies towards a trade or vocational qualification"),
    "exam_preparation": _("Preparation for a professional exam"),
    "bachelor": _("Bachelor's studies"),
    "master": _("Master's studies"),
    "undivided": _("Undivided degree studies"),
}
AWARD_CATEGORIES = {
    "doctorate": _("Doctoral degree"),
    "first_university": _("First university qualification"),
    "second_degree": _("Second college or university qualification"),
    "college_specialist": _("College-level specialist postgraduate qualification"),
    "university_specialist": _("University-level specialist postgraduate qualification"),
    "bachelor": _("Bachelor's degree"),
    "master": _("Master's degree"),
    "undivided": _("Undivided degree"),
    "professional_exam": _("Professional exam completed"),
    "it": _("IT qualification"),
    "ecdl": _("ECDL qualification"),
    "vocational": _("Trade or vocational qualification"),
    "language_b2": _("Language examination at B2 level or above"),
    "leadership": _("Leadership qualification"),
    "leadership_university": _("University-level leadership qualification"),
    "leadership_college": _("College-level leadership qualification"),
    "public_education_leadership": _("Public education leadership training completed"),
}
TRAINING_TOPICS = {
    "subject_theory": _("Subject theory"),
    "methodology": _("Methodology"),
    "digital_culture": _("IT and digital culture"),
    "other": _("Other topic"),
}
ORGANISER_TYPES = {
    "higher_education": _("Higher education institution"),
    "pedagogical_service": _("Pedagogical professional service institution"),
    "international_agreement": _("Programme under an international agreement"),
    "education_centre": _("Pedagogical Education Centre"),
    "other_licensed": _("Other licensed provider"),
}
FUNDING_TYPES = {
    "self": _("Self-funded"),
    "full": _("Fully supported by a cost contribution"),
    "partial": _("Partly supported by a cost contribution"),
}
ATTENDANCE_MODES = {
    "in_person": _("In person"),
    "e_learning": _("E-learning"),
    "blended": _("Blended learning"),
}


def taxonomy_context():
    return {
        "kinds": KINDS, "completion_states": COMPLETION_STATES,
        "study_categories": STUDY_CATEGORIES, "award_categories": AWARD_CATEGORIES,
        "training_topics": TRAINING_TOPICS, "organiser_types": ORGANISER_TYPES,
        "funding_types": FUNDING_TYPES, "attendance_modes": ATTENDANCE_MODES,
    }
