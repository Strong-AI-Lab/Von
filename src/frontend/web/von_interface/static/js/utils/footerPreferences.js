// Presentation preferences are browser-local, like conversation layout choices.
export const FOOTER_PREFERENCES_KEY = 'von:footerVisibility';
export const FOOTER_ITEMS = Object.freeze({
  model: ['Model', true],
  conversationCost: ['Conversation cost estimate', true],
  user: ['User', true],
  organisation: ['Organisation', true],
  language: ['Language', false],
  database: ['Database and connection details', false],
  uptime: ['Server uptime', false],
  runtimeCost: ['Cost estimate since server restart', false],
  build: ['Build revision and timestamp', false],
  diagnostics: ['Expert process and indexing details (when available)', false],
});

let fallback = null;
export function loadFooterPreferences() {
  let stored = fallback;
  if (!stored) {
    try { stored = JSON.parse(localStorage.getItem(FOOTER_PREFERENCES_KEY)); } catch (_) { }
  }
  return Object.fromEntries(Object.entries(FOOTER_ITEMS).map(([key, [, defaultValue]]) =>
    [key, typeof stored?.[key] === 'boolean' ? stored[key] : defaultValue]));
}

export function applyFooterPreferences() {
  const preferences = loadFooterPreferences();
  for (const [key, value] of Object.entries(preferences)) {
    document.documentElement.setAttribute(`data-footer-${key.toLowerCase()}`, String(value));
    const control = document.querySelector(`[data-footer-preference="${key}"]`);
    if (control) control.checked = value;
  }
}

export function saveFooterPreference(key, value) {
  if (!Object.hasOwn(FOOTER_ITEMS, key)) return;
  const preferences = { ...loadFooterPreferences(), [key]: Boolean(value) };
  try {
    localStorage.setItem(FOOTER_PREFERENCES_KEY, JSON.stringify(preferences));
    fallback = null;
  } catch (_) { fallback = preferences; }
  applyFooterPreferences();
  // Embedded Settings shares localStorage but does not get a storage event in
  // the writing window. Notify its parent without reloading footer data.
  try { window.parent.dispatchEvent(new CustomEvent('von-footer-preferences-changed')); } catch (_) { }
}

export function resetFooterPreferences() {
  try {
    localStorage.removeItem(FOOTER_PREFERENCES_KEY);
    fallback = null;
  } catch (_) {
    fallback = Object.fromEntries(Object.entries(FOOTER_ITEMS).map(([key, [, value]]) => [key, value]));
  }
  applyFooterPreferences();
  try { window.parent.dispatchEvent(new CustomEvent('von-footer-preferences-changed')); } catch (_) { }
}

export function mountFooterPreferences() {
  const container = document.getElementById('footerVisibilitySettings');
  if (container && !container.children.length) {
    for (const [key, [name]] of Object.entries(FOOTER_ITEMS)) {
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.dataset.footerPreference = key;
      input.addEventListener('change', () => saveFooterPreference(key, input.checked));
      label.append(input, document.createTextNode(` ${name}`));
      container.append(label);
    }
  }
  applyFooterPreferences();
}

window.addEventListener('storage', (event) => {
  if (event.key === FOOTER_PREFERENCES_KEY || event.key === null) applyFooterPreferences();
});
window.addEventListener('von-footer-preferences-changed', applyFooterPreferences);
applyFooterPreferences();
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mountFooterPreferences);
} else {
  mountFooterPreferences();
}
