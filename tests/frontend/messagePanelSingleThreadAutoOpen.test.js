/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJson: jest.fn(),
    postJson: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/toast.js', () => ({
    showToast: jest.fn(),
}));

const modulePath = '../../src/frontend/web/von_interface/static/js/components/messagePanel.js';

function renderFixture() {
    document.body.innerHTML = `
        <div id="messagesContainer"></div>
        <span id="unreadMessageBadge" class="hidden"></span>
    `;
}

function flushUi() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

describe('message panel single-thread auto-open', () => {
    beforeEach(() => {
        renderFixture();
    });

    afterEach(() => {
        jest.resetModules();
        jest.clearAllMocks();
        delete global.fetch;
    });

    test('automatically opens the only available conversation', async () => {
        const { getJson, postJson } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { initializeMessagePanel, showMessagesTab } = require(modulePath);

        postJson.mockResolvedValue({});
        getJson.mockImplementation(async (url) => {
            if (url === '/api/messages/threads?limit=20') {
                return {
                    threads: [{
                        _id: ['#V#von_system'],
                        last_message: {
                            concept_data: {
                                content_fallback: 'Hello there',
                            },
                        },
                        message_count: 1,
                    }],
                };
            }
            if (url === '/api/messages/unread/count') {
                return { unread_count: 0 };
            }
            if (url === '/api/messages/conversation/%23V%23von_system?limit=50') {
                return {
                    messages: [{
                        concept_id: '#V#message_1',
                        relationships: {
                            '#V#has_sender': ['#V#von_system'],
                        },
                        concept_data: {
                            content_fallback: 'Hello there',
                        },
                        created_at: '2026-04-14T03:28:00Z',
                    }],
                };
            }
            throw new Error(`Unexpected getJson call: ${url}`);
        });

        global.fetch = jest.fn(async () => ({
            ok: true,
            status: 200,
            text: async () => JSON.stringify({}),
            json: async () => ({}),
        }));

        initializeMessagePanel();
        await showMessagesTab();
        await flushUi();

        expect(document.querySelector('.message-thread-item.selected')?.dataset.userId).toBe('#V#von_system');
        expect(document.getElementById('messageViewHeader')?.classList.contains('hidden')).toBe(false);
        expect(document.getElementById('messageComposeArea')?.classList.contains('hidden')).toBe(false);
        expect(document.getElementById('conversationTitle')?.textContent).toContain('Von System');
        expect(document.getElementById('messageViewContent')?.textContent).toContain('Hello there');
    });

    test('leaves the empty state in place when multiple conversations are available', async () => {
        const { getJson, postJson } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { initializeMessagePanel, showMessagesTab } = require(modulePath);

        postJson.mockResolvedValue({});
        getJson.mockImplementation(async (url) => {
            if (url === '/api/messages/threads?limit=20') {
                return {
                    threads: [
                        {
                            _id: ['#V#von_system'],
                            last_message: {
                                concept_data: {
                                    content_fallback: 'Hello there',
                                },
                            },
                            message_count: 1,
                        },
                        {
                            _id: ['#V#michael_witbrock'],
                            last_message: {
                                concept_data: {
                                    content_fallback: 'Second thread',
                                },
                            },
                            message_count: 2,
                        },
                    ],
                };
            }
            if (url === '/api/messages/unread/count') {
                return { unread_count: 0 };
            }
            throw new Error(`Unexpected getJson call: ${url}`);
        });

        global.fetch = jest.fn(async () => ({
            ok: true,
            status: 200,
            text: async () => JSON.stringify({}),
            json: async () => ({}),
        }));

        initializeMessagePanel();
        await showMessagesTab();
        await flushUi();

        expect(document.querySelector('.message-thread-item.selected')).toBeNull();
        expect(document.getElementById('messageViewHeader')?.classList.contains('hidden')).toBe(true);
        expect(document.getElementById('messageViewContent')?.textContent).toContain('Select a conversation to view messages');
    });
});
