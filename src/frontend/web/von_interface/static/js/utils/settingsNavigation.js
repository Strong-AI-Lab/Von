export function openSettingsTabAndFocus(focusMessageType, options = {}) {
  const retries = Number.isFinite(Number(options?.retries)) ? Math.max(0, Number(options.retries)) : 8;
  const retryDelayMs = Number.isFinite(Number(options?.retryDelayMs)) ? Math.max(50, Number(options.retryDelayMs)) : 250;
  const messageType = typeof focusMessageType === 'string' ? focusMessageType.trim() : '';

  try {
    const settingsTabButton = document.querySelector('.tab-button[data-tab="settingsTab"]');
    if (settingsTabButton) {
      settingsTabButton.click();
    }

    if (!messageType) {
      return !!settingsTabButton;
    }

    const sendFocusMessage = (attempt = 0) => {
      const frame = document.getElementById('settingsFrame');
      const targetWindow = frame?.contentWindow;
      if (targetWindow) {
        try {
          targetWindow.postMessage({ type: messageType }, window.location.origin);
        } catch (_) {
          // Best-effort focus handoff.
        }
        return;
      }
      if (attempt < retries) {
        setTimeout(() => sendFocusMessage(attempt + 1), retryDelayMs);
      }
    };

    sendFocusMessage(0);
    return true;
  } catch (_) {
    return false;
  }
}
