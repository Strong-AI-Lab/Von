import { initializeConceptAutocomplete } from './conceptAutocomplete.js';
import { initializePromptCartoucheOverlay, normaliseVontologyIdsForBackend } from './promptCartoucheOverlay.js';
import { isCompactComposer, resizeCompactDraft, shouldSubmitComposerKey } from './compactComposer.js';

export { normaliseVontologyIdsForBackend };

// All conversation drafts use the Von editor, including its token rendering and
// keyboard ordering. Delivery, attachment ownership and history stay with callers.
export function resizeConversationDraft(input) {
    if (!(input instanceof HTMLTextAreaElement)) return;
    input.dataset.hasDraft = String(Boolean(input.value.trim()));
    if (isCompactComposer()) { resizeCompactDraft(input); return; }
    input.style.height = 'auto';
    const height = Math.min(144, Math.max(44, Number(input.scrollHeight) || 44));
    input.style.height = `${height}px`;
    input.style.overflowY = input.scrollHeight > 144 ? 'auto' : 'hidden';
}

export function setConversationDraft(input, value) {
    if (!input) return;
    input.value = value;
    const event = new Event('input', { bubbles: true });
    event.conversationDraftRestored = true;
    input.dispatchEvent(event);
}

export function initialiseConversationDraft({ input, sendButton, onSubmit, onInput, onHistory }) {
    if (!input || input.dataset.conversationDraftBound) return;
    input.dataset.conversationDraftBound = 'true';
    input.classList.add('prompt-input');
    input.rows = 1;
    initializeConceptAutocomplete(input);
    initializePromptCartoucheOverlay(input);
    // Autocomplete must get first refusal: Enter selects a concept, never sends it.
    input.addEventListener('keydown', event => {
        if (event.defaultPrevented) return;
        onHistory?.(event);
        if (!event.defaultPrevented && shouldSubmitComposerKey(event)) {
            event.preventDefault();
            onSubmit(event);
        }
    });
    sendButton?.addEventListener('click', onSubmit);
    input.addEventListener('input', event => {
        resizeConversationDraft(input);
        if (!event.conversationDraftRestored) onInput?.();
    });
    const resize = () => { if (input.isConnected) resizeConversationDraft(input); };
    window.addEventListener('resize', resize);
    window.visualViewport?.addEventListener('resize', resize);
    resize();
}

export function navigateConversationDraftHistory(event, input, history, cursor) {
    if (!event || !['ArrowUp', 'ArrowDown'].includes(event.key)
        || event.ctrlKey || event.altKey || event.metaKey || event.isComposing || !history.length) return null;
    const value = input.value || '';
    if (value !== '' && history[cursor] !== value) return null;
    let next = event.key === 'ArrowUp'
        ? (event.shiftKey ? 0 : Math.max(0, cursor - 1))
        : (event.shiftKey ? history.length - 1 : cursor + 1);
    next = Math.min(history.length, next);
    if (event.key === 'ArrowDown' && cursor >= history.length && !event.shiftKey) return null;
    event.preventDefault();
    return { cursor: next, value: history[next] || '' };
}
