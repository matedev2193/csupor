(() => {
  const dialog = document.getElementById('delete-user-dialog');
  const form = document.getElementById('delete-user-form');
  const password = document.getElementById('delete-manager-password');
  if (!dialog || !form || !password) return;
  let trigger = null;

  document.querySelectorAll('[data-delete-user]').forEach((button) => {
    button.addEventListener('click', () => {
      trigger = button;
      form.action = button.dataset.deleteUrl;
      dialog.querySelector('[data-delete-user-name]').textContent = button.dataset.userName;
      password.value = '';
      form.querySelector('[type="submit"]').disabled = false;
      dialog.showModal();
      password.focus();
    });
  });
  dialog.querySelector('[data-close-delete-dialog]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    password.value = '';
    form.removeAttribute('action');
    if (trigger) trigger.focus();
  });
  form.addEventListener('submit', () => {
    form.querySelector('[type="submit"]').disabled = true;
  });
})();
