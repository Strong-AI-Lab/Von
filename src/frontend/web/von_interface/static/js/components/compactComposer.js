// Presentation only: retain the existing controls, listeners and draft owners.
export const COMPACT_COMPOSER_QUERY = '(max-width: 800px), (max-width: 1024px) and (pointer: coarse)';
export const isCompactComposer = () => window.matchMedia?.(COMPACT_COMPOSER_QUERY).matches === true;

export function shouldSubmitComposerKey(event) {
    return event.key === 'Enter' && !event.shiftKey && !event.isComposing
        && event.keyCode !== 229 && !isCompactComposer();
}

export function resizeCompactDraft(input) {
    if (!input) return;
    input.dataset.hasDraft = String(Boolean(input.value.trim()));
    if (!isCompactComposer()) {
        if (input.dataset.compactHeight === 'true') {
            input.style.removeProperty('height');
            input.style.removeProperty('overflow-y');
            delete input.dataset.compactHeight;
        }
        return;
    }
    input.dataset.compactHeight = 'true';
    // Zero the previous/rows-based height so a cleared multiline draft shrinks.
    input.style.height = '0px';
    const viewportHeight = window.visualViewport?.height || window.innerHeight;
    const maximum = Math.min(144, Math.max(44, viewportHeight * 0.3));
    const height = Math.min(maximum, Math.max(44, input.scrollHeight));
    input.style.height = `${height}px`;
    input.style.overflowY = input.scrollHeight > height ? 'auto' : 'hidden';
}

export function initialiseCompactChatComposer(composer, resizeDraft = resizeCompactDraft) {
    if (!composer || composer.dataset.compactBound) return;
    composer.dataset.compactBound = 'true';
    const menu = composer.querySelector('.chat-composer-more-actions');
    const panel = menu?.querySelector('.button-row');
    if (!panel) return;
    const summary = menu.querySelector('summary');
    const label = summary?.querySelector('.composer-more-label');
    let closeWatcher;
    function dismiss(restoreFocus = false) {
        if (!menu.open) return;
        const focusWasInPanel = panel.contains(document.activeElement);
        menu.open = false;
        if (restoreFocus || focusWasInPanel) summary?.focus();
        updateDisclosure();
    }
    function updateDisclosure() {
        const name = menu.open ? 'Close actions' : 'More actions';
        if (label) label.textContent = name;
        if (summary) summary.title = name;
        // Use the browser's close-request stack (including Android Back where
        // supported), without adding synthetic entries to conversation history.
        if (menu.open && isCompactComposer() && menu.isConnected) {
            if (!closeWatcher && typeof window.CloseWatcher === 'function') {
                closeWatcher = new window.CloseWatcher();
                closeWatcher.addEventListener('close', () => dismiss(true));
            }
        } else {
            closeWatcher?.destroy();
            closeWatcher = undefined;
        }
    }
    menu.addEventListener('toggle', updateDisclosure);
    // Wait for the completed click: collapsing during pointer/focus events can
    // move Send before touch activation reaches it.
    document.addEventListener('click', event => {
        if (menu.isConnected && isCompactComposer() && !menu.contains(event.target)) dismiss();
    });
    // Keyboard activation also dismisses before the existing submission handler.
    composer.querySelector('#sendButton')?.addEventListener('click', () => {
        if (isCompactComposer()) dismiss();
    }, { capture: true });
    const active = document.createElement('div');
    active.className = 'compact-composer-active';
    composer.append(active);
    const input = composer.querySelector('#promptInput');
    const mic = composer.querySelector('#dictateButton');
    const voice = composer.querySelector('#voiceConversationButton');
    const status = composer.querySelector('#dictationStatus');
    const voiceStatus = composer.querySelector('#voiceConversationStatus');
    const cancel = composer.querySelector('#cancelDictationButton');
    const retry = composer.querySelector('#retryDictationButton');
    const homes = new Map([mic, voice, status, voiceStatus, cancel, retry].filter(Boolean).map(node => {
        const home = document.createComment(`desktop ${node.id}`);
        node.before(home);
        return [node, home];
    }));
    function place(node, parent) {
        if (!node || node.parentElement === parent) return;
        const focused = document.activeElement === node;
        parent.append(node);
        if (focused) node.focus();
    }
    function update() {
        const compact = isCompactComposer();
        if (compact) {
            if (mic?.classList.contains('active-dictation') || voice?.getAttribute('aria-pressed') === 'true') menu.open = false;
            place(mic, mic?.classList.contains('active-dictation') ? active : panel);
            place(voice, voice?.getAttribute('aria-pressed') === 'true' ? active : panel);
            place(cancel, active);
            place(retry, active);
            place(status, status?.dataset.passive === 'true' ? panel : composer);
            place(voiceStatus, voiceStatus?.dataset.passive === 'true' ? panel : composer);
        } else {
            for (const [node, home] of homes) {
                if (node.parentNode !== home.parentNode) {
                    const focused = document.activeElement === node;
                    home.after(node);
                    if (focused) node.focus();
                }
            }
        }
        updateDisclosure();
        resizeDraft(input);
    }
    const observer = new MutationObserver(update);
    if (mic) observer.observe(mic, { attributes: true, attributeFilter: ['class'] });
    if (voice) observer.observe(voice, { attributes: true, attributeFilter: ['aria-pressed'] });
    for (const note of [status, voiceStatus]) {
        if (note) observer.observe(note, { attributes: true, attributeFilter: ['data-passive'] });
    }
    menu.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
            event.preventDefault();
            dismiss(true);
        }
    });
    menu.addEventListener('click', event => {
        if (isCompactComposer() && event.target.closest('#dictateButton, #voiceConversationButton, #uploadFileButton')) {
            // Recording controls become visible on the controller's next render.
            dismiss(true);
        }
    });
    window.matchMedia?.(COMPACT_COMPOSER_QUERY).addEventListener?.('change', update);
    window.addEventListener('resize', update);
    window.visualViewport?.addEventListener('resize', update);
    update();
}
