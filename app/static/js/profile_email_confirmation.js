(() => {
  'use strict';

  const form = document.getElementById('profile-form');
  const email = document.getElementById('account-email');
  const dialog = document.getElementById('profile-email-confirmation');
  const password = document.getElementById('account-email-password');
  if (!form || !email || !dialog || !password) return;

  const normalise = value => value.trim().toLowerCase();
  const hasChanged = () => normalise(email.value) !== normalise(email.dataset.originalEmail);
  const confirm = dialog.querySelector('[data-email-confirm]');

  function openConfirmation() {
    password.disabled = false;
    // A server-rendered confirmation also works without JavaScript. Promote it
    // to a modal when JavaScript is available, for focus trapping and Escape.
    if (dialog.open) dialog.close();
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    password.focus();
  }

  function clearConfirmation() {
    password.value = '';
    password.disabled = true;
  }

  dialog.addEventListener('close', () => {
    // close() above schedules this event; do not disable a reopened modal.
    if (!dialog.open) clearConfirmation();
  });
  dialog.addEventListener('cancel', clearConfirmation);
  dialog.querySelector('[data-email-cancel]').addEventListener('click', () => {
    dialog.close();
    clearConfirmation();
  });

  form.addEventListener('submit', event => {
    if (!hasChanged()) {
      clearConfirmation();
      return;
    }
    if (dialog.open && event.submitter === confirm) return;
    event.preventDefault();
    openConfirmation();
  });

  // Enter in the password field should confirm this same profile submission.
  password.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      form.requestSubmit(confirm);
    }
  });

  window.addEventListener('pageshow', event => {
    if (event.persisted) {
      if (dialog.open) dialog.close();
      clearConfirmation();
    }
  });
  if (dialog.hasAttribute('data-confirmation-required')) openConfirmation();
  else clearConfirmation();
})();
