/* Tests the production reader's asynchronous page transition without a browser. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadReader(fetchImpl) {
  const elements = new Map();
  const make = id => ({
    dataset: id === 'reader' ? {startPage: '1', heartbeatUrl: '/heartbeat', readingToken: 'test'} : {},
    style: {setProperty() {}}, value: id === 'zoom' ? 'fit' : '', clientWidth: 800,
    hidden: false, disabled: false, addEventListener() {}, setAttribute() {},
    replaceChildren() {}, getContext() { return {}; },
  });
  const document = {
    visibilityState: 'visible', hasFocus: () => true,
    querySelector: () => ({content: 'csrf'}),
    getElementById(id) { if (!elements.has(id)) elements.set(id, make(id)); return elements.get(id); },
    addEventListener() {},
  };
  const context = vm.createContext({
    document, window: {devicePixelRatio: 1, addEventListener() {}},
    GlobalWorkerOptions: {}, URL, AbortSignal, fetch: fetchImpl,
    performance: {now: () => 1000}, setInterval() {},
    TextLayer: class { render() { return Promise.resolve(); } cancel() {} },
  });
  const raw = fs.readFileSync(path.join(__dirname, '../static/reader.js'), 'utf8');
  const body = raw.slice(raw.indexOf('const root ='), raw.lastIndexOf('\ntry {\n  const asset'))
    .replace('import.meta.url', "'https://example.test/static/reader.js'");
  vm.runInContext(body + '\nglobalThis.readerTest = {renderPage, setPdf(value) { pdf=value; shownPage=1; trackingOkay=true; }, snapshot() { return {busy,fatal,shownPage}; }};', context);
  return {reader: context.readerTest, elements};
}

test('rapid navigation is serialised while the previous page heartbeat is pending', async () => {
  let release;
  let requests = 0;
  const response = {ok:true, json:async () => ({ok:true})};
  const {reader} = loadReader(() => ++requests === 1 ? new Promise(resolve => {release=()=>resolve(response);}) : Promise.resolve(response));
  let rendered = 0;
  reader.setPdf({numPages:3, getPage:async () => ({
    getViewport:({scale}) => ({width:600*scale,height:800*scale}),
    render() { rendered++; return {promise:Promise.resolve()}; },
    getTextContent:async () => ({items:[]}),
  })});
  const first = reader.renderPage(2);
  const concurrent = reader.renderPage(3);
  await Promise.resolve();
  assert.equal(reader.snapshot().busy, true);
  release();
  await Promise.all([first, concurrent]);
  assert.equal(rendered, 1, 'never render concurrently into the same canvas');
  assert.equal(reader.snapshot().shownPage, 2);
  assert.equal(reader.snapshot().fatal, false);
});

test('a revoked reading session stops navigation and hides completion', async () => {
  const {reader, elements} = loadReader(async () => ({ok:false, status:409}));
  let rendered = 0;
  reader.setPdf({numPages:3, getPage:async () => {rendered++;}});
  await reader.renderPage(2);
  assert.equal(rendered,0);
  assert.equal(reader.snapshot().fatal,true);
  assert.equal(elements.get('completion').hidden,true);
  assert.equal(elements.get('next-page').disabled,true);
});
