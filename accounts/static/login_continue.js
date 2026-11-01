/* End the form submission on this origin before following an OIDC redirect. */
(() => {
  const link = document.getElementById('login-continuation');
  if (!link) return;
  const destination = new URL(link.href, window.location.href);
  if (destination.origin === window.location.origin) {
    window.location.replace(destination.href);
  }
})();
