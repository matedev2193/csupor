(() => {
  'use strict';

  const form = document.getElementById('qualification-editor-form');
  if (!form) return;
  const kind = form.querySelector('#kind');
  if (!kind) return;

  const assessmentOnly = form.querySelectorAll('[data-assessment-only]');
  const qualificationSections = Array.from(
    form.querySelectorAll('[data-assessment-inapplicable]'),
    section => ({
      section,
      controls: Array.from(section.querySelectorAll('input, select, textarea'),
        control => ({ control, originallyDisabled: control.disabled })),
    }),
  );

  function updateRecordType() {
    const isAssessment = kind.value === 'teacher_assessment';
    assessmentOnly.forEach(section => { section.hidden = !isAssessment; });
    qualificationSections.forEach(({ section, controls }) => {
      section.hidden = isAssessment;
      controls.forEach(({ control, originallyDisabled }) => {
        // Keep unsaved values when switching types, but exclude inapplicable
        // details from submission and native form validation.
        control.disabled = isAssessment || originallyDisabled;
      });
    });
  }

  kind.addEventListener('change', updateRecordType);
  window.addEventListener('pageshow', updateRecordType);
  form.addEventListener('reset', () => { queueMicrotask(updateRecordType); });
  updateRecordType();
})();
