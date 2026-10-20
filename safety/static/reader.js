import {getDocument, GlobalWorkerOptions, TextLayer} from './vendor/pdfjs/pdf.mjs';

const root = document.getElementById('reader');
const csrf = document.querySelector('meta[name="csrf-token"]').content;
GlobalWorkerOptions.workerSrc = new URL('./vendor/pdfjs/pdf.worker.mjs', import.meta.url).href;
const canvas = document.getElementById('pdf-canvas');
const textContainer = document.getElementById('pdf-text');
const sheet = document.getElementById('pdf-sheet');
const stage = document.getElementById('pdf-stage');
const status = document.getElementById('reader-status');
const pageInput = document.getElementById('page-number');
const previous = document.getElementById('previous-page');
const next = document.getElementById('next-page');
const go = document.getElementById('go-page');
const zoom = document.getElementById('zoom');
const completion = document.getElementById('completion');
const errorBox = document.getElementById('reader-error');
const errorText = document.getElementById('reader-error-text');
let pdf;
let pageNumber = Number(root.dataset.startPage) || 1;
let shownPage = 0;
let sequence = 0;
let activeSeconds = 0;
let clock = performance.now();
let lastActivity = clock;
let busy = false;
let fatal = false;
let trackingOkay = false;
let queue = Promise.resolve(true);
let currentTextLayer = null;

