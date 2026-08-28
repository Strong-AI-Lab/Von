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
const replyAttemptStorageKey = 'von_message_reply_delivery_attempt_v1';
const newMessageAttemptStorageKey = 'von_message_new_delivery_attempt_v1';

function renderFixture() {
    document.body.innerHTML = `
        <div id="messagesContainer"></div>
        <span id="unreadMessageBadge" class="hidden"></span>
    `;
}

function flushUi() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

function createDeferred() {
    let resolve;
    let reject;
    const promise = new Promise((resolvePromise, rejectPromise) => {
        resolve = resolvePromise;
        reject = rejectPromise;
    });
    return { promise, resolve, reject };
}

function buildSendError({ status = 403, payload = null, message = null } = {}) {
    const err = new Error(message || payload?.error || `HTTP ${status}`);
    err.status = status;
    err.payload = payload;
    return err;
}

function setMessageSessionScope({ actorId, organisationId }) {
    sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: actorId }));
    sessionStorage.setItem('von_current_org', JSON.stringify({ concept_id: organisationId }));
}

function mockReplyReads(getJson, {
    actorId = '#V#user_alice',
    recipientIds = ['#V#user_bob'],
    threadIdByRecipient = {},
} = {}) {
    getJson.mockImplementation(async (url) => {
        if (url === '/api/messages/threads?limit=20') {
            return {
                threads: recipientIds.map((recipientId) => ({
                    _id: [recipientId],
                    last_message: {
                        concept_data: { content_fallback: 'Earlier message' },
                    },
                    message_count: 1,
                })),
            };
        }
        if (url === '/api/messages/unread/count') {
            return { unread_count: 0 };
        }
        const conversationMatch = url.match(
            /^\/api\/messages\/conversation\/(.+)\?limit=50$/u,
        );
        if (conversationMatch) {
            const recipientId = decodeURIComponent(conversationMatch[1]);
            const threadId = threadIdByRecipient[recipientId] || '';
            return {
                messages: [{
                    concept_id: `#V#message_for_${recipientId.replace(/^#V#/u, '')}`,
                    relationships: {
                        '#V#has_sender': [actorId],
                        '#V#has_recipient': [recipientId],
                        ...(threadId ? { '#V#is_part_of_thread': [threadId] } : {}),
                    },
                    concept_data: {
                        content_fallback: 'Earlier message',
                        read_by: [],
                    },
                    created_at: '2026-08-28T00:00:00Z',
                }],
                current_user_id: actorId,
            };
        }
        throw new Error(`Unexpected getJson call: ${url}`);
    });
}

function mockNewMessageReads(getJson) {
    getJson.mockImplementation(async (url) => {
        if (url === '/api/messages/threads?limit=20') {
            return { threads: [] };
        }
        if (url === '/api/messages/unread/count') {
            return { unread_count: 0 };
        }
        if (url === '/api/messages/conversation/%23V%23user_bob?limit=50') {
            return { messages: [], current_user_id: '#V#user_alice' };
        }
        throw new Error(`Unexpected getJson call: ${url}`);
    });
}

