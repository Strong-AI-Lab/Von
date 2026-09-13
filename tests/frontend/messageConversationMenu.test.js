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
test('touch action and keyboard context key open the shared menu without selecting the conversation', () => {
    const openMenu = jest.fn();
    const el = renderMessageConversationRow(row, { openMenu }); document.body.append(el);
    el.querySelector('.conversation-menu-trigger').click();
    expect(openMenu).toHaveBeenCalledTimes(1);
    expect(require(base + 'components/messagePanel.js').openMessageExchange).not.toHaveBeenCalled();
    el.dispatchEvent(new KeyboardEvent('keydown', { key: 'F10', shiftKey: true, bubbles: true }));
    expect(openMenu).toHaveBeenCalledTimes(2);
    expect(openMenu.mock.calls[0][3]).toMatchObject({ focusFirst: true });
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
