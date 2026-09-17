import { maintenanceMessage, readMaintenance } from './status.js';
import { connectionMessage } from './health.js';

// The existing health poll owns the decision; this module only presents it.
export function installOutageView() {
  const dialog = document.createElement('dialog');
  dialog.className = 'von-outage';
  dialog.setAttribute('aria-labelledby', 'vonOutageTitle');
  dialog.innerHTML = '<section><h1 id="vonOutageTitle">Connection to Von interrupted</h1><p data-connection></p><p><a data-sign-in href="/von/" target="_blank" rel="noopener noreferrer">Sign in again in a new tab</a></p><p>Complete sign-in with the same account, then return to this tab and choose Check status / Retry. Keep this tab open: your conversation, unsent text and attachments stay here.</p><p data-maintenance role="status"></p><small data-checked></small><p><button type="button" data-retry>Check status / Retry</button><button type="button" data-return>Return to my work</button></p><p>We’ll reconnect automatically. An interrupted message will not be sent again; check its result before resending.</p></section>';
  document.body.append(dialog);
  let lastFocus = null;
  let state = 'waiting';
  let dismissed = false;
  let statusInFlight = false;
  let status = { record: null, cached: true };
  let lastChecked = null;
  let lastErrorKind = null;
  let signInAttempted = false;
  const updateText = () => {
    dialog.querySelector('[data-connection]').textContent = connectionMessage(lastErrorKind);
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
    lastErrorKind = detail.lastErrorKind || detail.latestErrorKind || null;
    lastChecked = Date.now();
    updateText();
    if (state === 'healthy') {
      dismissed = false;
      close();
      if (previous === 'down' || signInAttempted) {
        signInAttempted = false;
        document.dispatchEvent(new CustomEvent('von:connectionRestored'));
      }
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
  // A normal user-initiated navigation can complete an edge sign-in that fetch
  // cannot. Never navigate/reload the working tab or pass draft data to the new one.
  dialog.querySelector('[data-sign-in]').addEventListener('click', () => { signInAttempted = true; });
  dialog.addEventListener('cancel', event => { event.preventDefault(); dismissed = true; close(); });
  window.addEventListener('online', () => {
    document.dispatchEvent(new CustomEvent('von:requestHealthCheck'));
    if (state === 'down') void refreshStatus();
  });
  window.addEventListener('offline', updateText);
  const checkOnReturn = () => {
    if (document.visibilityState !== 'hidden' && (state === 'down' || signInAttempted)) {
      document.dispatchEvent(new CustomEvent('von:requestHealthCheck'));
    }
  };
  window.addEventListener('focus', checkOnReturn);
  document.addEventListener('visibilitychange', checkOnReturn);
  // Fetch before an outage so the last-known attributed record can survive it.
  void refreshStatus();
  setInterval(() => { if (dialog.open) updateText(); }, 10000);
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/von/service-worker.js', { scope: '/von/', updateViaCache: 'none' })
      .catch(() => { /* The open page still recovers if installation is unavailable. */ });
  }
}
