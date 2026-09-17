// A failed health request describes this browser's connection, not necessarily
// backend downtime. In particular, Access redirects can be opaque to fetch.
export async function fetchVonHealth({ signal } = {}) {
  const response = await fetch('/health', {
    cache: 'no-store', credentials: 'same-origin', redirect: 'manual', signal
  });
  const fail = (kind, message) => { throw Object.assign(new Error(message), { healthErrorKind: kind }); };
  if (response.type === 'opaqueredirect' || response.redirected ||
      (response.status >= 300 && response.status < 400)) {
    fail('redirect', 'Health request was redirected');
  }
  if (response.status === 401 || response.status === 403) {
    fail('access', `HTTP ${response.status}`);
  }
  if (!response.ok) fail('http', `HTTP ${response.status}`);
  if (!response.headers.get('content-type')?.includes('application/json')) {
    fail('unexpected_response', 'Expected a Von health response');
  }
  let data;
  try { data = await response.json(); }
  catch { fail('unexpected_response', 'Invalid Von health response'); }
  if (data?.status !== 'healthy' || typeof data.start_time !== 'string') {
    fail('unexpected_response', 'Invalid Von health response');
  }
  return data;
}

export function healthErrorKind(error) {
  return error?.healthErrorKind || (error?.name === 'AbortError' ? 'timeout' : 'network_or_unknown');
}

export function connectionMessage(kind, online = navigator.onLine) {
  if (online === false) return 'Your device reports that it is offline. Check your connection; Von’s status is unknown.';
  if (kind === 'redirect') return 'The connection was redirected. You may need to sign in again; this does not confirm a Von outage.';
  if (kind === 'access') return 'Access was refused. Try signing in again. If access is still refused, check your account’s access to Von.';
  if (kind === 'http') return 'Von or its gateway returned an error. We’ll keep checking the connection.';
  return 'The connection could not be checked. Your sign-in may have expired, or there may be a network or server problem.';
}
