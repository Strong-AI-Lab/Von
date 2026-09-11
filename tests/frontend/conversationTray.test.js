/** @jest-environment jsdom */
import { createConversationTray } from '../../src/frontend/web/von_interface/static/js/components/conversationTray.js';
import {
    loadConversationLayoutPreferences, CONVERSATION_LAYOUT_KEY,
    CONVERSATION_TRAY_WIDTH_KEY, CONVERSATION_TRAY_COLLAPSED_KEY,
} from '../../src/frontend/web/von_interface/static/js/utils/conversationLayoutPreferences.js';

const fs = require('fs');
const path = require('path');
const template = fs.readFileSync(path.resolve(__dirname, '../../src/frontend/web/von_interface/templates/chat_tab.html'), 'utf8');
let controller, workspace, toggle, navigation, tabs, grip;
const preferences = (overrides = {}) => ({ ...loadConversationLayoutPreferences(), ...overrides });
const pointer = (element, type, props = {}) => {
    const event = new MouseEvent(type, { bubbles: true, button: 0, ...props });
    Object.defineProperty(event, 'pointerId', { value: 7 });
    element.dispatchEvent(event);
};

beforeEach(() => {
    localStorage.clear();
    jest.useFakeTimers();
    document.body.innerHTML = template;
    workspace = document.getElementById('conversationWorkspace');
    workspace.dataset.effectiveTabsLayout = 'vertical';
    Object.defineProperty(workspace, 'clientWidth', { configurable: true, value: 1100 });
    toggle = document.getElementById('conversationTrayToggle');
    navigation = workspace.querySelector('.chat-conversation-nav');
    tabs = document.getElementById('chatSessionTabs');
    tabs.innerHTML = '<button role="tab" aria-selected="true">One</button><button role="tab">Two</button>';
    grip = document.getElementById('conversationTrayResize');
    controller = createConversationTray(workspace);
    controller.refresh(preferences());
});
afterEach(() => {
    controller.destroy();
    jest.clearAllTimers();
    jest.useRealTimers();
    jest.restoreAllMocks();
});

test('defaults to left, honours explicit horizontal, and bounds persisted geometry', () => {
    expect(loadConversationLayoutPreferences()).toEqual({ layout: 'vertical', width: 260, collapsed: false, expandOnHover: true });
    localStorage.setItem(CONVERSATION_LAYOUT_KEY, 'horizontal');
    localStorage.setItem(CONVERSATION_TRAY_WIDTH_KEY, '9999');
    expect(loadConversationLayoutPreferences()).toMatchObject({ layout: 'horizontal', width: 400 });
    expect(loadConversationLayoutPreferences(() => { throw new Error('Storage unavailable'); })).toMatchObject({ layout: 'vertical', width: 260 });
});

test('collapse and expansion retain the transcript, draft, attachments and list scroll', () => {
    const draft = document.getElementById('promptInput');
    draft.value = 'Keep this unsent draft';
    draft.setSelectionRange(5, 9);
    const transcript = document.getElementById('scrollableField');
    transcript.innerHTML = '<p>Existing response</p>';
    const response = transcript.firstChild;
    const attachment = document.createElement('img');
    draft.after(attachment);
    tabs.scrollTop = 80;
    toggle.click();
    expect(workspace.dataset.trayCollapsed).toBe('true');
    expect(localStorage.getItem(CONVERSATION_TRAY_COLLAPSED_KEY)).toBe('true');
    // A shorter rail can clamp the browser scroll position; expansion restores it.
    tabs.scrollTop = 0;
    toggle.click();
    expect(workspace.dataset.trayCollapsed).toBe('false');
    expect(transcript.firstChild).toBe(response);
    expect(draft.value).toBe('Keep this unsent draft');
    expect(draft.selectionStart).toBe(5);
    expect(attachment.isConnected).toBe(true);
    expect(tabs.scrollTop).toBe(80);
});

test('hover peeks temporarily and leaving closes without changing the collapsed preference', () => {
    toggle.click();
    toggle.focus();
    pointer(navigation, 'pointerenter');
    jest.advanceTimersByTime(201);
    expect(workspace.dataset.trayPeek).toBe('true');
    pointer(navigation, 'pointerleave');
    jest.advanceTimersByTime(251);
    expect(workspace.dataset.trayPeek).toBe('false');
    expect(localStorage.getItem(CONVERSATION_TRAY_COLLAPSED_KEY)).toBe('true');
});

test('keyboard focus reveals titles even with hover disabled; Escape returns focus', () => {
    controller.refresh(preferences({ collapsed: true, expandOnHover: false }));
    pointer(navigation, 'pointerenter');
    jest.advanceTimersByTime(300);
    expect(workspace.dataset.trayPeek).toBe('false');
    tabs.firstChild.focus();
    expect(workspace.dataset.trayPeek).toBe('true');
    tabs.firstChild.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
    expect(document.activeElement).toBe(tabs.lastChild);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(document.activeElement).toBe(toggle);
    expect(workspace.dataset.trayPeek).toBe('false');
});

test('temporary expansion keeps a conversation context menu usable', () => {
    controller.refresh(preferences({ collapsed: true }));
    pointer(navigation, 'pointerenter');
    jest.advanceTimersByTime(201);
    const menu = document.createElement('div');
    menu.className = 'chat-session-menu open';
    document.body.append(menu);
    pointer(navigation, 'pointerleave');
    jest.advanceTimersByTime(251);
    expect(workspace.dataset.trayPeek).toBe('true');
    pointer(document.body, 'pointerdown');
    expect(workspace.dataset.trayPeek).toBe('false');
});

test('resizing persists on release, clamps to available width, and cancellation restores width', () => {
    pointer(grip, 'pointerdown', { clientX: 260 });
    pointer(grip, 'pointermove', { clientX: 320 });
    expect(workspace.style.getPropertyValue('--conversation-tray-width')).toBe('320px');
    expect(localStorage.getItem(CONVERSATION_TRAY_WIDTH_KEY)).toBeNull();
    pointer(grip, 'pointerup');
    expect(localStorage.getItem(CONVERSATION_TRAY_WIDTH_KEY)).toBe('320');
    pointer(grip, 'pointerdown', { clientX: 320 });
    pointer(grip, 'pointermove', { clientX: 380 });
    pointer(grip, 'pointercancel');
    expect(workspace.style.getPropertyValue('--conversation-tray-width')).toBe('320px');
    Object.defineProperty(workspace, 'clientWidth', { configurable: true, value: 700 });
    window.dispatchEvent(new Event('resize'));
    expect(workspace.style.getPropertyValue('--conversation-tray-width')).toBe('280px');
    expect(localStorage.getItem(CONVERSATION_TRAY_WIDTH_KEY)).toBe('320');
});

test('keyboard resize works and horizontal fallback does not erase desktop collapse', () => {
    grip.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    expect(localStorage.getItem(CONVERSATION_TRAY_WIDTH_KEY)).toBe('270');
    controller.refresh(preferences({ collapsed: true }));
    workspace.dataset.effectiveTabsLayout = 'horizontal';
    controller.refresh(preferences({ collapsed: true }));
    pointer(navigation, 'pointerenter');
    jest.advanceTimersByTime(300);
    expect(workspace.dataset.trayCollapsed).toBe('false');
    expect(workspace.dataset.trayPeek).toBe('false');
    workspace.dataset.effectiveTabsLayout = 'vertical';
    controller.refresh(preferences({ collapsed: true }));
    expect(workspace.dataset.trayCollapsed).toBe('true');
});
