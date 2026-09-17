/** @jest-environment node */
const fs = require('node:fs');
const vm = require('node:vm');

test('failed offline-shell upgrade leaves the active bundle intact; activation retires it only after complete installation', async () => {
  const oldCache = new Map([['/static/outage/offline.js', 'old working shell']]);
  const cacheMaps = new Map([['von-outage-v1', oldCache]]);
  const caches = {
    open: async name => {
      if (!cacheMaps.has(name)) cacheMaps.set(name, new Map());
      return { put: async (key, value) => cacheMaps.get(name).set(key, value) };
    },
    keys: async () => [...cacheMaps.keys()],
    delete: async name => cacheMaps.delete(name)
  };
  const handlers = {};
  const self = { addEventListener: (name, handler) => { handlers[name] = handler; },
    skipWaiting: jest.fn(), clients: { claim: jest.fn() } };
  let dependencyAvailable = false;
  const fetch = jest.fn(async url => {
    if (url.endsWith('/health.js') && !dependencyAvailable) throw new TypeError('Interrupted update');
    const contentType = url.endsWith('.js') ? 'application/javascript' : url.endsWith('.css') ? 'text/css' : 'text/html';
    return new Response('new public asset', { headers: { 'Content-Type': contentType, 'Set-Cookie': 'must not cache' } });
  });
  vm.runInNewContext(fs.readFileSync('src/frontend/web/von_interface/static/outage/service-worker.js', 'utf8'), {
    self, caches, fetch, Response, URL
  });
  const run = event => new Promise((resolve, reject) => {
    handlers[event]({ waitUntil: promise => promise.then(resolve, reject) });
  });
  await expect(run('install')).rejects.toThrow('Interrupted update');
  expect(oldCache.get('/static/outage/offline.js')).toBe('old working shell');
  expect(self.skipWaiting).not.toHaveBeenCalled();

  dependencyAvailable = true;
  await run('install');
  expect(cacheMaps.has('von-outage-v1')).toBe(true);
  await run('activate');
  expect(cacheMaps.has('von-outage-v1')).toBe(false);
  const [installed] = [...cacheMaps.values()];
  expect(installed.has('/static/outage/health.js')).toBe(true);
  expect(await installed.get('/static/outage/offline.js').text()).toBe('new public asset');
  expect([...installed.values()].every(response => !response.headers.has('Set-Cookie'))).toBe(true);
});
