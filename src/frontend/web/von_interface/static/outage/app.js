import { maintenanceMessage, readMaintenance } from './status.js';

// The existing health poll owns the decision; this module only presents it.
export function installOutageView() {
  const dialog = document.createElement('dialog');
  dialog.className = 'von-outage';
  dialog.setAttribute('aria-labelledby', 'vonOutageTitle');
  dialog.innerHTML = '<section><h1 id="vonOutageTitle">Cannot reach Von</h1><p>Sorry, Von cannot connect right now.</p><p data-connection></p><p data-maintenance role="status"></p><small data-checked></small><p><button type="button" data-retry>Check status / Retry</button><button type="button" data-return>Return to my work</button></p><p>Your open work stays in place. We’ll reconnect automatically. An interrupted message will not be sent again; check its result before resending.</p></section>';
  document.body.append(dialog);
  let lastFocus = null;
  let state = 'waiting';
  let dismissed = false;
  let statusInFlight = false;
  let status = { record: null, cached: true };
  let lastChecked = null;
  const updateText = () => {
    dialog.querySelector('[data-connection]').textContent = navigator.onLine === false
      ? 'Your device reports that it is offline. Check your connection; Von’s status is unknown.'
      : 'Cannot reach Von. The cause is unknown.';
    dialog.querySelector('[data-maintenance]').textContent = maintenanceMessage(status.record, status);
    dialog.querySelector('[data-checked]').textContent = lastChecked ? `Last checked: ${new Date(lastChecked).toLocaleTimeString()}` : 'No check completed yet.';
  };
  const refreshStatus = async () => {
    if (statusInFlight) return;
    statusInFlight = true;
    try { status = await readMaintenance(); updateText(); }
    finally { statusInFlight = false; }
  };
  const close = () => {
    if (!dialog.open) return;
    dialog.close();
    if (lastFocus?.isConnected) lastFocus.focus({ preventScroll: true });
  };
  document.addEventListener('von:healthPollDiagnostics', event => {
    const detail = event.detail || {};
    const previous = state;
    state = detail.state;
    lastChecked = Date.now();
    updateText();
    if (state === 'healthy') {
      dismissed = false;
      close();
    } else if (state === 'down' && !detail.isThinkingActive && !dismissed && !dialog.open) {
      lastFocus = document.activeElement;
      dialog.showModal();
    }
    if (state === 'down' || previous === 'down') void refreshStatus();
  });
  dialog.querySelector('[data-retry]').addEventListener('click', () => {
    document.dispatchEvent(new CustomEvent('von:requestHealthCheck'));
    void refreshStatus();
  });
  dialog.querySelector('[data-return]').addEventListener('click', () => { dismissed = true; close(); });
  dialog.addEventListener('cancel', event => { event.preventDefault(); dismissed = true; close(); });
  window.addEventListener('online', () => {
    document.dispatchEvent(new CustomEvent('von:requestHealthCheck'));
    if (state === 'down') void refreshStatus();
  });
  window.addEventListener('offline', updateText);
  // Fetch before an outage so the last-known attributed record can survive it.
  void refreshStatus();
  setInterval(() => { if (dialog.open) updateText(); }, 10000);
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/von/outage-worker.js', { scope: '/von/', updateViaCache: 'none' })
      .catch(() => { /* The open page still recovers if installation is unavailable. */ });
  }
}
