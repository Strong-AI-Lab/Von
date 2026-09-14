// Both conversation sources keep one menu and the same non-selecting gestures.
const rowMenus = new WeakMap();
const guidance = 'Right-click, Control-click, or press Shift+F10 or the Context Menu key for conversation options. On touch, select a conversation and use Selected conversation options in the list.';

export function bindConversationRowMenu(row, showMenu) {
    const open = (event, returnFocus = row) => {
        event.preventDefault();
        event.stopPropagation();
        const rect = returnFocus.getBoundingClientRect();
        const pointer = (event.type === 'contextmenu' || event.type === 'click')
            && (event.clientX || event.clientY);
        showMenu(pointer ? event.clientX : rect.left, pointer ? event.clientY : rect.bottom,
            { returnFocus, focusFirst: true });
    };
    rowMenus.set(row, open);
    row.setAttribute('aria-haspopup', 'menu');
    row.setAttribute('aria-expanded', 'false');
    row.setAttribute('aria-keyshortcuts', 'Shift+F10');
    row.setAttribute('aria-description', guidance);
    row.title += `\n${guidance}`;
    row.setAttribute('data-keep-title', 'true');
    row.addEventListener('contextmenu', open);
    // Capture before the row's ordinary selection handler, including on macOS
    // browsers which dispatch a click as well as the native contextmenu event.
    row.addEventListener('click', event => {
        if (!event.ctrlKey) return;
        event.stopImmediatePropagation();
        open(event);
    }, true);
    row.addEventListener('keydown', event => {
        if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) open(event);
    });
}

export function createSelectedConversationOptions(container) {
    const selected = container.querySelector('[data-session-id].is-active');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'selected-conversation-options';
    button.textContent = 'Selected conversation options';
    button.setAttribute('aria-haspopup', 'menu');
    button.setAttribute('aria-expanded', 'false');
    button.disabled = !rowMenus.has(selected);
    button.title = selected
        ? `Options for ${selected.dataset.sessionName || selected.getAttribute('aria-label')}. ${guidance}`
        : 'Select a conversation to access its options. Rows also support right-click, Control-click and Shift+F10.';
    button.addEventListener('click', event => rowMenus.get(selected)?.(event, button));
    return button;
}
