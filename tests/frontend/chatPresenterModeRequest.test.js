/** @jest-environment jsdom */

import { sendMessageToServer } from '../../src/frontend/web/von_interface/static/js/chat.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    postJson: jest.fn(),
    annotateTurn: jest.fn(() => Promise.resolve({}))
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {
        promptInput: { value: 'Hello' },
        loadingIndicator: { style: { display: 'none' } },
        vonIntro: { style: { display: 'block' } },
        conversationId: 'local',
        scrollableField: null
    },
    addMessageToChat: jest.fn(),
    renderSpanSuggestions: jest.fn(),
    getCurrentUserConceptId: jest.fn(() => null)
}));

describe('chat.js presenter mode request', () => {
    test('sendMessageToServer posts with presenter_mode: true', async () => {
        const { postJson } = await import(
            '../../src/frontend/web/von_interface/static/js/apiService.js'
        );

        postJson.mockResolvedValue({ response: 'ok' });

        await sendMessageToServer();

        expect(postJson).toHaveBeenCalledTimes(1);
        expect(postJson).toHaveBeenCalledWith('/von/generate', {
            prompt: 'Hello',
            presenter_mode: true,
            thinking_card_mode: 'default'
        });
    });
});
