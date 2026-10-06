/* The reset secret stays out of request URLs, access logs and Referer headers. */
(() => {
  const input = document.getElementById('reset-token');
  const fragment = window.location.hash.slice(1);
  if (input && /^[A-Za-z0-9_-]{43}$/.test(fragment)) {
    input.value = fragment;
  }
  if (window.location.hash) {
    window.history.replaceState(null, '', window.location.pathname + window.location.search);
  }
})();
