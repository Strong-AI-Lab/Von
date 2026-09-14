// Public operational data only. Never store health payloads or authenticated responses.
const CACHE_KEY = 'von:publicMaintenance:v1';
export function maintenanceMessage(record, { now = Date.now(), cached = false } = {}) {
  if (!record || record.schema_version !== 'von_maintenance.v1' ||
      !['planned', 'recovering', 'failed', 'ready', 'cancelled'].includes(record.state) ||
      typeof record.agent !== 'string' || typeof record.release_id !== 'string') {
    return 'Return time unknown. No maintenance update is available.';
  }
  const updated = Date.parse(record.updated_at);
  const expires = Date.parse(record.expires_at);
  const ready = Date.parse(record.estimated_ready_at);
  const format = ms => new Date(ms).toLocaleString(undefined, { timeZoneName: 'short' });
  const source = `${cached ? 'Last known update' : 'Update'} from ${record.agent.slice(0, 100)}${Number.isFinite(updated) ? ` at ${format(updated)}` : ''}.`;
  if (!Number.isFinite(updated) || !Number.isFinite(expires) || now >= expires || updated > now + 60000) {
    return `${source} This update is stale; return time unknown.`;
  }
  if (record.state === 'ready' || record.state === 'cancelled') {
    return `${source} Maintenance ${record.state === 'ready' ? 'finished' : 'cancelled'}; checking the connection to Von.`;
  }
  const reason = typeof record.reason === 'string' ? record.reason.slice(0, 280) : 'Planned maintenance';
  if (record.state === 'failed') return `${reason}. ${source} Recovery is delayed; return time unknown.`;
  if (!Number.isFinite(ready)) return `${reason}. ${source} Return time unknown.`;
  if (ready <= now) return `${reason}. ${source} The estimated return time has passed. Recovery is delayed; a new return time is unknown.`;
  return `${reason}. ${source} Estimated return: ${format(ready)}. This is an estimate, not a guarantee.`;
}

export async function readMaintenance() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    // An operator-owned independent static route; a missing route is supported.
    const response = await fetch('/von-status/maintenance.json', {
      cache: 'no-store', credentials: 'omit', redirect: 'error', signal: controller.signal
    });
    if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('No public status');
    const text = await response.text();
    if (text.length > 4096) throw new Error('Oversized public status');
    const value = JSON.parse(text);
    // Allowlist data even if the independent server accidentally returns extra fields.
    const record = Object.fromEntries(['schema_version', 'state', 'release_id', 'agent', 'reason', 'planned_start', 'estimated_ready_at', 'updated_at', 'expires_at'].map(key => [key, value[key]]));
    if (record.schema_version !== 'von_maintenance.v1') throw new Error('Unknown public status');
    try { localStorage.setItem(CACHE_KEY, JSON.stringify(record)); } catch { /* optional */ }
    return { record, cached: false };
  } catch {
    try { return { record: JSON.parse(localStorage.getItem(CACHE_KEY)), cached: true }; }
    catch { return { record: null, cached: true }; }
  } finally { clearTimeout(timer); }
}
