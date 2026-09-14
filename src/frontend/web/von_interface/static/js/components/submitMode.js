// Shared browser preference and presentation; delivery remains owned by each route.
const fallbackModes = new Map();
const eventName = 'von:submit-mode-change';

export function getSubmitMode(actorKey) {
    if (!actorKey) return 'queue';
    try { return localStorage.getItem(`von:chat-submit-mode:${actorKey}`) === 'steer' ? 'steer' : 'queue'; }
    catch (_) { return fallbackModes.get(actorKey) || 'queue'; }
}

export function setSubmitMode(actorKey, value) {
    const mode = value === 'steer' ? 'steer' : 'queue';
    if (!actorKey) return;
    fallbackModes.set(actorKey, mode);
    try { localStorage.setItem(`von:chat-submit-mode:${actorKey}`, mode); } catch (_) { /* Keep the in-memory preference. */ }
    window.dispatchEvent(new CustomEvent(eventName, { detail: { actorKey } }));
}

export function subscribeSubmitMode(listener) {
    window.addEventListener(eventName, listener);
    window.addEventListener('storage', listener);
    return () => {
        window.removeEventListener(eventName, listener);
        window.removeEventListener('storage', listener);
    };
}

export function selectedSubmitMode(actorKey, event = {}) {
    const mode = getSubmitMode(actorKey);
    return event.shiftKey ? (mode === 'queue' ? 'steer' : 'queue') : mode;
}

export function presentSubmitMode(button, mode, { label, help, pending } = {}) {
    button.classList.add('composer-submit-arrow');
    button.dataset.submitMode = mode;
    if (label) {
        button.setAttribute('aria-label', label);
        const text = button.querySelector('.button-label');
        if (text) text.textContent = label;
    }
    if (help) button.title = help;
    if (pending !== undefined) button.setAttribute('aria-busy', String(pending));
}

export const submitArrowMarkup = '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5m-7 7 7-7 7 7" /></svg><span class="button-label sr-only">Submit message</span>';
