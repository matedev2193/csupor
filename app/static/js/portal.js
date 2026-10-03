/* Progressive enhancements; forms and navigation also work without JavaScript. */
(() => {
  const sidebar = document.querySelector('.sidebar');
  const toggle = document.querySelector('.menu-toggle');
  const backdrop = document.querySelector('.menu-backdrop');
  const main = document.querySelector('.main-content');
  const topbar = document.querySelector('.topbar');
  const mobile = window.matchMedia('(max-width: 1040px)');
  const groups = [...document.querySelectorAll('.sidebar-nav > .nav-group')];
  let previousFocus;

  const positionGroup = group => {
    if (mobile.matches || !group.open) return;
    const trigger = group.querySelector('summary');
    const panel = group.querySelector('.nav-group-menu');
    const top = Math.max(12, Math.min(trigger.getBoundingClientRect().top, window.innerHeight - panel.offsetHeight - 12));
    group.style.setProperty('--nav-panel-top', `${top}px`);
    group.style.setProperty('--nav-panel-left', `${sidebar.getBoundingClientRect().right + 8}px`);
  };

  const setGroupOpen = (group, open, restoreFocus = false) => {
    const trigger = group.querySelector('summary');
    group.open = open;
    trigger.setAttribute('aria-expanded', String(open));
    if (open) positionGroup(group);
    if (restoreFocus) trigger.focus({ preventScroll: true });
  };

  groups.forEach(group => {
    const trigger = group.querySelector('summary');
    setGroupOpen(group, mobile.matches && group.open);
    trigger.addEventListener('click', event => {
      event.preventDefault();
      const open = !group.open;
      groups.forEach(other => setGroupOpen(other, other === group && open));
    });
    trigger.addEventListener('keydown', event => {
      if (event.key !== 'ArrowRight') return;
      event.preventDefault();
      groups.forEach(other => setGroupOpen(other, other === group));
      group.querySelector('.nav-link')?.focus();
    });
    group.addEventListener('keydown', event => {
      if (!group.open || !['Escape', 'ArrowLeft'].includes(event.key)) return;
      event.preventDefault();
      event.stopPropagation();
      setGroupOpen(group, false, true);
    });
    group.addEventListener('toggle', () => {
      trigger.setAttribute('aria-expanded', String(group.open));
      positionGroup(group);
    });
  });
  sidebar?.classList.add('nav-enhanced');

  document.addEventListener('click', event => {
    if (mobile.matches) return;
    groups.forEach(group => {
      if (group.open && !group.contains(event.target)) {
        const focusInside = group.querySelector('.nav-group-menu').contains(document.activeElement);
        setGroupOpen(group, false, focusInside);
      }
    });
  });
  document.addEventListener('focusin', event => {
    if (mobile.matches) return;
    groups.forEach(group => {
      if (group.open && !group.contains(event.target)) setGroupOpen(group, false);
    });
  });
  window.addEventListener('resize', () => groups.forEach(positionGroup));
  sidebar?.querySelector('.sidebar-nav').addEventListener('scroll', () => groups.forEach(positionGroup));

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
  mobile.addEventListener('change', () => {
    setMenuOpen(false, false);
    groups.forEach(group => {
      const focusInside = group.querySelector('.nav-group-menu').contains(document.activeElement);
      setGroupOpen(group, mobile.matches && group.classList.contains('has-current'), !mobile.matches && focusInside);
    });
  });
  document.addEventListener('keydown', event => {
    if (!mobile.matches && event.key === 'Escape') {
      const openGroup = groups.find(group => group.open);
      if (openGroup) {
        event.preventDefault();
        setGroupOpen(openGroup, false, true);
      }
    }
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
