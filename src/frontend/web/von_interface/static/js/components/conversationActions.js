/** Native disclosures use ordinary Tab navigation, not partial ARIA menu semantics. */
export function initialiseConversationActions(root = document) {
    root.querySelectorAll('details.conversation-actions').forEach(disclosure => {
        if (disclosure.dataset.bound) return;
        disclosure.dataset.bound = 'true';
        const summary = disclosure.querySelector('summary');
        disclosure.addEventListener('keydown', event => {
            if (event.key === 'Escape' && disclosure.open) {
                event.preventDefault();
                disclosure.open = false;
                summary.focus();
            }
        });
        disclosure.addEventListener('click', event => {
            if (!event.target.closest('button')) return;
            // Let the action run first, preserving focus when it opens a dialog.
            queueMicrotask(() => {
                const restoreFocus = disclosure.contains(document.activeElement);
                disclosure.open = false;
                if (restoreFocus) summary.focus();
            });
        });
    });
}
