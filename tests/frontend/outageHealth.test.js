import { fetchVonHealth, healthErrorKind, connectionMessage } from '../../src/frontend/web/von_interface/static/outage/health.js';

afterEach(() => { delete global.fetch; });

test('healthy JSON is read without following an edge redirect or caching the response', async () => {
  const data = { status: 'healthy', start_time: '2026-09-16T00:00:00Z' };
  global.fetch = jest.fn().mockResolvedValue({ ok: true, status: 200,
    headers: { get: () => 'application/json' }, json: async () => data });
  expect(await fetchVonHealth()).toEqual(data);
  expect(fetch).toHaveBeenCalledWith('/health', expect.objectContaining({
    redirect: 'manual', cache: 'no-store', credentials: 'same-origin'
  }));
});

test.each([
  [{ type: 'opaqueredirect', status: 0 }, 'redirect'],
  [{ status: 302 }, 'redirect'],
  [{ status: 401 }, 'access'],
  [{ status: 403 }, 'access'],
  [{ status: 503 }, 'http'],
  [{ ok: true, status: 200, headers: { get: () => 'text/html' } }, 'unexpected_response'],
  [{ ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => ({}) }, 'unexpected_response'],
  [{ ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => { throw new SyntaxError(); } }, 'unexpected_response'],
])('unhealthy responses retain the observed cause without asserting expired authentication', async (response, kind) => {
  global.fetch = jest.fn().mockResolvedValue(response);
  await expect(fetchVonHealth()).rejects.toMatchObject({ healthErrorKind: kind });
});

test('opaque fetch failure remains uncertain; offline and HTTP failures have appropriate copy', () => {
  expect(healthErrorKind(new TypeError('Failed to fetch'))).toBe('network_or_unknown');
  expect(connectionMessage('network_or_unknown', true)).toContain('may have expired');
  expect(connectionMessage('network_or_unknown', true)).toContain('network or server problem');
  expect(connectionMessage('redirect', true)).toContain('does not confirm a Von outage');
  expect(connectionMessage('access', true)).toContain('Access was refused');
  expect(connectionMessage('http', true)).toContain('gateway returned an error');
  expect(connectionMessage('redirect', false)).toContain('device reports that it is offline');
  expect(healthErrorKind({ name: 'AbortError' })).toBe('timeout');
});
