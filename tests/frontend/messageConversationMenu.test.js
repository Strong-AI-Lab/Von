/** @jest-environment jsdom */
jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({ getJson: jest.fn(), postJson: jest.fn() }));
jest.mock('../../src/frontend/web/von_interface/static/js/components/messagePanel.js', () => ({ openMessageExchange: jest.fn() }));
jest.mock('../../src/frontend/web/von_interface/static/js/components/participantProfile.js', () => ({ participantAvatar: () => global.document.createElement('span'), openParticipantProfile: jest.fn() }));
jest.mock('../../src/frontend/web/von_interface/static/js/utils/copyJsonButtonState.js', () => ({ copyTextWithClipboardFallback: jest.fn(async () => true) }));
jest.mock('../../src/frontend/web/von_interface/static/js/utils/toast.js', () => ({ showToast: jest.fn() }));
const base = '../../src/frontend/web/von_interface/static/js/';
const { renderMessageConversationRow, buildMessageConversationMenuItems } = require(base + 'components/conversationCatalogue.js');
const { postJson } = require(base + 'apiService.js');
const { copyTextWithClipboardFallback: copy } = require(base + 'utils/copyJsonButtonState.js');
const row = { session_id: 'messages:exact', viewer_id: '#V#alice', participant_ids: ['#V#alice', '#V#bob'], other_participant_ids: ['#V#bob'], organisation_concept_id: '#V#lab', session_name: 'Bob', last_message_at: '2026-09-13' };
beforeEach(() => { document.body.innerHTML = ''; jest.clearAllMocks(); });
test.each(['contextmenu', 'ctrlclick', 'F10', 'ContextMenu'])('%s opens row options without selecting the conversation', gesture => {
    const openMenu = jest.fn();
    const el = renderMessageConversationRow(row, { openMenu }); document.body.append(el);
    expect(el.querySelector('.conversation-menu-trigger')).toBeNull();
    expect(el.textContent).not.toContain('⋯');
    expect(el.getAttribute('aria-description')).toContain('Shift+F10');
    const event = ['F10', 'ContextMenu'].includes(gesture)
        ? new KeyboardEvent('keydown', { key: gesture, shiftKey: gesture === 'F10', bubbles: true, cancelable: true })
        : new MouseEvent(gesture === 'ctrlclick' ? 'click' : 'contextmenu', { ctrlKey: gesture === 'ctrlclick', bubbles: true, cancelable: true });
    el.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(openMenu).toHaveBeenCalledTimes(1);
    expect(require(base + 'components/messagePanel.js').openMessageExchange).not.toHaveBeenCalled();
    expect(openMenu.mock.calls[0][3]).toMatchObject({ focusFirst: true, returnFocus: el });
    expect(openMenu.mock.calls[0][2].map(item => item.label)).toEqual(expect.arrayContaining(['Open conversation', 'Copy Concept ID', 'Copy conversation reference', 'Profile: #V#bob']));
});
test('shared touch control opens the selected row menu and ordinary row activation still selects', () => {
    const openMenu = jest.fn();
    const el = renderMessageConversationRow(row, { openMenu, selected: true }); document.body.append(el);
    const { mountCatalogueControls } = require(base + 'components/conversationCatalogue.js');
    mountCatalogueControls(document.body);
    const control = document.querySelector('.selected-conversation-options');
    control.click();
    expect(openMenu.mock.calls[0][3].returnFocus).toBe(control);
    const { openMessageExchange } = require(base + 'components/messagePanel.js');
    expect(openMessageExchange).not.toHaveBeenCalled();
    el.click();
    expect(openMessageExchange).toHaveBeenCalledWith(row);
});
test('copies only the server-verified Concept ID and preserves exact group/organisation reference', async () => {
    const group = { ...row, participant_ids: [...row.participant_ids, '#V#carol'] };
    const items = buildMessageConversationMenuItems(group, { pinned: true, hidden: true, togglePin: jest.fn(), hide: jest.fn() });
    postJson.mockResolvedValue({ success: true, concept_id: '#V#message_exchange_reference_verified' });
    await items.find(i => i.label === 'Copy Concept ID').onClick();
    expect(postJson).toHaveBeenCalledWith('/api/messages/exchange/reference', { participant_ids: group.participant_ids, organisation_concept_id: '#V#lab' });
    expect(copy).toHaveBeenLastCalledWith('#V#message_exchange_reference_verified');
    await items.find(i => i.label === 'Copy conversation reference').onClick();
    expect(JSON.parse(copy.mock.calls[1][0]).message_lookup).toEqual({ method: 'POST /api/messages/exchange', participant_ids: group.participant_ids, organisation_concept_id: '#V#lab' });
    expect(items.map(i => i.label)).toEqual(expect.arrayContaining(['Unpin conversation', 'Unhide conversation', 'Profile: #V#carol']));
});
test('failed materialisation never copies a fabricated identifier', async () => {
    postJson.mockRejectedValue(new Error('Unavailable'));
    await buildMessageConversationMenuItems(row).find(i => i.label === 'Copy Concept ID').onClick();
    expect(copy).not.toHaveBeenCalled();
    expect(require(base + 'utils/toast.js').showToast).toHaveBeenCalledWith(expect.stringContaining('unavailable'), 'error');
});
