import { maintenanceMessage, readMaintenance } from './status.js';
let inFlight = false;
let failures = 0;
let timer;
async function check() {
  if (inFlight) return;
  inFlight = true;
  clearTimeout(timer);
  const button = document.getElementById('retry');
  button.disabled = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  const status = readMaintenance().then(value => {
    document.getElementById('maintenance').textContent = maintenanceMessage(value.record, value);
  });
  try {
    const response = await fetch('/health', { cache: 'no-store', redirect: 'error', signal: controller.signal });
    const health = response.ok ? await response.json() : null;
    if (health?.status === 'healthy' && typeof health.start_time === 'string' &&
        health.runtime_authority?.startup_seed_materialisations?.ready !== false) {
      // Health may be routed independently of app HTML. Do not loop on a proxy
      // error page while health alone is available. This response is never cached.
      const app = await fetch(location.href, { cache: 'no-store', redirect: 'error', signal: controller.signal });
      if (app.ok && app.headers.get('content-type')?.includes('text/html') &&
          (await app.text()).includes('name="von-app-shell" content="1"')) {
        // A fresh navigation restores authentication normally. No effects replayed.
        location.reload();
        return;
      }
    }
  } catch { /* A proxy error or device-offline event does not identify the server-side cause. */ }
  finally {
    clearTimeout(timeout);
    await status;
    document.getElementById('checked').textContent = `Last checked: ${new Date().toLocaleTimeString()}`;
    document.getElementById('connection').textContent = navigator.onLine === false
      ? 'Your device reports that it is offline. Check your connection; Von’s status is unknown.'
      : 'Cannot reach Von. The cause is unknown. We’ll keep checking.';
    button.disabled = false;
    inFlight = false;
    failures++;
    timer = setTimeout(check, Math.min(30000, 5000 * 2 ** Math.min(failures - 1, 3)));
  }
}
document.getElementById('retry').addEventListener('click', check);
window.addEventListener('online', check);
void check();
