import {
    clampConversationTrayWidth,
    CONVERSATION_TRAY_COLLAPSED_KEY,
    CONVERSATION_TRAY_WIDTH_KEY,
    saveConversationLayoutPreference
} from '../utils/conversationLayoutPreferences.js';

/** Adjust navigation chrome only: never remount or reload the conversation. */
export function createConversationTray(workspace) {
    const tray = workspace.querySelector('.chat-session-tabs-row');
    const navigation = workspace.querySelector('.chat-conversation-nav') || tray;
    const toggle = workspace.querySelector('#conversationTrayToggle');
    const resize = workspace.querySelector('#conversationTrayResize');
    const tabs = workspace.querySelector('#chatSessionTabs');
    let preferences = { width: 260, collapsed: false, expandOnHover: true };
    let peek = false;
    let enterTimer;
    let leaveTimer;
    let drag = null;
    let presentation;
    const scrollPositions = new Map();
    const cleanup = [];
    const listen = (target, event, callback) => {
        if (!target) return;
        target.addEventListener(event, callback);
        cleanup.push(() => target.removeEventListener(event, callback));
    };
    const isVertical = () => workspace.dataset.effectiveTabsLayout === 'vertical';
    const isMenuOpen = () => !!document.querySelector('.chat-session-menu.open');
    const maxWidth = () => Math.max(220, Math.min(400, (workspace.clientWidth || window.innerWidth) - 420));
    const effectiveWidth = () => Math.min(preferences.width, maxWidth());
    const clearTimers = () => { clearTimeout(enterTimer); clearTimeout(leaveTimer); };

    function render() {
        const collapsed = isVertical() && preferences.collapsed;
        if (!collapsed) peek = false;
        const nextPresentation = !isVertical() ? 'horizontal' : collapsed && !peek ? 'rail' : 'expanded';
        const presentationChanged = presentation !== nextPresentation;
        if (tabs && presentationChanged && presentation) {
            scrollPositions.set(presentation, { top: tabs.scrollTop, left: tabs.scrollLeft });
        }
        presentation = nextPresentation;
        workspace.dataset.trayCollapsed = String(collapsed);
        workspace.dataset.trayPeek = String(collapsed && peek);
        workspace.style.setProperty('--conversation-tray-width', `${effectiveWidth()}px`);
        const position = scrollPositions.get(presentation);
        if (tabs && presentationChanged && position) {
            tabs.scrollTop = position.top;
            tabs.scrollLeft = position.left;
        }
        if (toggle) {
            const label = collapsed ? (peek ? 'Keep conversation list open' : 'Open conversation list') : 'Collapse conversation list';
            toggle.setAttribute('aria-label', label);
            toggle.title = label;
            toggle.setAttribute('aria-expanded', String(!collapsed || peek));
        }
        if (resize) {
            resize.setAttribute('aria-valuenow', String(effectiveWidth()));
            resize.setAttribute('aria-valuemax', String(maxWidth()));
        }
    }

    function setCollapsed(value) {
        clearTimers();
        preferences.collapsed = value;
        peek = false;
        render();
        saveConversationLayoutPreference(CONVERSATION_TRAY_COLLAPSED_KEY, value);
    }

    function closePeek() {
        if (!peek || drag || isMenuOpen()
            || (document.activeElement !== toggle && navigation?.contains(document.activeElement))) return;
        peek = false;
        render();
    }

    listen(toggle, 'click', () => setCollapsed(!preferences.collapsed));
    listen(navigation, 'pointerenter', (event) => {
        clearTimers();
        if (event.pointerType === 'touch' || !preferences.expandOnHover || !preferences.collapsed || !isVertical()) return;
        enterTimer = setTimeout(() => { peek = true; render(); }, 200);
    });
    listen(navigation, 'pointerleave', () => {
        clearTimers();
        leaveTimer = setTimeout(closePeek, 250);
    });
    listen(navigation, 'focusin', (event) => {
        clearTimers();
        // Focusing the collapse button itself must not immediately reopen it.
        if (event.target !== toggle && preferences.collapsed && isVertical()) {
            peek = true;
            render();
        }
    });
    listen(navigation, 'focusout', (event) => {
        if (!navigation.contains(event.relatedTarget)) leaveTimer = setTimeout(closePeek, 0);
    });
    listen(document, 'pointerdown', (event) => {
        if (peek && !navigation?.contains(event.target) && !event.target.closest?.('.chat-session-menu')) {
            clearTimers();
            peek = false;
            render();
        }
    });
    listen(document, 'keydown', (event) => {
        if (event.key === 'Escape' && isVertical() && !isMenuOpen()
            && (peek || (!preferences.collapsed && navigation?.contains(event.target)))) {
            event.preventDefault();
            clearTimers();
            if (peek) {
                peek = false;
                render();
            } else {
                setCollapsed(true);
            }
            toggle?.focus({ preventScroll: true });
        }
    });

    // Roving focus follows the actual layout. Enter/Space retain native activation.
    listen(tabs, 'keydown', (event) => {
        if (!event.target.matches?.('[role="tab"]')) return;
        const backwards = isVertical() ? 'ArrowUp' : 'ArrowLeft';
        const forwards = isVertical() ? 'ArrowDown' : 'ArrowRight';
        if (![backwards, forwards, 'Home', 'End'].includes(event.key)) return;
        const rows = Array.from(tabs.querySelectorAll('[role="tab"]'));
        const index = rows.indexOf(event.target);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1
            : (index + (event.key === forwards ? 1 : -1) + rows.length) % rows.length;
        event.preventDefault();
        rows[next]?.focus({ preventScroll: true });
        rows[next]?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
    });

    function finishDrag(cancelled = false) {
        if (!drag) return;
        const start = drag;
        drag = null;
        if (resize?.hasPointerCapture?.(start.pointerId)) resize.releasePointerCapture(start.pointerId);
        if (cancelled) preferences.width = start.savedWidth;
        workspace.classList.remove('is-resizing-conversation-tray');
        render();
        if (!cancelled) saveConversationLayoutPreference(CONVERSATION_TRAY_WIDTH_KEY, preferences.width);
    }

    listen(resize, 'pointerdown', (event) => {
        if (event.button !== 0 || !isVertical()) return;
        event.preventDefault();
        drag = { x: event.clientX, width: effectiveWidth(), savedWidth: preferences.width, pointerId: event.pointerId };
        resize.setPointerCapture?.(event.pointerId);
        workspace.classList.add('is-resizing-conversation-tray');
    });
    listen(resize, 'pointermove', (event) => {
        if (!drag) return;
        preferences.width = Math.min(maxWidth(), clampConversationTrayWidth(drag.width + event.clientX - drag.x));
        render();
    });
    listen(resize, 'pointerup', () => finishDrag());
    listen(resize, 'pointercancel', () => finishDrag(true));
    listen(resize, 'lostpointercapture', () => finishDrag(true));
    listen(resize, 'keydown', (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        preferences.width = event.key === 'Home' ? 220 : event.key === 'End' ? maxWidth()
            : Math.min(maxWidth(), clampConversationTrayWidth(effectiveWidth() + (event.key === 'ArrowRight' ? 10 : -10)));
        render();
        saveConversationLayoutPreference(CONVERSATION_TRAY_WIDTH_KEY, preferences.width);
    });
    listen(window, 'resize', render);
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(render) : null;
    observer?.observe(workspace);

    return {
        workspace,
        refresh(next) {
            const wasCollapsed = preferences.collapsed;
            preferences = { ...next };
            if (!isVertical() || wasCollapsed !== preferences.collapsed || !preferences.expandOnHover) {
                clearTimers();
                peek = false;
            }
            render();
        },
        destroy() {
            clearTimers();
            finishDrag(true);
            observer?.disconnect();
            cleanup.forEach(fn => fn());
        }
    };
}
