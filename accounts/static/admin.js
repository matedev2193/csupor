'use strict';
(() => {
  const status = (message) => {
    for (const element of document.querySelectorAll('[data-copy-status]')) element.textContent = message;
  };
  for (const button of document.querySelectorAll('[data-generate-secret]')) {
    button.addEventListener('click', () => {
      const input = document.getElementById(button.dataset.generateSecret);
      const bytes = new Uint8Array(32);
      crypto.getRandomValues(bytes);
      input.value = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
      status('A véletlen kulcs elkészült. Másold át a kapcsolódó alkalmazás beállításába, majd mentsd az űrlapot.');
    });
  }
  for (const button of document.querySelectorAll('[data-copy-input], [data-copy-value]')) {
    button.addEventListener('click', async () => {
      const input = button.dataset.copyInput ? document.getElementById(button.dataset.copyInput) : null;
      const value = input ? input.value : button.dataset.copyValue;
      if (!value) { status('Előbb adj meg vagy készíts egy kulcsot.'); return; }
      try {
        await navigator.clipboard.writeText(value);
        status('Az értéket a vágólapra másoltuk.');
      } catch (_) {
        if (input) { input.focus(); input.select(); }
        status('A böngésző nem engedte a másolást. Jelöld ki és másold ki az értéket kézzel.');
      }
    });
  }
})();
