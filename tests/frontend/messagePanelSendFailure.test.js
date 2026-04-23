/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJson: jest.fn(),
    postJson: jest.fn(),
    postJsonDetailed: jest.fn(),
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

function buildSendError({ status = 403, payload = null, message = null } = {}) {
    const err = new Error(message || payload?.error || `HTTP ${status}`);
    err.status = status;
    err.payload = payload;
    return err;
}

describe('message panel send failure recovery', () => {
    beforeEach(() => {
        renderFixture();
    });

    afterEach(() => {
        jest.resetModules();
        jest.clearAllMocks();
        delete global.fetch;
    });

    test('new-message send failure keeps the draft inline and retries with the single shared organisation', async () => {
        const { getJson, postJson, postJsonDetailed } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { showToast } = require('../../src/frontend/web/von_interface/static/js/utils/toast.js');
        const { initializeMessagePanel, showMessagesTab } = require(modulePath);

        let threadFetchCount = 0;
        postJson.mockResolvedValue({});
        postJsonDetailed
            .mockRejectedValueOnce(
                buildSendError({
                    payload: {
                        error: 'Sender/recipient must share the current organisation',
                        common_organisation_options: [
                            {
                                concept_id: '#V#org_shared',
                                name: 'Shared Lab',
                                role: 'member',
                            },
                        ],
                    },
                }),
            )
            .mockResolvedValueOnce({
                data: { success: true, message_id: '#V#message_sent' },
                status: 201,
                headers: { get: () => null },
            });
        getJson.mockImplementation(async (url) => {
            if (url === '/api/messages/threads?limit=20') {
                threadFetchCount += 1;
                return threadFetchCount === 1
                    ? { threads: [] }
                    : {
                        threads: [{
                            _id: ['#V#user_bob'],
                            last_message: {
                                concept_data: {
                                    content_fallback: 'Recovered send',
                                },
                            },
                            message_count: 1,
                        }],
                    };
            }
            if (url === '/api/messages/unread/count') {
                return { unread_count: 0 };
            }
            if (url === '/api/messages/conversation/%23V%23user_bob?limit=50') {
                return { messages: [] };
            }
            throw new Error(`Unexpected getJson call: ${url}`);
        });

        initializeMessagePanel();
        await showMessagesTab();

        document.getElementById('newMessageBtn').click();
        document.getElementById('newMessageRecipient').value = 'user_bob';
        document.getElementById('newMessageContent').value = 'Please review the draft.';

        document.getElementById('sendNewMessage').click();
        await flushUi();
        await flushUi();

        expect(document.getElementById('newMessageFailure').textContent).toContain(
            'Sender/recipient must share the current organisation',
        );
        expect(document.getElementById('newMessageRecipient').value).toBe('user_bob');
        expect(document.getElementById('newMessageContent').value).toBe('Please review the draft.');

        const recoverySelect = document.getElementById('newMessageRecoverySelect');
        expect(recoverySelect).not.toBeNull();
        expect(recoverySelect.value).toBe('#V#org_shared');
        expect(Array.from(recoverySelect.options).map((option) => option.textContent)).toEqual([
            'Shared Lab (member)',
        ]);
        expect(showToast).not.toHaveBeenCalledWith('Failed to send message', 'error');

        document.getElementById('sendNewMessage').click();
        await flushUi();
        await flushUi();

        expect(postJsonDetailed).toHaveBeenNthCalledWith(2, '/api/messages/', {
            recipient_ids: ['#V#user_bob'],
            content: 'Please review the draft.',
            organisation_concept_id: '#V#org_shared',
        });
        expect(showToast).toHaveBeenCalledWith('Message sent', 'success');
        expect(document.getElementById('newMessageModal').classList.contains('hidden')).toBe(true);
    });

    test('new-message send failure shows a pulldown when multiple shared organisations are available', async () => {
        const { getJson, postJson, postJsonDetailed } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { showToast } = require('../../src/frontend/web/von_interface/static/js/utils/toast.js');
        const { initializeMessagePanel, showMessagesTab } = require(modulePath);

        postJson.mockResolvedValue({});
        postJsonDetailed.mockRejectedValueOnce(
            buildSendError({
                payload: {
                    error: 'Sender/recipient must share the current organisation',
                    common_organisation_options: [
                        {
                            concept_id: '#V#org_alpha',
                            name: 'Alpha Lab',
                            role: 'member',
                        },
                        {
                            concept_id: '#V#org_beta',
                            name: 'Beta Lab',
                            role: 'admin',
                        },
                    ],
                },
            }),
        );
        getJson.mockImplementation(async (url) => {
            if (url === '/api/messages/threads?limit=20') {
                return { threads: [] };
            }
            if (url === '/api/messages/unread/count') {
                return { unread_count: 0 };
            }
            throw new Error(`Unexpected getJson call: ${url}`);
        });

        initializeMessagePanel();
        await showMessagesTab();

        document.getElementById('newMessageBtn').click();
        document.getElementById('newMessageRecipient').value = 'user_bob';
        document.getElementById('newMessageContent').value = 'Need the other organisation context.';

        document.getElementById('sendNewMessage').click();
        await flushUi();
        await flushUi();

        const recoverySelect = document.getElementById('newMessageRecoverySelect');
        expect(document.getElementById('newMessageFailure').textContent).toContain(
            'Sender/recipient must share the current organisation',
        );
        expect(recoverySelect.value).toBe('');
        expect(Array.from(recoverySelect.options).map((option) => option.textContent)).toEqual([
            'Choose an organisation',
            'Alpha Lab (member)',
            'Beta Lab (admin)',
        ]);
        expect(document.getElementById('newMessageRecoveryNote').textContent).toContain(
            'Choose a shared organisation and send again.',
        );
        expect(showToast).not.toHaveBeenCalledWith('Failed to send message', 'error');
    });

    test('reply send failure shows the backend reason inline and preserves the draft', async () => {
        const { getJson, postJson, postJsonDetailed } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { showToast } = require('../../src/frontend/web/von_interface/static/js/utils/toast.js');
        const { initializeMessagePanel, showMessagesTab } = require(modulePath);

        postJson.mockResolvedValue({});
        postJsonDetailed.mockRejectedValueOnce(new Error('No organisation context'));
        getJson.mockImplementation(async (url) => {
            if (url === '/api/messages/threads?limit=20') {
                return {
                    threads: [{
                        _id: ['#V#user_bob'],
                        last_message: {
                            concept_data: {
                                content_fallback: 'Earlier message',
                            },
                        },
                        message_count: 1,
                    }],
                };
            }
            if (url === '/api/messages/unread/count') {
                return { unread_count: 0 };
            }
            if (url === '/api/messages/conversation/%23V%23user_bob?limit=50') {
                return {
                    messages: [{
                        concept_id: '#V#message_1',
                        relationships: {
                            '#V#has_sender': ['#V#user_bob'],
                        },
                        concept_data: {
                            content_fallback: 'Earlier message',
                        },
                        created_at: '2026-04-22T19:30:00Z',
                    }],
                };
            }
            throw new Error(`Unexpected getJson call: ${url}`);
        });

        initializeMessagePanel();
        await showMessagesTab();
        await flushUi();

        const replyInput = document.getElementById('messageInput');
        replyInput.value = 'Need a reply from the correct organisation.';
        document.getElementById('sendMessageBtn').click();
        await flushUi();
        await flushUi();

        expect(document.getElementById('messageComposeFailure').textContent).toContain(
            'No organisation context',
        );
        expect(replyInput.value).toBe('Need a reply from the correct organisation.');
        expect(document.getElementById('messageComposeRecovery').classList.contains('hidden')).toBe(true);
        expect(showToast).not.toHaveBeenCalledWith('Failed to send message', 'error');
    });
});