describe('message panel send failure recovery', () => {
    beforeEach(() => {
        localStorage.clear();
        sessionStorage.clear();
        window.scrollTo = jest.fn();
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
            delivery_idempotency_key: expect.any(String),
            organisation_concept_id: '#V#org_shared',
        });
        const firstDeliveryKey = postJsonDetailed.mock.calls[0][1].delivery_idempotency_key;
        const retryDeliveryKey = postJsonDetailed.mock.calls[1][1].delivery_idempotency_key;
        expect(retryDeliveryKey).not.toBe(firstDeliveryKey);
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

    test('reply send is single-flight and a same-draft retry reuses its delivery key', async () => {
        const { getJson, postJson, postJsonDetailed } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { initializeMessagePanel, showMessagesTab } = require(modulePath);
        const firstSend = createDeferred();
        const retrySend = createDeferred();

        postJson.mockResolvedValue({});
        postJsonDetailed
            .mockImplementationOnce(() => firstSend.promise)
            .mockImplementationOnce(() => retrySend.promise);
        getJson.mockImplementation(async (url) => {
            if (url === '/api/messages/threads?limit=20') {
                return {
                    threads: [{
                        _id: ['#V#user_bob'],
                        last_message: {
                            concept_data: { content_fallback: 'Earlier message' },
                        },
                        message_count: 1,
                    }],
                };
            }
            if (url === '/api/messages/unread/count') {
                return { unread_count: 0 };
            }
            if (url === '/api/messages/conversation/%23V%23user_bob?limit=50') {
                return { messages: [], current_user_id: '#V#user_alice' };
            }
            throw new Error(`Unexpected getJson call: ${url}`);
        });

        initializeMessagePanel();
        await showMessagesTab();
        await flushUi();

        const replyInput = document.getElementById('messageInput');
        const sendButton = document.getElementById('sendMessageBtn');
        replyInput.value = 'Please review this once.';
        sendButton.click();

        expect(postJsonDetailed).toHaveBeenCalledTimes(1);
        expect(sendButton.disabled).toBe(true);
        expect(sendButton.textContent).toBe('Sending…');
        expect(sendButton.getAttribute('aria-busy')).toBe('true');
        expect(replyInput.value).toBe('Please review this once.');

        sendButton.click();
        replyInput.dispatchEvent(new KeyboardEvent('keydown', {
            key: 'Enter',
            bubbles: true,
            cancelable: true,
        }));
        expect(postJsonDetailed).toHaveBeenCalledTimes(1);

        firstSend.reject(new Error('Temporary delivery failure'));
        await flushUi();
        await flushUi();

        expect(sendButton.disabled).toBe(false);
        expect(sendButton.textContent).toBe('Send');
        expect(sendButton.getAttribute('aria-busy')).toBe('false');
        expect(replyInput.value).toBe('Please review this once.');

        sendButton.click();
        expect(postJsonDetailed).toHaveBeenCalledTimes(2);
        expect(postJsonDetailed.mock.calls[1][1].delivery_idempotency_key).toBe(
            postJsonDetailed.mock.calls[0][1].delivery_idempotency_key,
        );

        retrySend.resolve({
            data: { success: true, message_id: '#V#message_sent' },
            status: 201,
            headers: { get: () => null },
        });
        await flushUi();
        await flushUi();

        expect(replyInput.value).toBe('');
        expect(sendButton.disabled).toBe(false);
        expect(sendButton.textContent).toBe('Send');
    });

    test('new-message retries get new delivery keys after recipient or content changes', async () => {
        const { getJson, postJson, postJsonDetailed } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { initializeMessagePanel, showMessagesTab } = require(modulePath);
        const firstSend = createDeferred();

        postJson.mockResolvedValue({});
        postJsonDetailed
            .mockImplementationOnce(() => firstSend.promise)
            .mockRejectedValueOnce(new Error('Second temporary failure'))
            .mockRejectedValueOnce(new Error('Third temporary failure'));
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

        const recipientInput = document.getElementById('newMessageRecipient');
        const contentInput = document.getElementById('newMessageContent');
        const sendButton = document.getElementById('sendNewMessage');
        recipientInput.value = 'user_bob';
        contentInput.value = 'Original draft';
        sendButton.click();

        expect(postJsonDetailed).toHaveBeenCalledTimes(1);
        expect(sendButton.disabled).toBe(true);
        expect(sendButton.textContent).toBe('Sending…');
        expect(recipientInput.disabled).toBe(true);
        expect(contentInput.disabled).toBe(true);
        sendButton.click();
        expect(postJsonDetailed).toHaveBeenCalledTimes(1);

        firstSend.reject(new Error('First temporary failure'));
        await flushUi();
        await flushUi();
        expect(recipientInput.value).toBe('user_bob');
        expect(contentInput.value).toBe('Original draft');
        expect(recipientInput.disabled).toBe(false);
        expect(contentInput.disabled).toBe(false);
        const firstKey = postJsonDetailed.mock.calls[0][1].delivery_idempotency_key;

        recipientInput.value = 'user_alice';
        recipientInput.dispatchEvent(new Event('input', { bubbles: true }));
        sendButton.click();
        await flushUi();
        await flushUi();
        const recipientChangedKey = postJsonDetailed.mock.calls[1][1].delivery_idempotency_key;
        expect(recipientChangedKey).not.toBe(firstKey);
        expect(recipientInput.value).toBe('user_alice');
        expect(contentInput.value).toBe('Original draft');

        contentInput.value = 'Changed draft';
        sendButton.click();
        await flushUi();
        await flushUi();
        const contentChangedKey = postJsonDetailed.mock.calls[2][1].delivery_idempotency_key;
        expect(contentChangedKey).not.toBe(recipientChangedKey);
        expect(recipientInput.value).toBe('user_alice');
        expect(contentInput.value).toBe('Changed draft');
        expect(sendButton.disabled).toBe(false);
        expect(sendButton.textContent).toBe('Send');
    });

    test('new-message retry restores its exact recipient, draft, and key across render and module reload', async () => {
        setMessageSessionScope({
            actorId: '#V#user_alice',
            organisationId: '#V#org_alpha',
        });
        const firstApi = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        firstApi.postJson.mockResolvedValue({});
        firstApi.postJsonDetailed.mockRejectedValueOnce(
            new Error('Unknown delivery outcome'),
        );
        mockNewMessageReads(firstApi.getJson);

        const firstPanel = require(modulePath);
        firstPanel.initializeMessagePanel();
        await firstPanel.showMessagesTab();
        document.getElementById('newMessageBtn').click();

        const exactRecipient = '  user_bob  ';
        const exactDraft = '  Retry this exact new-message draft.  ';
        document.getElementById('newMessageRecipient').value = exactRecipient;
        document.getElementById('newMessageContent').value = exactDraft;
        document.getElementById('sendNewMessage').click();
        await flushUi();
        await flushUi();

        const originalDeliveryKey = firstApi.postJsonDetailed.mock.calls[0][1]
            .delivery_idempotency_key;
        expect(JSON.parse(sessionStorage.getItem(newMessageAttemptStorageKey))).toMatchObject({
            key: originalDeliveryKey,
            recipient: exactRecipient,
            draft: exactDraft,
            scope: {
                actor_user_id: '#V#user_alice',
                organisation_concept_id: '#V#org_alpha',
            },
            delivery_scope: {
                recipient_ids: ['#V#user_bob'],
            },
        });

        await firstPanel.showMessagesTab();
        expect(document.getElementById('newMessageModal').classList.contains('hidden')).toBe(false);
        expect(document.getElementById('newMessageRecipient').value).toBe(exactRecipient);
        expect(document.getElementById('newMessageContent').value).toBe(exactDraft);

        jest.resetModules();
        jest.clearAllMocks();
        renderFixture();
        const reloadedApi = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        reloadedApi.postJson.mockResolvedValue({});
        reloadedApi.postJsonDetailed.mockResolvedValueOnce({
            data: { success: true, message_id: '#V#message_reconciled' },
            status: 200,
            headers: { get: () => null },
        });
        mockNewMessageReads(reloadedApi.getJson);

        const reloadedPanel = require(modulePath);
        reloadedPanel.initializeMessagePanel();
        await reloadedPanel.showMessagesTab();
        expect(document.getElementById('newMessageModal').classList.contains('hidden')).toBe(false);
        expect(document.getElementById('newMessageRecipient').value).toBe(exactRecipient);
        expect(document.getElementById('newMessageContent').value).toBe(exactDraft);

        document.getElementById('sendNewMessage').click();
        await flushUi();
        await flushUi();

        expect(reloadedApi.postJsonDetailed.mock.calls[0][1]).toMatchObject({
            recipient_ids: ['#V#user_bob'],
            content: 'Retry this exact new-message draft.',
            delivery_idempotency_key: originalDeliveryKey,
        });
        expect(sessionStorage.getItem(newMessageAttemptStorageKey)).toBeNull();
        expect(document.getElementById('newMessageModal').classList.contains('hidden')).toBe(true);
    });

    test.each([
        {
            label: 'actor',
            actorId: '#V#user_eve',
            organisationId: '#V#org_alpha',
        },
        {
            label: 'organisation',
            actorId: '#V#user_alice',
            organisationId: '#V#org_beta',
        },
    ])('new-message retry stays isolated from a changed $label scope', async ({
        actorId,
        organisationId,
    }) => {
        setMessageSessionScope({
            actorId: '#V#user_alice',
            organisationId: '#V#org_alpha',
        });
        const seedApi = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        seedApi.postJson.mockResolvedValue({});
        seedApi.postJsonDetailed.mockRejectedValueOnce(
            new Error('Unknown delivery outcome'),
        );
        mockNewMessageReads(seedApi.getJson);
        const seedPanel = require(modulePath);
        seedPanel.initializeMessagePanel();
        await seedPanel.showMessagesTab();
        document.getElementById('newMessageBtn').click();
        document.getElementById('newMessageRecipient').value = 'user_bob';
        document.getElementById('newMessageContent').value = 'Scope-bound draft';
        document.getElementById('sendNewMessage').click();
        await flushUi();
        await flushUi();

        const originalAttemptRaw = sessionStorage.getItem(newMessageAttemptStorageKey);
        const originalKey = JSON.parse(originalAttemptRaw).key;

        jest.resetModules();
        jest.clearAllMocks();
        renderFixture();
        setMessageSessionScope({ actorId, organisationId });
        sessionStorage.setItem(newMessageAttemptStorageKey, originalAttemptRaw);
        const scopedApi = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        scopedApi.postJson.mockResolvedValue({});
        scopedApi.postJsonDetailed.mockRejectedValueOnce(new Error('Scoped retry failed'));
        mockNewMessageReads(scopedApi.getJson);
        const scopedPanel = require(modulePath);
        scopedPanel.initializeMessagePanel();
        await scopedPanel.showMessagesTab();

        expect(document.getElementById('newMessageModal').classList.contains('hidden')).toBe(true);
        expect(document.getElementById('newMessageRecipient').value).toBe('');
        expect(document.getElementById('newMessageContent').value).toBe('');
        expect(sessionStorage.getItem(newMessageAttemptStorageKey)).toBe(originalAttemptRaw);

        document.getElementById('newMessageBtn').click();
        const recipientInput = document.getElementById('newMessageRecipient');
        const contentInput = document.getElementById('newMessageContent');
        recipientInput.value = 'user_bob';
        recipientInput.dispatchEvent(new Event('input', { bubbles: true }));
        contentInput.value = 'Scope-bound draft';
        contentInput.dispatchEvent(new Event('input', { bubbles: true }));
        document.getElementById('sendNewMessage').click();
        await flushUi();
        await flushUi();

        expect(scopedApi.postJsonDetailed.mock.calls[0][1].delivery_idempotency_key).not.toBe(
            originalKey,
        );
    });

    test('cancelling a restored new-message attempt abandons its persisted key', async () => {
        setMessageSessionScope({
            actorId: '#V#user_alice',
            organisationId: '#V#org_alpha',
        });
        const { getJson, postJson, postJsonDetailed } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        postJson.mockResolvedValue({});
        postJsonDetailed.mockRejectedValueOnce(new Error('Unknown delivery outcome'));
        mockNewMessageReads(getJson);
        const { initializeMessagePanel, showMessagesTab } = require(modulePath);
        initializeMessagePanel();
        await showMessagesTab();
        document.getElementById('newMessageBtn').click();
        document.getElementById('newMessageRecipient').value = 'user_bob';
        document.getElementById('newMessageContent').value = 'Abandon this retry';
        document.getElementById('sendNewMessage').click();
        await flushUi();
        await flushUi();

        expect(sessionStorage.getItem(newMessageAttemptStorageKey)).not.toBeNull();
        document.getElementById('cancelNewMessage').click();
        expect(sessionStorage.getItem(newMessageAttemptStorageKey)).toBeNull();
        expect(document.getElementById('newMessageModal').classList.contains('hidden')).toBe(true);

        await showMessagesTab();
        expect(document.getElementById('newMessageModal').classList.contains('hidden')).toBe(true);
    });

    test('confirmed new-message success clears the persisted attempt', async () => {
        setMessageSessionScope({
            actorId: '#V#user_alice',
            organisationId: '#V#org_alpha',
        });
        const { getJson, postJson, postJsonDetailed } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const send = createDeferred();
        postJson.mockResolvedValue({});
        postJsonDetailed.mockImplementationOnce(() => send.promise);
        mockNewMessageReads(getJson);
        const { initializeMessagePanel, showMessagesTab } = require(modulePath);
        initializeMessagePanel();
        await showMessagesTab();
        document.getElementById('newMessageBtn').click();
        document.getElementById('newMessageRecipient').value = 'user_bob';
        document.getElementById('newMessageContent').value = 'Clear after success';
        document.getElementById('sendNewMessage').click();

        expect(sessionStorage.getItem(newMessageAttemptStorageKey)).not.toBeNull();
        send.resolve({
            data: { success: true, message_id: '#V#message_sent' },
            status: 201,
            headers: { get: () => null },
        });
        await flushUi();
        await flushUi();

        expect(sessionStorage.getItem(newMessageAttemptStorageKey)).toBeNull();
        expect(document.getElementById('newMessageModal').classList.contains('hidden')).toBe(true);
    });

    test('conversation refresh preserves an ordinary unsent reply draft', async () => {
        setMessageSessionScope({
            actorId: '#V#user_alice',
            organisationId: '#V#org_alpha',
        });
        const { getJson, postJson } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        postJson.mockResolvedValue({});
        mockReplyReads(getJson);
        const { initializeMessagePanel, showMessagesTab } = require(modulePath);
        initializeMessagePanel();
        await showMessagesTab();

        const replyInput = document.getElementById('messageInput');
        replyInput.value = 'Do not erase this unsent draft.';
        document.getElementById('refreshMessagesBtn').click();
        await flushUi();
        await flushUi();

        expect(replyInput.value).toBe('Do not erase this unsent draft.');
        expect(sessionStorage.getItem(replyAttemptStorageKey)).toBeNull();
    });

    test('reply retry restores its exact visible draft and delivery key across render and module reload', async () => {
        setMessageSessionScope({
            actorId: '#V#user_alice',
            organisationId: '#V#org_alpha',
        });
        const firstApi = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        firstApi.postJson.mockResolvedValue({});
        firstApi.postJsonDetailed.mockRejectedValueOnce(new Error('Unknown delivery outcome'));
        mockReplyReads(firstApi.getJson, {
            recipientIds: ['#V#user_bob', '#V#user_carol'],
            threadIdByRecipient: {
                '#V#user_bob': '#V#thread_bob',
                '#V#user_carol': '#V#thread_carol',
            },
        });

        const firstPanel = require(modulePath);
        firstPanel.initializeMessagePanel();
        await firstPanel.showMessagesTab();
        document.querySelector(
            '.message-thread-item[data-user-id="#V#user_bob"]',
        ).click();
        await flushUi();
        await flushUi();

        const exactDraft = '  Retry this exact visible draft.  ';
        const firstInput = document.getElementById('messageInput');
        firstInput.value = exactDraft;
        firstInput.dispatchEvent(new Event('input', { bubbles: true }));
        document.getElementById('sendMessageBtn').click();
        await flushUi();
        await flushUi();

        const storedAfterFailure = JSON.parse(
            sessionStorage.getItem(replyAttemptStorageKey),
        );
        const originalDeliveryKey = firstApi.postJsonDetailed.mock.calls[0][1]
            .delivery_idempotency_key;
        expect(storedAfterFailure).toMatchObject({
            key: originalDeliveryKey,
            draft: exactDraft,
            scope: {
                actor_user_id: '#V#user_alice',
                organisation_concept_id: '#V#org_alpha',
                recipient_ids: ['#V#user_bob'],
                thread_id: '#V#thread_bob',
            },
        });

        await firstPanel.showMessagesTab();
        expect(document.querySelector('.message-thread-item.selected')?.dataset.userId).toBe(
            '#V#user_bob',
        );
        expect(document.getElementById('messageInput').value).toBe(exactDraft);

        jest.resetModules();
        jest.clearAllMocks();
        renderFixture();
        const reloadedApi = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        reloadedApi.postJson.mockResolvedValue({});
        reloadedApi.postJsonDetailed.mockResolvedValueOnce({
            data: { success: true, message_id: '#V#message_reconciled' },
            status: 200,
            headers: { get: () => null },
        });
        mockReplyReads(reloadedApi.getJson, {
            recipientIds: ['#V#user_bob', '#V#user_carol'],
            threadIdByRecipient: {
                '#V#user_bob': '#V#thread_bob',
                '#V#user_carol': '#V#thread_carol',
            },
        });

        const reloadedPanel = require(modulePath);
        reloadedPanel.initializeMessagePanel();
        await reloadedPanel.showMessagesTab();
        expect(document.querySelector('.message-thread-item.selected')?.dataset.userId).toBe(
            '#V#user_bob',
        );
        expect(document.getElementById('messageInput').value).toBe(exactDraft);

        document.getElementById('sendMessageBtn').click();
        await flushUi();
        await flushUi();

        expect(reloadedApi.postJsonDetailed.mock.calls[0][1].delivery_idempotency_key).toBe(
            originalDeliveryKey,
        );
        expect(document.getElementById('messageInput').value).toBe('');
        expect(sessionStorage.getItem(replyAttemptStorageKey)).toBeNull();
    });

    test('reply retry does not restore across actor, organisation, recipient, or thread scope changes', async () => {
        const exactDraft = 'Retry only in the original reply scope.';
        setMessageSessionScope({
            actorId: '#V#user_alice',
            organisationId: '#V#org_alpha',
        });
        const seedApi = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        seedApi.postJson.mockResolvedValue({});
        seedApi.postJsonDetailed.mockRejectedValueOnce(new Error('Unknown delivery outcome'));
        mockReplyReads(seedApi.getJson, {
            threadIdByRecipient: { '#V#user_bob': '#V#thread_one' },
        });
        const seedPanel = require(modulePath);
        seedPanel.initializeMessagePanel();
        await seedPanel.showMessagesTab();
        const seedInput = document.getElementById('messageInput');
        seedInput.value = exactDraft;
        seedInput.dispatchEvent(new Event('input', { bubbles: true }));
        document.getElementById('sendMessageBtn').click();
        await flushUi();
        await flushUi();

        const originalAttemptRaw = sessionStorage.getItem(replyAttemptStorageKey);
        const originalAttempt = JSON.parse(originalAttemptRaw);
        const mismatchCases = [
            {
                label: 'actor',
                actorId: '#V#user_eve',
                organisationId: '#V#org_alpha',
                selectedRecipientId: '#V#user_bob',
                recipientIds: ['#V#user_bob'],
                threadId: '#V#thread_one',
            },
            {
                label: 'organisation',
                actorId: '#V#user_alice',
                organisationId: '#V#org_beta',
                selectedRecipientId: '#V#user_bob',
                recipientIds: ['#V#user_bob'],
                threadId: '#V#thread_one',
            },
            {
                label: 'recipient',
                actorId: '#V#user_alice',
                organisationId: '#V#org_alpha',
                selectedRecipientId: '#V#user_carol',
                recipientIds: ['#V#user_bob', '#V#user_carol'],
                threadId: '#V#thread_carol',
            },
            {
                label: 'thread',
                actorId: '#V#user_alice',
                organisationId: '#V#org_alpha',
                selectedRecipientId: '#V#user_bob',
                recipientIds: ['#V#user_bob'],
                threadId: '#V#thread_two',
            },
        ];

        for (const mismatchCase of mismatchCases) {
            jest.resetModules();
            jest.clearAllMocks();
            localStorage.clear();
            sessionStorage.clear();
            renderFixture();
            setMessageSessionScope(mismatchCase);
            sessionStorage.setItem(replyAttemptStorageKey, originalAttemptRaw);

            const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
            api.postJson.mockResolvedValue({});
            api.postJsonDetailed.mockRejectedValueOnce(
                new Error(`${mismatchCase.label} scope retry failed`),
            );
            const threadIdByRecipient = Object.fromEntries(
                mismatchCase.recipientIds.map((recipientId) => [
                    recipientId,
                    recipientId === mismatchCase.selectedRecipientId
                        ? mismatchCase.threadId
                        : '#V#thread_one',
                ]),
            );
            mockReplyReads(api.getJson, {
                actorId: mismatchCase.actorId,
                recipientIds: mismatchCase.recipientIds,
                threadIdByRecipient,
            });

            const panel = require(modulePath);
            panel.initializeMessagePanel();
            await panel.showMessagesTab();
            if (mismatchCase.label === 'recipient') {
                expect(document.getElementById('messageInput').value).toBe(exactDraft);
                document.querySelector(
                    `.message-thread-item[data-user-id="${mismatchCase.selectedRecipientId}"]`,
                ).click();
                await flushUi();
                await flushUi();
            }

            const input = document.getElementById('messageInput');
            expect(input.value).toBe('');
            input.value = exactDraft;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            document.getElementById('sendMessageBtn').click();
            await flushUi();
            await flushUi();

            const replacementKey = api.postJsonDetailed.mock.calls[0][1]
                .delivery_idempotency_key;
            expect(replacementKey).not.toBe(originalAttempt.key);
            const replacementAttempt = JSON.parse(
                sessionStorage.getItem(replyAttemptStorageKey),
            );
            expect(replacementAttempt.key).toBe(replacementKey);
            expect(replacementAttempt.scope).toMatchObject({
                actor_user_id: mismatchCase.actorId,
                organisation_concept_id: mismatchCase.organisationId,
                recipient_ids: [mismatchCase.selectedRecipientId],
                thread_id: mismatchCase.threadId,
            });
        }
    });

    test('successful reply clears the persisted attempt and its visible draft', async () => {
        setMessageSessionScope({
            actorId: '#V#user_alice',
            organisationId: '#V#org_alpha',
        });
        const { getJson, postJson, postJsonDetailed } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const send = createDeferred();
        postJson.mockResolvedValue({});
        postJsonDetailed.mockImplementationOnce(() => send.promise);
        mockReplyReads(getJson, {
            threadIdByRecipient: { '#V#user_bob': '#V#thread_one' },
        });

        const { initializeMessagePanel, showMessagesTab } = require(modulePath);
        initializeMessagePanel();
        await showMessagesTab();

        const input = document.getElementById('messageInput');
        input.value = 'Clear this after confirmed success.';
        input.dispatchEvent(new Event('input', { bubbles: true }));
        document.getElementById('sendMessageBtn').click();

        expect(JSON.parse(sessionStorage.getItem(replyAttemptStorageKey))).toMatchObject({
            draft: 'Clear this after confirmed success.',
            key: postJsonDetailed.mock.calls[0][1].delivery_idempotency_key,
        });

        send.resolve({
            data: { success: true, message_id: '#V#message_sent' },
            status: 201,
            headers: { get: () => null },
        });
        await flushUi();
        await flushUi();

        expect(input.value).toBe('');
        expect(sessionStorage.getItem(replyAttemptStorageKey)).toBeNull();
    });

    test('new-message explicit recovery organisation survives an ambiguous failure and module reload', async () => {
        setMessageSessionScope({
            actorId: '#V#user_alice',
            organisationId: '#V#org_alpha',
        });
        const firstApi = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        firstApi.postJson.mockResolvedValue({});
        firstApi.postJsonDetailed
            .mockRejectedValueOnce(buildSendError({
                payload: {
                    error: 'Use a shared organisation',
                    common_organisation_options: [{
                        concept_id: '#V#org_shared',
                        name: 'Shared Lab',
                        role: 'member',
                    }],
                },
            }))
            .mockRejectedValueOnce(new Error('Delivery receipt unavailable'));
        mockNewMessageReads(firstApi.getJson);
        const firstPanel = require(modulePath);
        firstPanel.initializeMessagePanel();
        await firstPanel.showMessagesTab();
        document.getElementById('newMessageBtn').click();
        document.getElementById('newMessageRecipient').value = 'user_bob';
        document.getElementById('newMessageContent').value = 'Retry through Shared Lab.';

        document.getElementById('sendNewMessage').click();
        await flushUi();
        await flushUi();
        expect(document.getElementById('newMessageRecoverySelect').value).toBe(
            '#V#org_shared',
        );
        const sharedOrganisationKey = JSON.parse(
            sessionStorage.getItem(newMessageAttemptStorageKey),
        ).key;

        document.getElementById('sendNewMessage').click();
        await flushUi();
        await flushUi();
        expect(firstApi.postJsonDetailed.mock.calls[1][1]).toMatchObject({
            organisation_concept_id: '#V#org_shared',
            delivery_idempotency_key: sharedOrganisationKey,
        });
        expect(document.getElementById('newMessageFailure').textContent).toContain(
            'Delivery receipt unavailable',
        );
        expect(document.getElementById('newMessageRecoverySelect').value).toBe(
            '#V#org_shared',
        );
        expect(JSON.parse(sessionStorage.getItem(newMessageAttemptStorageKey))).toMatchObject({
            key: sharedOrganisationKey,
            scope: {
                actor_user_id: '#V#user_alice',
                organisation_concept_id: '#V#org_alpha',
            },
            delivery_scope: {
                organisation_concept_id: '#V#org_shared',
                recipient_ids: ['#V#user_bob'],
            },
            recovery_state: {
                selectedOrganisationConceptId: '#V#org_shared',
                organisationOptions: [{
                    conceptId: '#V#org_shared',
                    name: 'Shared Lab',
                    role: 'member',
                }],
            },
        });

        await firstPanel.showMessagesTab();
        expect(document.getElementById('newMessageModal').classList.contains('hidden')).toBe(false);
        expect(document.getElementById('newMessageRecoverySelect').value).toBe(
            '#V#org_shared',
        );

        jest.resetModules();
        jest.clearAllMocks();
        renderFixture();
        const reloadedApi = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        reloadedApi.postJson.mockResolvedValue({});
        reloadedApi.postJsonDetailed.mockRejectedValueOnce(
            new Error('Still awaiting a canonical receipt'),
        );
        mockNewMessageReads(reloadedApi.getJson);
        const reloadedPanel = require(modulePath);
        reloadedPanel.initializeMessagePanel();
        await reloadedPanel.showMessagesTab();

        expect(document.getElementById('newMessageModal').classList.contains('hidden')).toBe(false);
        expect(document.getElementById('newMessageRecoverySelect').value).toBe(
            '#V#org_shared',
        );
        document.getElementById('sendNewMessage').click();
        await flushUi();
        await flushUi();
        expect(reloadedApi.postJsonDetailed.mock.calls[0][1]).toMatchObject({
            organisation_concept_id: '#V#org_shared',
            delivery_idempotency_key: sharedOrganisationKey,
        });
    });

    test('reply explicit recovery organisation survives an ambiguous failure and module reload', async () => {
        setMessageSessionScope({
            actorId: '#V#user_alice',
            organisationId: '#V#org_alpha',
        });
        const firstApi = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        firstApi.postJson.mockResolvedValue({});
        firstApi.postJsonDetailed
            .mockRejectedValueOnce(buildSendError({
                payload: {
                    error: 'Use a shared organisation',
                    common_organisation_options: [{
                        concept_id: '#V#org_shared',
                        name: 'Shared Lab',
                        role: 'member',
                    }],
                },
            }))
            .mockRejectedValueOnce(new Error('Delivery receipt unavailable'));
        mockReplyReads(firstApi.getJson, {
            threadIdByRecipient: { '#V#user_bob': '#V#thread_bob' },
        });
        const firstPanel = require(modulePath);
        firstPanel.initializeMessagePanel();
        await firstPanel.showMessagesTab();
        const exactDraft = '  Retry this reply through Shared Lab.  ';
        document.getElementById('messageInput').value = exactDraft;

        document.getElementById('sendMessageBtn').click();
        await flushUi();
        await flushUi();
        expect(document.getElementById('messageComposeRecoverySelect').value).toBe(
            '#V#org_shared',
        );
        const sharedOrganisationKey = JSON.parse(
            sessionStorage.getItem(replyAttemptStorageKey),
        ).key;

        document.getElementById('sendMessageBtn').click();
        await flushUi();
        await flushUi();
        expect(firstApi.postJsonDetailed.mock.calls[1][1]).toMatchObject({
            organisation_concept_id: '#V#org_shared',
            delivery_idempotency_key: sharedOrganisationKey,
        });
        expect(document.getElementById('messageComposeFailure').textContent).toContain(
            'Delivery receipt unavailable',
        );
        expect(document.getElementById('messageComposeRecoverySelect').value).toBe(
            '#V#org_shared',
        );
        expect(JSON.parse(sessionStorage.getItem(replyAttemptStorageKey))).toMatchObject({
            key: sharedOrganisationKey,
            draft: exactDraft,
            scope: {
                organisation_concept_id: '#V#org_shared',
                recipient_ids: ['#V#user_bob'],
                thread_id: '#V#thread_bob',
            },
            session_scope: {
                actor_user_id: '#V#user_alice',
                organisation_concept_id: '#V#org_alpha',
            },
            recovery_state: {
                selectedOrganisationConceptId: '#V#org_shared',
            },
        });

        await firstPanel.showMessagesTab();
        expect(document.getElementById('messageInput').value).toBe(exactDraft);
        expect(document.getElementById('messageComposeRecoverySelect').value).toBe(
            '#V#org_shared',
        );

        jest.resetModules();
        jest.clearAllMocks();
        renderFixture();
        const reloadedApi = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        reloadedApi.postJson.mockResolvedValue({});
        reloadedApi.postJsonDetailed.mockRejectedValueOnce(
            new Error('Still awaiting a canonical receipt'),
        );
        mockReplyReads(reloadedApi.getJson, {
            threadIdByRecipient: { '#V#user_bob': '#V#thread_bob' },
        });
        const reloadedPanel = require(modulePath);
        reloadedPanel.initializeMessagePanel();
        await reloadedPanel.showMessagesTab();

        expect(document.getElementById('messageInput').value).toBe(exactDraft);
        expect(document.getElementById('messageComposeRecoverySelect').value).toBe(
            '#V#org_shared',
        );
        document.getElementById('sendMessageBtn').click();
        await flushUi();
        await flushUi();
        expect(reloadedApi.postJsonDetailed.mock.calls[0][1]).toMatchObject({
            organisation_concept_id: '#V#org_shared',
            delivery_idempotency_key: sharedOrganisationKey,
        });
    });

    test('new-message close and cancel fail closed while pending, then success force-closes', async () => {
        setMessageSessionScope({
            actorId: '#V#user_alice',
            organisationId: '#V#org_alpha',
        });
        const { getJson, postJson, postJsonDetailed } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const send = createDeferred();
        postJson.mockResolvedValue({});
        postJsonDetailed.mockImplementationOnce(() => send.promise);
        mockNewMessageReads(getJson);
        const { initializeMessagePanel, showMessagesTab } = require(modulePath);
        initializeMessagePanel();
        await showMessagesTab();
        document.getElementById('newMessageBtn').click();
        document.getElementById('newMessageRecipient').value = 'user_bob';
        document.getElementById('newMessageContent').value = 'Keep pending attempt visible';
        document.getElementById('sendNewMessage').click();

        const closeButton = document.getElementById('closeNewMessageModal');
        const cancelButton = document.getElementById('cancelNewMessage');
        const persistedWhilePending = sessionStorage.getItem(newMessageAttemptStorageKey);
        expect(closeButton.disabled).toBe(true);
        expect(cancelButton.disabled).toBe(true);
        closeButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        cancelButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        expect(document.getElementById('newMessageModal').classList.contains('hidden')).toBe(false);
        expect(document.getElementById('newMessageRecipient').value).toBe('user_bob');
        expect(document.getElementById('newMessageContent').value).toBe(
            'Keep pending attempt visible',
        );
        expect(sessionStorage.getItem(newMessageAttemptStorageKey)).toBe(
            persistedWhilePending,
        );

        send.resolve({
            data: { success: true, message_id: '#V#message_sent' },
            status: 201,
            headers: { get: () => null },
        });
        await flushUi();
        await flushUi();

        expect(sessionStorage.getItem(newMessageAttemptStorageKey)).toBeNull();
        expect(document.getElementById('newMessageModal').classList.contains('hidden')).toBe(true);
    });
});
