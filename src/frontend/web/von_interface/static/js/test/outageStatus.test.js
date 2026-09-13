import { maintenanceMessage, readMaintenance } from '../../outage/status.js';
const now = Date.parse('2026-09-12T22:00:00Z');
const record = {
  schema_version: 'von_maintenance.v1', state: 'planned', agent: 'Codex DGX',
  release_id: 'fixture-release', reason: 'Von is being updated',
  updated_at: '2026-09-12T21:59:00Z', expires_at: '2026-09-12T22:30:00Z',
  estimated_ready_at: '2026-09-12T22:05:00Z'
};
test('ETA is attributable, localised and explicitly an estimate', () => {
  const message = maintenanceMessage(record, { now });
  expect(message).toContain('Codex DGX');
  expect(message).toContain('Estimated return:');
  expect(message).toContain('not a guarantee');
});
test.each([
  [null, 'unknown'],
  [{ ...record, estimated_ready_at: null }, 'unknown'],
  [{ ...record, expires_at: '2026-09-12T21:00:00Z' }, 'stale'],
  [{ ...record, estimated_ready_at: '2026-09-12T21:59:00Z' }, 'has passed'],
  [{ ...record, state: 'failed' }, 'delayed'],
  [{ ...record, state: 'ready' }, 'checking the connection'],
])('missing, stale, late and terminal updates do not promise readiness', (value, expected) => {
  const message = maintenanceMessage(value, { now, cached: true });
  expect(message).toContain(expected);
  expect(message).not.toContain('Estimated return:');
});
test('status cache contains only public fields and fallback is labelled', async () => {
  localStorage.clear();
  global.fetch = jest.fn().mockResolvedValue({ ok: true,
    headers: { get: () => 'application/json' },
    text: async () => JSON.stringify({ ...record, private_history: 'must not store' })
  });
  expect((await readMaintenance()).cached).toBe(false);
  expect(localStorage.getItem('von:publicMaintenance:v1')).not.toContain('private_history');
  expect(fetch.mock.calls[0][1].credentials).toBe('omit');
  fetch.mockRejectedValue(new TypeError('offline'));
  const fallback = await readMaintenance();
  expect(maintenanceMessage(fallback.record, { now, ...fallback })).toContain('Last known update');
});

const { preserveOutageDraft } = require('../../outage/draft.js');
test('reload recovery is namespace-bound and never fills or sends the composer', () => {
  sessionStorage.clear();
  document.body.innerHTML = '<main><div><textarea id="promptInput"></textarea></div></main>';
  let namespace = 'alice@org';
  const cleanup = preserveOutageDraft(() => namespace);
  document.getElementById('promptInput').value = 'Unsent draft';
  window.dispatchEvent(new Event('pagehide'));
  cleanup();
  document.getElementById('promptInput').value = '';
  const otherCleanup = preserveOutageDraft(() => 'bob@org');
  expect(document.getElementById('vonRecoveredDraft')).toBeNull();
  otherCleanup();
  const reloadCleanup = preserveOutageDraft(() => namespace);
  expect(document.querySelector('#vonRecoveredDraft textarea').value).toBe('Unsent draft');
  expect(document.getElementById('promptInput').value).toBe('');
  document.dispatchEvent(new Event('orgSwitchStarted'));
  expect(document.getElementById('vonRecoveredDraft')).toBeNull();
  namespace = 'alice@other-org';
  document.getElementById('promptInput').value = 'Other organisation draft';
  window.dispatchEvent(new Event('pagehide'));
  expect(sessionStorage.getItem('von:unsentReloadDraft:v1:alice@org')).not.toContain('Other organisation');
  reloadCleanup();
});
