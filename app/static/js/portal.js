/* Progressive enhancements; forms and navigation also work without JavaScript. */
(() => {
  const sidebar = document.querySelector('.sidebar');
  const toggle = document.querySelector('.menu-toggle');
  const backdrop = document.querySelector('.menu-backdrop');
  const main = document.querySelector('.main-content');
  const topbar = document.querySelector('.topbar');
  const mobile = window.matchMedia('(max-width: 1040px)');
  let previousFocus;

  const setMenuOpen = (open, restoreFocus = true) => {
    if (!sidebar || !toggle) return;
    open = open && mobile.matches;
    if (open) previousFocus = document.activeElement;
    sidebar.classList.toggle('is-open', open);
    document.body.classList.toggle('nav-open', open);
    toggle.setAttribute('aria-expanded', String(open));
    backdrop.hidden = !open;
    main.inert = open;
    topbar.inert = open;
    if (open) {
      sidebar.setAttribute('role', 'dialog');
      sidebar.setAttribute('aria-modal', 'true');
      sidebar.querySelector('[data-menu-close]').focus();
    } else {
      sidebar.removeAttribute('role');
      sidebar.removeAttribute('aria-modal');
      if (restoreFocus && previousFocus) previousFocus.focus();
    }
  };

  toggle?.addEventListener('click', () => setMenuOpen(true));
  document.querySelectorAll('[data-menu-close]').forEach(button => {
    button.addEventListener('click', () => setMenuOpen(false));
  });
  mobile.addEventListener('change', () => setMenuOpen(false, false));
  document.addEventListener('keydown', event => {
    if (!sidebar?.classList.contains('is-open')) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      setMenuOpen(false);
    }
    if (event.key === 'Tab') {
      const focusable = [...sidebar.querySelectorAll('a, button, summary, select, [tabindex="0"]')]
        .filter(element => !element.disabled && element.getClientRects().length);
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });

  document.querySelectorAll('[data-dismiss-flash]').forEach(button => {
    button.addEventListener('click', () => {
      button.closest('.flash').remove();
      main.focus({ preventScroll: true });
    });
  });
  document.querySelectorAll('[data-password-toggle]').forEach(button => {
    button.addEventListener('click', () => {
      const input = document.getElementById(button.dataset.passwordToggle);
      const show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      button.setAttribute('aria-pressed', String(show));
      button.setAttribute('aria-label', show ? button.dataset.hideLabel : button.dataset.showLabel);
    });
  });
})();