function showError(message, terminal = false) {
  errorText.textContent = message; errorBox.hidden = false;
  trackingOkay = false; fatal = fatal || terminal; activeSeconds = 0;
  if (fatal) navigationDisabled(true);
  if (completion) completion.hidden = true;
}
function updateCompletion() {
  if (completion) completion.hidden = busy || !trackingOkay || fatal || shownPage !== pdf?.numPages;
}
function isActive() {
  return trackingOkay && !fatal && !busy && shownPage > 0 && document.visibilityState === 'visible' && document.hasFocus() && performance.now() - lastActivity < 60000;
}
function tick() {
  const now = performance.now();
  const elapsed = Math.min(1.5, Math.max(0, (now - clock) / 1000));
  if (isActive()) activeSeconds += elapsed;
  clock = now;
}
function heartbeat({keepalive = false} = {}) {
  if (!shownPage || fatal) return Promise.resolve(false);
  tick();
  const payload = {token: root.dataset.readingToken, sequence: ++sequence, page: shownPage, active_seconds: Math.floor(activeSeconds)};
  activeSeconds = 0;
  queue = queue.then(async () => {
    if (fatal) return false;
    try {
      const response = await fetch(root.dataset.heartbeatUrl, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify(payload), keepalive, redirect: 'error', signal: AbortSignal.timeout(12000)});
      if (!response.ok) {
        showError(response.status === 409 ? 'Ez az olvasási munkamenet már nem aktív. Lehet, hogy másik ablakban megnyitottad a tananyagot. A folytatáshoz töltsd újra az oldalt.' : 'Az olvasás rögzítése nem sikerült. A folytatáshoz töltsd újra az oldalt, vagy jelentkezz be ismét.', [400,401,403,404,409].includes(response.status));
        return false;
      }
      const data = await response.json();
      if (data.ok !== true) throw new Error('Unexpected response');
      trackingOkay = true; errorBox.hidden = true; updateCompletion();
      return true;
    } catch {
      showError('Az olvasás rögzítése átmenetileg nem elérhető. Ellenőrizd az internetkapcsolatot. A lezárást csak sikeres mentés után végezheted el.');
      return false;
    }
  });
  return queue;
}
function navigationDisabled(disabled) {
  previous.disabled = disabled || pageNumber <= 1;
  next.disabled = disabled || !pdf || pageNumber >= pdf.numPages;
  pageInput.disabled = disabled; go.disabled = disabled; zoom.disabled = disabled;
}
async function renderPage(number) {
  if (!pdf || busy || fatal || number < 1 || number > pdf.numPages) return;
  tick();
  busy = true; navigationDisabled(true); updateCompletion();
  if (shownPage) await heartbeat();
  if (fatal) { busy = false; return; }
  pageNumber = number;
  status.textContent = 'Az oldal betöltése…';
  try {
    const page = await pdf.getPage(number);
    const natural = page.getViewport({scale: 1});
    const scale = zoom.value === 'fit' ? Math.max(0.3, Math.min(2, (stage.clientWidth - 52) / natural.width)) : Number(zoom.value);
    const viewport = page.getViewport({scale});
    const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(viewport.width * pixelRatio); canvas.height = Math.floor(viewport.height * pixelRatio);
    canvas.style.width = `${viewport.width}px`; canvas.style.height = `${viewport.height}px`;
    sheet.style.width = `${viewport.width}px`; sheet.style.height = `${viewport.height}px`;
    sheet.style.setProperty('--total-scale-factor', String(scale));
    if (currentTextLayer) currentTextLayer.cancel();
    textContainer.replaceChildren();
    await page.render({canvasContext: canvas.getContext('2d'), viewport, transform: pixelRatio === 1 ? null : [pixelRatio,0,0,pixelRatio,0,0]}).promise;
    try {
      const textContent = await page.getTextContent();
      currentTextLayer = new TextLayer({textContentSource: textContent, container: textContainer, viewport});
      await currentTextLayer.render();
    } catch {
      // A valid canvas remains usable when an unusual PDF has no text layer.
      textContainer.replaceChildren();
    }
    shownPage = number; pageInput.value = String(number);
    canvas.setAttribute('aria-label', `Tananyag, ${number}. oldal`);
    status.textContent = `${number}. oldal`;
    activeSeconds = 0; clock = performance.now(); lastActivity = clock;
    busy = false; navigationDisabled(false);
    await heartbeat(); updateCompletion();
  } catch {
    busy = false; navigationDisabled(false);
    showError('Ezt az oldalt nem sikerült megjeleníteni. Próbáld újratölteni a tananyagot.', true);
  }
}
previous.addEventListener('click', () => renderPage(pageNumber - 1));
next.addEventListener('click', () => renderPage(pageNumber + 1));
document.getElementById('page-form').addEventListener('submit', event => {
  event.preventDefault(); if (pageInput.reportValidity()) renderPage(Number(pageInput.value));
});
zoom.addEventListener('change', () => renderPage(pageNumber));
['pointerdown','pointermove','keydown','scroll','wheel','touchstart'].forEach(type => document.addEventListener(type, () => { lastActivity = performance.now(); }, {passive: true, capture: type === 'scroll'}));
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') heartbeat({keepalive: true});
  activeSeconds = 0; clock = performance.now(); lastActivity = clock;
});
window.addEventListener('blur', () => { heartbeat({keepalive: true}); activeSeconds = 0; clock = performance.now(); });
window.addEventListener('focus', () => { clock = performance.now(); lastActivity = clock; activeSeconds = 0; });
window.addEventListener('pagehide', () => heartbeat({keepalive: true}));
setInterval(tick, 1000);
setInterval(() => { if (shownPage && !busy && !fatal && document.visibilityState === 'visible' && document.hasFocus()) heartbeat(); }, 15000);
let submitting = false;
const completionForm = document.getElementById('completion-form');
if (completionForm) completionForm.addEventListener('submit', async event => {
  if (submitting) return;
  event.preventDefault();
  if (await heartbeat()) { submitting = true; completionForm.requestSubmit(); }
});
const examLink = document.getElementById('exam-link');
if (examLink) examLink.addEventListener('click', async event => {
  event.preventDefault(); if (await heartbeat()) window.location.assign(examLink.href);
});
try {
  const asset = name => new URL(`./vendor/pdfjs/${name}/`, import.meta.url).href;
  pdf = await getDocument({url: root.dataset.pdfUrl, cMapUrl: asset('cmaps'), cMapPacked: true, standardFontDataUrl: asset('standard_fonts'), wasmUrl: asset('wasm'), isEvalSupported: false, enableXfa: false}).promise;
  if (pdf.numPages !== Number(root.dataset.pageCount)) throw new Error('Page count changed');
  pageNumber = Math.min(Math.max(1,pageNumber),pdf.numPages);
  await renderPage(pageNumber);
} catch {
  showError('A PDF nem tölthető be. Töltsd újra az oldalt, vagy kérj segítséget az oktatás felelősétől.', true);
  status.textContent = 'A tananyag nem érhető el.';
}
