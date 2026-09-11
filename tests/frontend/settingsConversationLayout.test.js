/** @jest-environment jsdom */
import { setupConversationHistorySettingsSection } from '../../src/frontend/web/von_interface/static/js/settingsPage.js';
import {
    __testOnly_loadChatSessionTabsLayoutPreference as loadLayout,
    __testOnly_setChatSessionTabsLayout as setLayout,
} from '../../src/frontend/web/von_interface/static/js/chatTab.js';
import { CONVERSATION_LAYOUT_KEY, CONVERSATION_TRAY_HOVER_KEY } from '../../src/frontend/web/von_interface/static/js/utils/conversationLayoutPreferences.js';
const fs = require('fs');
const path = require('path');
const settingsTemplate = fs.readFileSync(path.resolve(__dirname, '../../src/frontend/web/von_interface/templates/settings_tab.html'), 'utf8');

beforeEach(() => {
    localStorage.clear();
    window.matchMedia = jest.fn(() => ({ matches: false }));
    document.body.innerHTML = `<div id="chatTab"><div id="conversationWorkspace"><div class="chat-session-tabs-row"><div id="chatSessionTabs"></div></div><textarea id="draft">Unsent text</textarea></div></div>`;
    const template = document.createElement('template');
    template.innerHTML = settingsTemplate;
    document.body.append(template.content.querySelector('#conversation-history-settings'));
    setupConversationHistorySettingsSection();
    loadLayout();
});
afterEach(() => jest.restoreAllMocks());

test('new desktop defaults left; Settings switches immediately without remounting the draft', () => {
    const workspace = document.getElementById('conversationWorkspace');
    const draft = document.getElementById('draft');
    const select = document.getElementById('settingsConversationLayoutSelect');
    expect(workspace.dataset.effectiveTabsLayout).toBe('vertical');
    expect(select.value).toBe('vertical');
    select.value = 'horizontal';
    select.dispatchEvent(new Event('change'));
    expect(workspace.dataset.effectiveTabsLayout).toBe('horizontal');
    expect(localStorage.getItem(CONVERSATION_LAYOUT_KEY)).toBe('horizontal');
    expect(document.getElementById('draft')).toBe(draft);
    expect(draft.value).toBe('Unsent text');
    expect(document.getElementById('settingsConversationTrayHoverToggle').disabled).toBe(true);
    setLayout('vertical');
    expect(select.value).toBe('vertical');
    const hover = document.getElementById('settingsConversationTrayHoverToggle');
    hover.checked = false;
    hover.dispatchEvent(new Event('change'));
    expect(localStorage.getItem(CONVERSATION_TRAY_HOVER_KEY)).toBe('false');
});

test('narrow/coarse layout and cross-window changes keep the explicit preference truthful', () => {
    window.matchMedia = jest.fn(() => ({ matches: true }));
    expect(loadLayout()).toEqual({ preferred: 'vertical', effective: 'horizontal' });
    localStorage.setItem(CONVERSATION_LAYOUT_KEY, 'horizontal');
    window.dispatchEvent(new StorageEvent('storage', { key: CONVERSATION_LAYOUT_KEY, newValue: 'horizontal' }));
    expect(document.getElementById('settingsConversationLayoutSelect').value).toBe('horizontal');
    window.matchMedia = jest.fn(() => ({ matches: false }));
    expect(loadLayout()).toEqual({ preferred: 'horizontal', effective: 'horizontal' });
});
