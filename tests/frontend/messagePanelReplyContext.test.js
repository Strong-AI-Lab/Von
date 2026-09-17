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

describe('reply with selected message context', () => {
    let api;
    let panel;
    const input = () => document.querySelector('#messageInput');
    const select = () => document.querySelector('.message-reply-context-btn').click();
    beforeEach(async () => {
        localStorage.clear(); sessionStorage.clear();
        renderFixture(); window.scrollTo = jest.fn();
        api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        panel = require(modulePath);
        setMessageSessionScope({ actorId: '#V#user_alice', organisationId: '#V#lab' });
        mockReplyReads(api.getJson, { threadIdByRecipient: { '#V#user_bob': '#V#thread_1' } });
        api.postJson.mockResolvedValue({});
        api.postJsonDetailed.mockResolvedValue({ data: { success: true }, status: 201 });
        global.fetch = jest.fn(async () => ({ ok: true, json: async () => ({}), text: async () => '{}' }));
        panel.initializeMessagePanel();
        await panel.showMessagesTab(); await flushUi();
    });
    afterEach(() => { jest.resetModules(); jest.clearAllMocks(); delete global.fetch; });

    test('selects visibly in the current composer and cancels without losing the draft', () => {
        input().value = 'My answer';
        const discuss = jest.fn(); document.addEventListener('von:discussConcept', discuss);
        select();
        expect(input().value).toContain('> Earlier message');
        expect(input().value).toContain('#V#message_for_user_bob');
        expect(document.activeElement).toBe(input());
        expect(document.querySelector('.message-thread-item.selected').dataset.userId).toBe('#V#user_bob');
        expect(discuss).not.toHaveBeenCalled();
        document.querySelector('.message-reply-context-controls button').click();
        expect(input().value).toBe('My answer');
        document.removeEventListener('von:discussConcept', discuss);
    });

    test('reselecting replaces the quote and edited quotes are never silently removed', () => {
        input().value = 'My answer'; select(); select();
        expect(input().value.match(/Replying to/g)).toHaveLength(1);
        input().value = input().value.replace('Earlier', 'Edited');
        const edited = input().value;
        document.querySelector('.message-reply-context-controls button').click();
        expect(input().value).toBe(edited);
        expect(require('../../src/frontend/web/von_interface/static/js/utils/toast.js').showToast)
            .toHaveBeenCalledWith(expect.stringContaining('edited'), 'info');
    });

    test('sends and renders the quote through the existing thread and keeps it on failure', async () => {
        input().value = 'My answer'; select();
        const draft = input().value;
        api.postJsonDetailed.mockRejectedValueOnce(new Error('Offline'));
        document.querySelector('#sendMessageBtn').click(); await flushUi();
        expect(input().value).toBe(draft);
        api.getJson.mockImplementation(async url => {
            if (url.includes('/conversation/')) return { current_user_id: '#V#user_alice', messages: [{
                concept_id: '#V#sent', concept_data: { content_fallback: draft },
                relationships: { '#V#has_sender': ['#V#user_alice'] }
            }] };
            return { threads: [], unread_count: 0 };
        });
        document.querySelector('#sendMessageBtn').click(); await flushUi();
        expect(api.postJsonDetailed).toHaveBeenLastCalledWith('/api/messages/', expect.objectContaining({
            content: draft, recipient_ids: ['#V#user_bob'], organisation_concept_id: '#V#lab'
        }));
        expect(document.querySelector('.message-content').textContent).toContain('Earlier message');
        expect(document.querySelector('.message-content').textContent).toContain('My answer');
        expect(input().value).toBe('');
    });

    test('changes source, retains a deleted source snapshot, and stays in the selected exchange', async () => {
        let messages = [
            { concept_id: '#V#first', concept_data: { content_fallback: 'First source' } },
            { concept_id: '#V#second', concept_data: {} }
        ];
        api.postJson.mockImplementation(async () => ({ messages, current_user_id: '#V#user_alice' }));
        const row = { session_id: 'existing-exchange', viewer_id: '#V#user_alice',
            session_name: 'Bob', participant_ids: ['#V#user_alice', '#V#user_bob'],
            other_participant_ids: ['#V#user_bob'], organisation_concept_id: '#V#original_lab' };
        await panel.openMessageExchange(row);
        input().value = 'Answer'; select();
        document.querySelectorAll('.message-reply-context-btn')[1].click();
        expect(input().value).not.toContain('First source');
        expect(input().value).toContain('#V#second');
        expect(input().value).toContain('[Message text unavailable]');
        const draft = input().value;
        messages = [];
        document.body.insertAdjacentHTML('beforeend', '<div id="conversationWorkspace" class="show-message-exchange"></div>');
        await panel.refreshOpenMessageExchange();
        expect(input().value).toBe(draft);
        document.querySelector('#sendMessageBtn').click(); await flushUi();
        expect(api.postJsonDetailed).toHaveBeenLastCalledWith('/api/messages/', expect.objectContaining({
            content: draft, recipient_ids: ['#V#user_bob'], organisation_concept_id: '#V#original_lab'
        }));
        expect(api.postJson).toHaveBeenLastCalledWith('/api/messages/exchange', expect.objectContaining({
            participant_ids: row.participant_ids, organisation_concept_id: '#V#original_lab'
        }));
    });

    test('unavailable selection keeps the draft', () => {
        input().value = 'Keep this';
        document.querySelector('.message-reply-context-btn').dataset.messageId = '#V#deleted';
        select();
        expect(input().value).toBe('Keep this');
        expect(require('../../src/frontend/web/von_interface/static/js/utils/toast.js').showToast)
            .toHaveBeenCalledWith(expect.stringContaining('no longer available'), 'warning');
    });
});
