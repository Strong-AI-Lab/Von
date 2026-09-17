import { initialiseCompactChatComposer } from './compactComposer.js';
import { resizeConversationDraft } from './conversationDraft.js';
// Shared presentation for the controls supported by every conversation carrier.
export const SEND_ICON = '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5m-7 7 7-7 7 7" /></svg>';
const IMAGE_ICON = '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1"/><path d="m3 17 6-6 4 4 3-3 5 5"/></svg>';
const MICROPHONE_ICON = '<svg class="microphone-icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8" /></svg><svg class="dictation-stop-icon" aria-hidden="true" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>';

export function styleDraftButton(button, kind, label) {
    if (!button) return;
    button.classList.add('btn', kind === 'send' ? 'btn-primary' : 'btn-secondary', 'composer-icon-button');
    button.type = 'button';
    button.classList.add(`conversation-${kind}-button`);
    button.innerHTML = ({ send: SEND_ICON, attachment: IMAGE_ICON, dictation: MICROPHONE_ICON })[kind];
    const name = document.createElement('span');
    name.className = 'button-label sr-only';
    name.textContent = label;
    button.append(name);
    button.setAttribute('aria-label', label);
    button.title = label;
}

export function initialiseDraftControls({ sendButton, attachmentButton, dictateButton }) {
    styleDraftButton(sendButton, 'send', 'Send message');
    styleDraftButton(attachmentButton, 'attachment', attachmentButton?.getAttribute('aria-label') || 'Attach image');
    styleDraftButton(dictateButton, 'dictation', 'Dictate');
}

export function createMessageDraftControls(root, input, sendButton) {
    const actions = document.createElement('div');
    actions.className = 'chat-composer-actions';
    const primary = document.createElement('div');
    primary.className = 'chat-composer-primary-actions';
    const button = document.createElement('button');
    const attachmentButton = root.querySelector('.message-attachment-button');
    primary.append(sendButton);
    if (attachmentButton) primary.append(attachmentButton);
    primary.append(button);
    const menu = document.createElement('details');
    menu.className = 'chat-composer-more-actions';
    menu.innerHTML = `<summary title="More actions"><span class="composer-more-label">More actions</span><span class="composer-more-icon" aria-hidden="true">⋯</span><span class="composer-add-icon" aria-hidden="true">+</span></summary>
        <div class="button-row composer-options-panel"><section class="composer-options-section">
        <h3>Voice &amp; dictation</h3><label>Dictation <select>
        <option value="streaming">Live transcription · context-aware</option>
        <option value="recorded" selected>Recorded audio · context-aware</option>
        <option value="browser">Browser recognition · limited support</option></select></label>
        <p class="compact-speech-help">Von dictation sends audio and draft context to OpenAI. Von does not save the audio. Browser recognition uses your browser’s speech service.</p>
        </section></div>`;
    const status = document.createElement('p');
    status.className = 'speech-settings-note'; status.role = 'status';
    status.setAttribute('aria-live', 'polite');
    const cancelButton = document.createElement('button');
    cancelButton.type = 'button'; cancelButton.className = 'btn btn-secondary';
    cancelButton.textContent = 'Cancel dictation'; cancelButton.hidden = true;
    const retryButton = cancelButton.cloneNode(true); retryButton.textContent = 'Retry recording';
    primary.append(cancelButton, retryButton);
    actions.append(primary, menu);
    const row = input.closest('.message-compose-row, .message-attachment-input-row');
    const shell = document.createElement('div');
    shell.className = 'chat-composer message-draft-shell';
    row.before(shell);
    input.classList.add('conversation-draft-input');
    shell.append(input, actions, status);
    row.remove();
    initialiseDraftControls({ sendButton, attachmentButton, dictateButton: button });
    button.classList.add('conversation-dictate-button');
    cancelButton.dataset.draftRecovery = 'true'; retryButton.dataset.draftRecovery = 'true';
    initialiseCompactChatComposer(shell, resizeConversationDraft, {
        input, send: sendButton, mic: button, status, cancel: cancelButton, retry: retryButton
    });
    return { button, status, cancelButton, retryButton, engineSelect: menu.querySelector('select') };
}
