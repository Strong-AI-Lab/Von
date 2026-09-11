/** @jest-environment jsdom */
jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({ getJson: jest.fn(), postJson: jest.fn(), postJsonDetailed: jest.fn() }));
jest.mock('../../src/frontend/web/von_interface/static/js/chatTab.js', () => ({ openChatSessionMenu: jest.fn() }));
jest.mock('../../src/frontend/web/von_interface/static/js/components/taskPanel.js', () => ({ showTaskPanel: jest.fn() }));
jest.mock('../../src/frontend/web/von_interface/static/js/utils/selectConceptByIdHandler.js', () => ({ hydrateConceptCartouchesInRoot: jest.fn() }));
jest.mock('../../src/frontend/web/von_interface/static/js/utils/copyJsonButtonState.js', () => ({ copyTextWithClipboardFallback: jest.fn(async () => true) }));
const base = '../../src/frontend/web/von_interface/static/js/';
const flush = () => new Promise(resolve => setTimeout(resolve, 0));

beforeEach(async () => {
    jest.resetModules();
    document.body.innerHTML = '<div id="messagesContainer"></div>';
    window.scrollTo = jest.fn();
    const { getJson } = require(base + 'apiService.js');
    getJson.mockImplementation(async url => {
        if (url.includes('/threads')) return { current_user_id: '#V#michael', threads: [{ _id: ['#V#codex_dgx'], message_count: 1 }] };
        if (url.includes('/conversation/')) return { current_user_id: '#V#michael', messages: [{ concept_id: '#V#message_one', concept_data: { content_fallback: 'Finished.' } }] };
        return {};
    });
    const panel = require(base + 'components/messagePanel.js');
    panel.initializeMessagePanel();
    await panel.showMessagesTab();
});
afterEach(() => jest.clearAllMocks());

test.each(['contextmenu', 'ctrlclick', 'keyboard'])('%s offers a copyable real message-stream reference', async gesture => {
    const item = document.querySelector('.message-thread-item');
    if (gesture === 'keyboard') item.dispatchEvent(new KeyboardEvent('keydown', { key: 'F10', shiftKey: true, bubbles: true }));
    else item.dispatchEvent(new MouseEvent(gesture === 'ctrlclick' ? 'click' : 'contextmenu', { ctrlKey: gesture === 'ctrlclick', bubbles: true, cancelable: true }));
    await flush();
    const { openChatSessionMenu } = require(base + 'chatTab.js');
    const items = openChatSessionMenu.mock.calls.at(-1)[2];
    expect(items[0].label).toBe('Copy conversation reference');
    await items[0].onClick();
    const { copyTextWithClipboardFallback } = require(base + 'utils/copyJsonButtonState.js');
    const copied = JSON.parse(copyTextWithClipboardFallback.mock.calls.at(-1)[0]);
    expect(copied.message_stream_ref.participant_concept_ids).toEqual(['#V#codex_dgx', '#V#michael']);
    expect(copied.message_lookup).toMatchObject({ method: 'message_list_direct', other_user_concept_id: '#V#codex_dgx' });
    expect(copied.visible_message_ids).toEqual(['#V#message_one']);
    expect(copied).not.toHaveProperty('conversation_ref');
    expect(copied).not.toHaveProperty('chat_history_lookup');
});

test('Messages task button uses the existing task panel with represented task links', async () => {
    document.querySelector('#messageViewContent').insertAdjacentHTML('beforeend', '<span data-task-concept="true" data-full-concept-id="#V#task_one"></span>');
    document.getElementById('messageTaskPanelBtn').click();
    await flush();
    expect(require(base + 'components/taskPanel.js').showTaskPanel).toHaveBeenCalledWith({ taskIds: ['#V#task_one'], sessionId: null });
});
