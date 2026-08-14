/**
 * Bring the saved-conversation list back into view without changing the
 * selected conversation or reloading its transcript.
 */
export function scrollConversationToSessionList(options = {}) {
    const tabs = document.getElementById('chatSessionTabs');
    const target = tabs?.closest?.('.chat-session-tabs-row') || tabs || document.getElementById('chatTab');
    if (!(target instanceof HTMLElement)) {
        return false;
    }

    const smooth = options?.smooth !== false;
    if (typeof target.scrollIntoView === 'function') {
        target.scrollIntoView({
            behavior: smooth ? 'smooth' : 'auto',
            block: 'start',
            inline: 'nearest'
        });
        return true;
    }

    if (typeof window !== 'undefined' && typeof window.scrollTo === 'function') {
        window.scrollTo({ top: 0, behavior: smooth ? 'smooth' : 'auto' });
        return true;
    }

    return false;
}
