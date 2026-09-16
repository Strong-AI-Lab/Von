/** @jest-environment jsdom */
jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJson: jest.fn(), postJson: jest.fn(), postJsonDetailed: jest.fn(), getWindowSessionId: () => 'fixture', WINDOW_SESSION_HEADER: 'X-Von-Window-Session'
}));
jest.mock('../../src/frontend/web/von_interface/static/js/dictation.js', () => ({ createDictationController: jest.fn() }));

const { createDictationController } = require('../../src/frontend/web/von_interface/static/js/dictation.js');
const { getJson, postJsonDetailed } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
const { showMessagesTab, resetMessagePanelContext, initializeMessagePanel } = require('../../src/frontend/web/von_interface/static/js/components/messagePanel.js');
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
let controllers;

beforeEach(async () => {
    document.body.innerHTML = '<div id="messagesContainer"></div>';
    sessionStorage.clear();
    controllers = [];
    createDictationController.mockImplementation(options => {
        const controller = { options, pending: false, hasPendingInput: () => controller.pending,
            finish: jest.fn(async () => true), cancel: jest.fn(), dispose: jest.fn() };
        controllers.push(controller); return controller;
    });
    getJson.mockImplementation(async url => {
        if (url.includes('/threads')) return { threads: [] };
        return { unread_count: 0, messages: [], current_user_id: '#V#alice' };
    });
    postJsonDetailed.mockReset();
    postJsonDetailed.mockResolvedValue({ data: { success: true } });
    initializeMessagePanel();
    await showMessagesTab();
    document.getElementById('newMessageBtn').click();
    document.getElementById('newMessageRecipient').value = '#V#bob';
});

test('Send waits for dictation and sends the resulting draft once', async () => {
    const controller = controllers[1];
    controller.pending = true;
    let finish;
    controller.finish.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    document.getElementById('sendNewMessage').click();
    document.getElementById('sendNewMessage').click();
    expect(postJsonDetailed).not.toHaveBeenCalled();
    expect(controller.finish).toHaveBeenCalledTimes(1);
    controller.options.setValue('Dictated #V\u200b#research');
    controller.pending = false; finish(true);
    await tick();
    expect(postJsonDetailed).toHaveBeenCalledTimes(1);
    expect(postJsonDetailed.mock.calls[0][1]).toMatchObject({ content: 'Dictated #V#research', recipient_ids: ['#V#bob'] });
});

test('failed dictation retains draft and does not send', async () => {
    const controller = controllers[1];
    controller.pending = true; controller.finish.mockResolvedValue(false);
    document.getElementById('newMessageContent').value = 'Retain this';
    document.getElementById('sendNewMessage').click();
    await tick();
    expect(postJsonDetailed).not.toHaveBeenCalled();
    expect(document.getElementById('newMessageContent').value).toBe('Retain this');
});

test('changing recipient invalidates capture context; resetting cancels capture', () => {
    const controller = controllers[1];
    const before = controller.options.getContext().key;
    document.getElementById('newMessageRecipient').value = '#V#charlie';
    expect(controller.options.getContext().key).not.toBe(before);
    resetMessagePanelContext();
    for (const entry of controllers) expect(entry.cancel).toHaveBeenCalled();
});
