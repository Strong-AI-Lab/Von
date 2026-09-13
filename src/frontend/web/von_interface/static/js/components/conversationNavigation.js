// Navigation does not acknowledge content. Each adapter owns its scroll surface
// and canonical read receipts.
export function createLatestMessageButton(onClick) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'chat-scroll-to-end-btn';
    button.title = 'Scroll to latest message';
    button.setAttribute('aria-label', button.title);
    button.innerHTML = '<span aria-hidden="true">↓</span>';
    button.addEventListener('click', onClick);
    setNavigationVisible(button, false);
    return button;
}

export function setNavigationVisible(button, visible) {
    button.classList.toggle('visible', visible);
    button.setAttribute('aria-hidden', String(!visible));
    button.tabIndex = visible ? 0 : -1;
}

export function focusConversationTarget(target) {
    if (!target) return;
    target.tabIndex = -1;
    target.focus({ preventScroll: true });
}
