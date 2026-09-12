import { resizeCompactDraft, shouldSubmitComposerKey } from './compactComposer.js';
import { initializeConceptAutocomplete } from './conceptAutocomplete.js';
import { simpleMarkdownToHtml } from '../markdownUtils.js';
import { profileButton, participantAvatar } from './participantProfile.js';
/**
 * Message Panel Component (JVNAUTOSCI-1071)
 *
 * Provides UI for viewing and sending direct messages between users.
 * Messages are stored as Vontology concepts and accessed via REST API.
 */

import { getJson, postJson, postJsonDetailed } from '../apiService.js';
import { getSessionScopedOrgId } from '../utils/sessionScopedStorage.js';
import { hydrateConceptCartouchesInRoot } from '../utils/selectConceptByIdHandler.js';
import { createCartoucheFragment } from '../utils/textDecorator.js';
import { showToast } from '../utils/toast.js';
import { buildMessageStreamReference } from '../utils/messageStreamReference.js';
import { copyTextWithClipboardFallback } from '../utils/copyJsonButtonState.js';
import {
    clearRecommendationReviewResults,
    renderRecommendationReviewPayload,
} from './paperRecommendationUi.js';

// Message panel state
let _messagesContainer = null;
let _messageListEl = null;
let _threads = [];
let _currentMessages = [];
let _currentConversationUserId = null;
let _currentUserId = null;
let _isLoading = false;
let _unreadCount = 0;
let _replySendFailureState = null;
let _newMessageSendFailureState = null;
let _replySendPending = false;
let _newMessageSendPending = false;
let _replyDeliveryAttempt = null;
let _newMessageDeliveryAttempt = null;
let _deliveryKeySequence = 0;

let _exchange = null;
let _exchangeGeneration = 0;
let _olderCursor = null;
let _readObserver = null;
const _exchangeDrafts = new Map();
const _pendingReads = new Set();

function exchangeScope(row = _exchange) {
    return row ? `${row.browser_scope ?? getSessionScopedOrgId() ?? ''}:${row.viewer_id}:${row.session_id}` : '';
}

export function resetMessagePanelContext() {
    const input = _messagesContainer?.querySelector('#messageInput');
    if (_exchange && input) _exchangeDrafts.set(exchangeScope(), input.value);
    _exchangeGeneration += 1;
    _exchange = null;
    _currentConversationUserId = null;
    _currentMessages = [];
    _isLoading = false;
    _readObserver?.disconnect();
    _messagesContainer?.replaceChildren();
}

export async function openMessageExchange(row) {
    const oldInput = _messagesContainer?.querySelector('#messageInput');
    if (_exchange && oldInput) _exchangeDrafts.set(exchangeScope(), oldInput.value);
    _exchangeGeneration += 1;
    _isLoading = false;
    _readObserver?.disconnect();
    _exchange = { ...row, browser_scope: getSessionScopedOrgId() };
    resetMessagesViewportPosition();
    _olderCursor = null;
    if (!_messagesContainer) initializeMessagePanel();
    if (!_messagesContainer?.querySelector('#messageViewContent')) renderMessagesTabContent();
    _currentUserId = row.viewer_id;
    const selectedGeneration = _exchangeGeneration;
    await selectConversation(row.other_participant_ids[0] || row.viewer_id);
    if (selectedGeneration !== _exchangeGeneration) return;
    const title = _messagesContainer?.querySelector('#conversationTitle');
    if (title) title.textContent = row.session_name;
    const header = _messagesContainer?.querySelector('#messageViewHeader');
    header?.querySelectorAll('.exchange-profile-button').forEach(el => el.remove());
    for (const id of row.participant_ids) {
        const b = profileButton(id, id === row.viewer_id ? 'Your profile' : 'Participant profile');
        b.className = 'exchange-profile-button'; header?.append(b);
    }
    const input = _messagesContainer?.querySelector('#messageInput');
    if (input) {
        input.value = _exchangeDrafts.get(exchangeScope()) || '';
        resizeCompactDraft(input);
        input.placeholder = `Reply to ${row.session_name}`;
    }
    const compose = _messagesContainer?.querySelector('#messageComposeArea');
    let destination = compose?.querySelector('.message-reply-destination');
    if (compose && !destination) { destination = document.createElement('p'); destination.className = 'message-reply-destination'; compose.prepend(destination); }
    if (destination) destination.textContent = `To: ${row.session_name} · ${row.organisation_concept_id?.replace(/^#V#/, '').replaceAll('_', ' ') || 'Original conversation context'}`;
    // A legacy group exchange can be read without silently dropping recipients.
    if (row.other_participant_ids.length > 1) {
        if (destination) destination.textContent += ' · Group replies are not yet supported.';
        if (input) input.disabled = true;
        const send = compose?.querySelector('#sendMessageBtn'); if (send) send.disabled = true;
    } else {
        if (input) input.disabled = false;
        const send = compose?.querySelector('#sendMessageBtn'); if (send) send.disabled = false;
    }
}

let composerOpenedFromChat = false;

export async function showMessageComposer() {
    if (!_messagesContainer) initializeMessagePanel();
    if (!_messagesContainer?.querySelector('#newMessageModal')) renderMessagesTabContent();
    const workspace = document.getElementById('conversationWorkspace');
    composerOpenedFromChat = !workspace?.classList.contains('show-message-exchange');
    workspace?.classList.add('show-message-exchange');
    document.body.classList.add('viewing-message-exchange');
    showNewMessageModal();
}

export async function refreshOpenMessageExchange() {
    if (!_exchange || document.hidden || !document.getElementById('conversationWorkspace')?.classList.contains('show-message-exchange')) return;
    await loadConversation(_currentConversationUserId, { silent: true });
}

function isUnreadMessage(message) {
    return (message.relationships?.['#V#has_recipient'] || []).includes(_currentUserId)
        && !(message.concept_data?.read_by || []).includes(_currentUserId);
}

function updateUnreadMarkers() {
    _messagesContainer?.querySelectorAll('[data-contribution-id]').forEach(element => {
        const message = _currentMessages.find(item => item.concept_id === element.dataset.contributionId);
        const unread = Boolean(message && isUnreadMessage(message));
        element.classList.toggle('is-unread', unread);
        let label = element.querySelector('.message-unread-label');
        if (unread && !label) {
            label = document.createElement('span');
            label.className = 'message-unread-label';
            label.textContent = 'Unread';
            element.prepend(label);
        } else if (!unread) label?.remove();
    });
}

function observeDisplayedMessages() {
    _readObserver?.disconnect();
    if (typeof IntersectionObserver !== 'function') return;
    const root = _messagesContainer?.querySelector('#messageViewContent');
    const generation = _exchangeGeneration;
    const org = getSessionScopedOrgId();
    const observer = new IntersectionObserver(entries => {
        // Disconnected observers can still deliver entries for the old pane.
        if (observer !== _readObserver || generation !== _exchangeGeneration
            || org !== getSessionScopedOrgId() || document.hidden || !root?.getClientRects().length) return;
        const ids = entries.filter(entry => entry.isIntersecting && entry.intersectionRatio > 0 && entry.target.isConnected)
            .map(entry => entry.target.dataset.contributionId)
            .filter(id => !_pendingReads.has(`${generation}:${id}`) && _currentMessages.some(m => m.concept_id === id && isUnreadMessage(m)));
        if (!ids.length) return;
        ids.forEach(id => _pendingReads.add(`${generation}:${id}`));
        entries.filter(entry => ids.includes(entry.target.dataset.contributionId))
            .forEach(entry => observer.unobserve(entry.target));
        void postJson('/api/messages/read/bulk', { message_ids: ids }).then(async response => {
            if (generation !== _exchangeGeneration || org !== getSessionScopedOrgId()) return;
            if (response?.success !== true) throw new Error('Read state was not saved');
            // A full update receipt confirms every submitted ID, including loaded
            // older pages. Partial receipts require read-back; never clear all IDs.
            if (response.updated_count === ids.length) {
                _currentMessages.forEach(message => {
                    if (!ids.includes(message.concept_id)) return;
                    message.concept_data ||= {};
                    message.concept_data.read_by = [...new Set([...(message.concept_data.read_by || []), _currentUserId])];
                });
                updateUnreadMarkers();
            } else await loadConversation(_currentConversationUserId, { silent: true, observeReads: false });
            if (generation !== _exchangeGeneration) return;
            if (_currentMessages.some(m => ids.includes(m.concept_id) && isUnreadMessage(m))) showReadFailure();
            else {
                root.querySelector('.message-read-status')?.remove();
                document.dispatchEvent(new CustomEvent('von:conversation-contribution'));
            }
        }).catch(() => {
            if (generation === _exchangeGeneration) showReadFailure();
        }).finally(() => ids.forEach(id => _pendingReads.delete(`${generation}:${id}`)));
    }, { root, threshold: 0.01 });
    _readObserver = observer;
    root?.querySelectorAll('.is-unread[data-contribution-id]').forEach(el => observer.observe(el));
}

function showReadFailure() {
    const root = _messagesContainer?.querySelector('#messageViewContent');
    if (!root || root.querySelector('.message-read-status')) return;
    const status = document.createElement('div');
    status.className = 'message-read-status';
    status.setAttribute('role', 'status');
    status.textContent = 'Could not save read state. Unread labels are retained. ';
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.textContent = 'Retry';
    retry.onclick = () => observeDisplayedMessages();
    status.append(retry);
    root.prepend(status);
}

const COMPOSE_SCOPE_REPLY = 'reply';
const COMPOSE_SCOPE_NEW_MESSAGE = 'newMessage';
const REPLY_DELIVERY_ATTEMPT_STORAGE_KEY = 'von_message_reply_delivery_attempt_v1';
const REPLY_DELIVERY_ATTEMPT_SCHEMA_VERSION = 'direct_message_reply_delivery_attempt.v2';
const NEW_MESSAGE_DELIVERY_ATTEMPT_STORAGE_KEY = 'von_message_new_delivery_attempt_v1';
const NEW_MESSAGE_DELIVERY_ATTEMPT_SCHEMA_VERSION = 'direct_message_new_delivery_attempt.v2';
const MESSAGE_THREAD_PREDICATE_ID = '#V#is_part_of_thread';

const MESSAGE_PANEL_ICONS = Object.freeze({
    chat: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M21 12a8 8 0 0 1-8 8H7l-4 3v-6.2A7.8 7.8 0 0 1 5 4.8 8 8 0 0 1 13 4a8 8 0 0 1 8 8Z"/><path d="M8 10h8"/><path d="M8 14h5"/></svg>',
    compose: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 5v14"/><path d="M5 12h14"/></svg>',
    mail: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="m4 7 8 6 8-6"/></svg>',
    refresh: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M20 6v5h-5"/><path d="M4 18v-5h5"/><path d="M18.2 9A7 7 0 0 0 6.3 7.6L4 10"/><path d="M5.8 15A7 7 0 0 0 17.7 16.4L20 14"/></svg>',
});

function renderMessagePanelIcon(name) {
    return MESSAGE_PANEL_ICONS[name] || '';
}

/**
 * Initialize the messages panel.
 * Call this once on page load to set up the panel element.
 */
export function initializeMessagePanel() {
    _messagesContainer = document.getElementById('messagesContainer');

    if (!_messagesContainer) {
        console.warn('[messagePanel] Messages container not found in DOM');
        return;
    }

    console.log('[messagePanel] Initialized');
}

/**
 * Show the messages panel in the dedicated tab.
 */
export async function showMessagesTab() {
    if (!_messagesContainer) {
        _messagesContainer = document.getElementById('messagesContainer');
    }

    if (!_messagesContainer) {
        console.warn('[messagePanel] Messages container not found');
        return;
    }

    // Render the messages panel UI
    renderMessagesTabContent();
    resetMessagesViewportPosition();

    // Load messages/threads
    await loadMessageThreads();
    await loadUnreadCount();
}

function resetMessagesViewportPosition() {
    try {
        if (typeof window !== 'undefined' && typeof window.scrollTo === 'function') {
            window.scrollTo(0, 0);
        }
    } catch (_) {
        // Some test/browser shells expose scrollTo but do not implement it.
    }

    const messagesTab = document.getElementById('messagesTab');
    if (messagesTab) {
        messagesTab.scrollTop = 0;
    }
    if (_messagesContainer) {
        _messagesContainer.scrollTop = 0;
    }
}

/**
 * Render the messages tab content structure.
 */
function renderMessagesTabContent() {
    if (!_messagesContainer) return;

    syncVisibleReplyDeliveryAttempt();
    syncVisibleNewMessageDeliveryAttempt();

    _messagesContainer.innerHTML = `
        <div class="messages-layout">
            <div class="messages-sidebar">
                <div class="messages-sidebar-header">
                    <div class="messages-sidebar-header-main">
                        <span class="messages-sidebar-glyph" aria-hidden="true">${renderMessagePanelIcon('chat')}</span>
                        <div>
                            <h3>Conversations</h3>
                            <div class="messages-sidebar-kicker">Direct messages</div>
                        </div>
                    </div>
                    <button id="newMessageBtn" class="new-message-btn" type="button" title="New message" aria-label="New message">
                        ${renderMessagePanelIcon('compose')}
                    </button>
                </div>
                <div id="messageThreadList" class="message-thread-list">
                    <div class="loading">Loading conversations...</div>
                </div>
            </div>
            <div class="messages-main">
                <div id="messageViewHeader" class="message-view-header hidden">
                    <span id="conversationTitle" class="conversation-title" tabindex="0">Select a conversation</span>
                    <button id="messageTaskPanelBtn" class="chat-export-btn" type="button" title="Open tasks panel" aria-label="Tasks">📋</button>
                    <button id="refreshMessagesBtn" class="message-refresh-btn" type="button" title="Refresh" aria-label="Refresh messages">
                        ${renderMessagePanelIcon('refresh')}
                    </button>
                </div>
                <div id="messageViewContent" class="message-view-content">
                    <div class="message-empty-state" role="status" aria-live="polite">
                        <span class="message-empty-icon">${renderMessagePanelIcon('mail')}</span>
                        <p class="message-empty-title">Select a conversation</p>
                        <p class="message-empty-hint">Or start a new conversation</p>
                    </div>
                </div>
                <div id="messageComposeArea" class="message-compose-area hidden">
                    <div id="messageComposeFailure" class="message-send-error hidden" role="alert"></div>
                    <div id="messageComposeRecovery" class="message-send-recovery hidden">
                        <label id="messageComposeRecoveryLabel" for="messageComposeRecoverySelect">Send as member of:</label>
                        <select id="messageComposeRecoverySelect" class="message-send-recovery-select"></select>
                        <div id="messageComposeRecoveryNote" class="message-send-recovery-note"></div>
                    </div>
                    <div class="message-compose-row">
                        <textarea id="messageInput" aria-label="Reply" class="message-input" placeholder="Type your message..." rows="3"></textarea>
                        <button id="sendMessageBtn" class="send-message-btn" type="button" aria-busy="false">Send</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- New Message Modal -->
        <div id="newMessageModal" class="message-modal hidden">
            <div class="message-modal-content">
                <div class="message-modal-header">
                    <h3>New Message</h3>
                    <button id="closeNewMessageModal" class="modal-close-btn">×</button>
                </div>
                <div class="message-modal-body">
                    <label for="newMessageRecipient">To:</label>
                    <input type="text" id="newMessageRecipient" class="message-recipient-input"
                           placeholder="Type #V# then a person or agent name">
                    <label for="newMessageContent">Message:</label>
                    <textarea id="newMessageContent" class="message-content-input"
                              placeholder="Type your message..." rows="4"></textarea>
                    <div id="newMessageFailure" class="message-send-error hidden" role="alert"></div>
                    <div id="newMessageRecovery" class="message-send-recovery hidden">
                        <label id="newMessageRecoveryLabel" for="newMessageRecoverySelect">Send as member of:</label>
                        <select id="newMessageRecoverySelect" class="message-send-recovery-select"></select>
                        <div id="newMessageRecoveryNote" class="message-send-recovery-note"></div>
                    </div>
                </div>
                <div class="message-modal-footer">
                    <button id="cancelNewMessage" class="modal-cancel-btn">Cancel</button>
                    <button id="sendNewMessage" class="modal-send-btn" type="button" aria-busy="false">Send</button>
                </div>
            </div>
        </div>
    `;

    _replySendFailureState = null;
    _newMessageSendFailureState = null;

    // Attach event listeners
    attachMessagesEventListeners();
    initializeConceptAutocomplete(_messagesContainer.querySelector('#newMessageRecipient'));
    restoreNewMessageDeliveryAttemptForCurrentScope();
    renderComposePendingUi(COMPOSE_SCOPE_REPLY);
    renderComposePendingUi(COMPOSE_SCOPE_NEW_MESSAGE);
}

/**
 * Attach event listeners for the messages panel.
 */
function attachMessagesEventListeners() {
    if (!_messagesContainer) return;

    // New message button
    const newMsgBtn = _messagesContainer.querySelector('#newMessageBtn');
    if (newMsgBtn) {
        newMsgBtn.addEventListener('click', showNewMessageModal);
    }

    // Refresh button
    const refreshBtn = _messagesContainer.querySelector('#refreshMessagesBtn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => {
            if (_currentConversationUserId) {
                loadConversation(_currentConversationUserId);
            }
        });
    }

    // Send message button
    const sendBtn = _messagesContainer.querySelector('#sendMessageBtn');
    if (sendBtn) {
        sendBtn.addEventListener('click', handleSendReply);
    }

    // Message input - send on Enter (Shift+Enter for newline)
    const msgInput = _messagesContainer.querySelector('#messageInput');
    if (msgInput) {
        msgInput.addEventListener('keydown', (e) => {
            if (shouldSubmitComposerKey(e)) {
                e.preventDefault();
                handleSendReply();
            }
        });
        msgInput.addEventListener('input', syncVisibleReplyDeliveryAttempt);
        resizeCompactDraft(msgInput);
        window.addEventListener('resize', resizeVisibleReplyComposer);
        window.visualViewport?.addEventListener('resize', resizeVisibleReplyComposer);
    }

    const replyRecoverySelect = _messagesContainer.querySelector('#messageComposeRecoverySelect');
    if (replyRecoverySelect) {
        replyRecoverySelect.addEventListener('change', (event) => {
            updateComposeRecoverySelection(COMPOSE_SCOPE_REPLY, event.target?.value || '');
            syncVisibleReplyDeliveryAttempt();
        });
    }

    // New message modal
    const closeModalBtn = _messagesContainer.querySelector('#closeNewMessageModal');
    if (closeModalBtn) {
        closeModalBtn.addEventListener('click', () => hideNewMessageModal());
    }

    const cancelBtn = _messagesContainer.querySelector('#cancelNewMessage');
    if (cancelBtn) {
        cancelBtn.addEventListener('click', () => hideNewMessageModal());
    }

    const sendNewBtn = _messagesContainer.querySelector('#sendNewMessage');
    if (sendNewBtn) {
        sendNewBtn.addEventListener('click', handleSendNewMessage);
    }

    const newMessageRecoverySelect = _messagesContainer.querySelector('#newMessageRecoverySelect');
    if (newMessageRecoverySelect) {
        newMessageRecoverySelect.addEventListener('change', (event) => {
            updateComposeRecoverySelection(COMPOSE_SCOPE_NEW_MESSAGE, event.target?.value || '');
            syncVisibleNewMessageDeliveryAttempt();
        });
    }

    const recipientInput = _messagesContainer.querySelector('#newMessageRecipient');
    if (recipientInput) {
        recipientInput.addEventListener('input', () => {
            resetComposeFailureUi(COMPOSE_SCOPE_NEW_MESSAGE);
            syncVisibleNewMessageDeliveryAttempt();
        });
    }

    const newMessageContent = _messagesContainer.querySelector('#newMessageContent');
    if (newMessageContent) {
        newMessageContent.addEventListener('input', syncVisibleNewMessageDeliveryAttempt);
    }
}

/**
 * Load message threads (conversations) for the current user.
 */
async function loadMessageThreads() {
    if (_isLoading) return;

    _isLoading = true;
    let autoOpenUserId = null;
    const threadListEl = _messagesContainer?.querySelector('#messageThreadList');

    if (threadListEl) {
        threadListEl.innerHTML = '<div class="loading">Loading conversations...</div>';
    }

    try {
        const response = await getJson('/api/messages/threads?limit=20');
        _threads = response.threads || [];
        _currentUserId = response.current_user_id || null;
        renderThreadList();
        autoOpenUserId = resolveStoredReplyAttemptAutoOpenUserId()
            || resolveAutoOpenThreadUserId();

    } catch (err) {
        console.error('[messagePanel] Failed to load threads:', err);
        if (threadListEl) {
            threadListEl.innerHTML = '<div class="message-error">Failed to load conversations</div>';
        }
    } finally {
        _isLoading = false;
    }

    if (autoOpenUserId) {
        await selectConversation(autoOpenUserId);
    }
}

function getThreadUserId(thread) {
    const otherUsers = thread?._id || [];
    if (Array.isArray(otherUsers)) {
        return otherUsers[0] || null;
    }
    return otherUsers || null;
}

function resolveAutoOpenThreadUserId() {
    if (_threads.length !== 1) {
        return null;
    }

    const onlyUserId = getThreadUserId(_threads[0]);
    if (!onlyUserId) {
        return null;
    }

    const selectedThread = _messagesContainer?.querySelector('.message-thread-item.selected');
    const selectedUserId = selectedThread?.dataset?.userId || _currentConversationUserId;
    if (selectedUserId === onlyUserId) {
        return null;
    }

    return onlyUserId;
}

/**
 * Render the thread list.
 */
function renderThreadList() {
    const threadListEl = _messagesContainer?.querySelector('#messageThreadList');
    if (!threadListEl) return;

    if (_threads.length === 0) {
        threadListEl.innerHTML = `
            <div class="message-empty-threads">
                <p>No conversations yet</p>
                <p class="hint">Start a new conversation</p>
            </div>
        `;
        return;
    }

    let html = '';
    _threads.forEach(thread => {
        const otherUsers = thread._id || [];
        const lastMessage = thread.last_message || {};
        const messageCount = thread.message_count || 0;

        // Get display name for the other user(s)
        const displayName = Array.isArray(otherUsers)
            ? otherUsers.map(u => formatUserName(u)).join(', ')
            : formatUserName(otherUsers);

        // Get last message preview
        const lastContent = lastMessage.concept_data?.content_fallback || '';
        const preview = lastContent.length > 72
            ? lastContent.substring(0, 72) + '...'
            : lastContent;

        // Format time
        const lastTime = lastMessage.created_at
            ? formatTime(new Date(lastMessage.created_at))
            : '';

        const userId = getThreadUserId(thread);

        html += `
            <div class="message-thread-item" tabindex="0" data-user-id="${escapeHtml(userId)}"
                 title="Conversation with ${escapeHtml(displayName)}">
                <div class="thread-avatar">${getInitials(displayName)}</div>
                <div class="thread-info">
                    <div class="thread-name">${escapeHtml(displayName)}</div>
                    <div class="thread-preview">${escapeHtml(preview) || 'No messages'}</div>
                </div>
                <div class="thread-meta">
                    <span class="thread-time">${lastTime}</span>
                    <span class="thread-count" aria-label="${messageCount} messages">${messageCount}</span>
                </div>
            </div>
        `;
    });

    threadListEl.innerHTML = html;

    // Attach click handlers
    threadListEl.querySelectorAll('.message-thread-item').forEach(item => {
        bindMessageStreamMenu(item, () => item.dataset.userId);
        item.addEventListener('click', (event) => {
            if (event.ctrlKey) return;
            const userId = item.dataset.userId;
            if (userId) {
                selectConversation(userId);
            }
        });
    });
}

/**
 * Select a conversation to view.
 */
async function selectConversation(userId) {
    const renderedSelection = _messagesContainer?.querySelector(
        '.message-thread-item.selected',
    );
    if (renderedSelection) {
        syncVisibleReplyDeliveryAttempt();
    }
    const replyInput = _messagesContainer?.querySelector('#messageInput');
    if (replyInput) {
        replyInput.value = '';
        resizeCompactDraft(replyInput);
    }
    // Detach the in-memory attempt while changing scope without deleting the
    // session record. A matching recipient can bind it again after actor/thread
    // context has been read from the server.
    _replyDeliveryAttempt = null;
    _currentMessages = [];
    _currentConversationUserId = userId;
    resetComposeFailureUi(COMPOSE_SCOPE_REPLY);

    // Update thread list selection
    const threadListEl = _messagesContainer?.querySelector('#messageThreadList');
    if (threadListEl) {
        threadListEl.querySelectorAll('.message-thread-item').forEach(item => {
            item.classList.toggle('selected', item.dataset.userId === userId);
        });
    }

    // Show header and compose area
    const header = _messagesContainer?.querySelector('#messageViewHeader');
    const compose = _messagesContainer?.querySelector('#messageComposeArea');
    const title = _messagesContainer?.querySelector('#conversationTitle');

    if (header) header.classList.remove('hidden');
    if (compose) compose.classList.remove('hidden');
    if (title) title.textContent = `Conversation with ${formatUserName(userId)}`;
    if (title && !title.dataset.menuBound) {
        bindMessageStreamMenu(title, () => _currentConversationUserId);
        title.dataset.menuBound = 'true';
    }
    const taskButton = _messagesContainer?.querySelector('#messageTaskPanelBtn');
    if (taskButton) taskButton.onclick = () => void openMessageTasks();

    // Load the conversation
    await loadConversation(userId);
}

async function openMessageTasks() {
    const { showTaskPanel } = await import('./taskPanel.js');
    const taskIds = [...(_messagesContainer?.querySelectorAll('[data-task-concept="true"]') || [])]
        .map(element => element.dataset.fullConceptId);
    await showTaskPanel({ taskIds, sessionId: null });
}

function bindMessageStreamMenu(element, getOtherUserId) {
    const open = async event => {
        event.preventDefault();
        event.stopPropagation();
        const otherUserId = getOtherUserId();
        const userId = _currentUserId;
        if (!otherUserId || !userId) return;
        const thread = _threads.find(row => getThreadUserId(row) === otherUserId);
        const messages = otherUserId === _currentConversationUserId ? _currentMessages : [thread?.last_message].filter(Boolean);
        const payload = buildMessageStreamReference({ currentUserId: userId, otherUserId, messages, displayName: _exchange?.session_name || `Conversation with ${formatUserName(otherUserId)}`, participantIds: _exchange?.participant_ids, organisationConceptId: _exchange?.organisation_concept_id });
        const { openChatSessionMenu } = await import('../chatTab.js');
        if (!element.isConnected || _currentUserId !== userId) return;
        const rect = element.getBoundingClientRect();
        openChatSessionMenu(event.clientX || rect.left, event.clientY || rect.bottom, [{
            label: 'Copy conversation reference',
            onClick: async () => {
                if (_currentUserId !== userId) return;
                const copied = await copyTextWithClipboardFallback(JSON.stringify(payload, null, 2));
                showToast(copied ? 'Copied message conversation reference.' : 'Failed to copy conversation reference.', copied ? 'success' : 'error');
            }
        }], { returnFocus: element, focusFirst: true });
    };
    element.addEventListener('contextmenu', open);
    element.addEventListener('click', event => { if (event.ctrlKey) void open(event); });
    element.addEventListener('keydown', event => {
        if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) void open(event);
        else if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); void selectConversation(getOtherUserId()); }
    });
}

/**
 * Load messages for a conversation with a specific user.
 */
async function loadConversation(userId, { silent = false, before = null, observeReads = true } = {}) {
    if (_isLoading) return;
    _isLoading = true;
    const generation = _exchangeGeneration;
    const org = getSessionScopedOrgId();
    const contentEl = _messagesContainer?.querySelector('#messageViewContent');
    const oldTop = contentEl?.scrollTop || 0;
    const oldHeight = contentEl?.scrollHeight || 0;
    const nearBottom = !contentEl || oldHeight - oldTop - contentEl.clientHeight < 60;
    if (contentEl && !silent) contentEl.innerHTML = '<div class="loading">Loading conversation…</div>';
    try {
        const response = _exchange
            ? await postJson('/api/messages/exchange', { participant_ids: _exchange.participant_ids, organisation_concept_id: _exchange.organisation_concept_id || null, before })
            : await getJson(`/api/messages/conversation/${encodeURIComponent(userId)}?limit=50`);
        if (generation !== _exchangeGeneration || org !== getSessionScopedOrgId() || userId !== _currentConversationUserId) return;
        const incoming = response.messages || [];
        const compare = (a, b) => new Date(a.created_at) - new Date(b.created_at) || String(a.concept_id).localeCompare(String(b.concept_id));
        let nextMessages = incoming;
        if (before) nextMessages = [...new Map([..._currentMessages, ...incoming].map(m => [m.concept_id, m])).values()].sort(compare);
        else if (silent && incoming.length) {
            // Reconcile the returned range, including deletions and edits, while
            // retaining already loaded earlier pages. A reconnect gap remains pageable.
            const overlap = incoming.some(m => _currentMessages.some(old => old.concept_id === m.concept_id));
            if (!overlap && response.before) _olderCursor = response.before;
            nextMessages = [..._currentMessages.filter(m => compare(m, incoming[0]) < 0), ...incoming];
        }
        const sameContent = JSON.stringify(nextMessages.map(m => [m.concept_id, m.concept_data?.content_fallback])) === JSON.stringify(_currentMessages.map(m => [m.concept_id, m.concept_data?.content_fallback]));
        _currentMessages = nextMessages;
        if (silent && !before && sameContent) {
            updateUnreadMarkers();
            if (observeReads) observeDisplayedMessages();
            return;
        }
        if (before || !silent) _olderCursor = response.before || null;
        _currentUserId = response.current_user_id || null;
        await renderMessages();
        if (generation !== _exchangeGeneration || org !== getSessionScopedOrgId()) return;
        restoreReplyDeliveryAttemptForCurrentScope();
        if (_olderCursor && contentEl) {
            const earlier = document.createElement('button'); earlier.type = 'button'; earlier.textContent = 'Load earlier messages';
            earlier.onclick = () => void loadConversation(userId, { silent: true, before: _olderCursor });
            contentEl.prepend(earlier);
        }
        if (contentEl && before) contentEl.scrollTop = oldTop + contentEl.scrollHeight - oldHeight;
        else if (contentEl && silent && !nearBottom) {
            contentEl.scrollTop = oldTop;
            const jump = document.createElement('button'); jump.type = 'button'; jump.className = 'message-jump-latest'; jump.textContent = 'New messages · Jump to latest';
            jump.onclick = () => { contentEl.scrollTop = contentEl.scrollHeight; jump.remove(); };
            contentEl.append(jump);
        }
        updateUnreadMarkers();
        if (observeReads) observeDisplayedMessages();
    } catch (_) {
        if (generation === _exchangeGeneration && contentEl && !silent) contentEl.innerHTML = '<div class="message-error">Could not load this conversation. Try Refresh.</div>';
    } finally { if (generation === _exchangeGeneration) _isLoading = false; }
}

function isPaperRecommendationMessage(message) {
    const metadata = message?.concept_data?.metadata || {};
    return metadata?.delivery_channel === 'paper_recommendation_message'
        && Array.isArray(metadata?.recommendation_assertion_ids)
        && metadata.recommendation_assertion_ids.length > 0;
}

function findRecommendationPanel(messageId) {
    return Array.from(
        _messagesContainer?.querySelectorAll('[data-message-recommendation-panel]') || [],
    ).find((panel) => panel.dataset.messageId === messageId) || null;
}

function readLatestFeedbackValue(latestFeedback, keys = []) {
    if (!latestFeedback || typeof latestFeedback !== 'object') {
        return '';
    }
    for (const key of keys) {
        const value = String(latestFeedback?.[key] || '').trim();
        if (value) {
            return value;
        }
    }
    return '';
}

function normaliseLatestFeedback(latestFeedback) {
    return {
        recommendation_usefulness: readLatestFeedbackValue(latestFeedback, [
            'recommendation_usefulness',
            'recommendation_usefulness_label',
        ]),
        explanation_usefulness: readLatestFeedbackValue(latestFeedback, [
            'explanation_usefulness',
            'explanation_usefulness_label',
        ]),
        free_text_feedback: readLatestFeedbackValue(latestFeedback, [
            'free_text_feedback',
            'feedback_text',
        ]),
    };
}

function formatFeedbackSummary(latestFeedback) {
    const feedback = normaliseLatestFeedback(latestFeedback);
    const recommendation = feedback.recommendation_usefulness;
    const explanation = feedback.explanation_usefulness;
    const note = feedback.free_text_feedback;
    const bits = [];
    if (recommendation) bits.push(`Recommendation: ${recommendation.replace(/_/g, ' ')}`);
    if (explanation) bits.push(`Explanation: ${explanation.replace(/_/g, ' ')}`);
    if (note) bits.push(`Note: ${note}`);
    return bits.length
        ? `Latest feedback: ${bits.join(' | ')}`
        : 'Record whether the recommendation and its explanation were useful.';
}

async function submitMessageRecommendationFeedback({ messageId, row }) {
    const panel = findRecommendationPanel(messageId);
    if (!panel) return;
    const card = Array.from(panel.querySelectorAll('.recommendation-review-card')).find(
        (candidate) => candidate.dataset.assertionConceptId === row?.assertion_concept_id,
    );
    if (!card) return;

    const recommendationSelect = card.querySelector('[data-feedback-field="recommendation_usefulness"]');
    const explanationSelect = card.querySelector('[data-feedback-field="explanation_usefulness"]');
    const noteInput = card.querySelector('[data-feedback-field="feedback_text"]');
    const recommendation_usefulness = recommendationSelect?.value || '';
    const explanation_usefulness = explanationSelect?.value || '';
    const feedback_text = noteInput?.value?.trim() || '';

    if (!recommendation_usefulness && !explanation_usefulness && !feedback_text) {
        showToast('Choose feedback or add a note before submitting', 'warning');
        return;
    }

    const submitButton = card.querySelector('[data-feedback-submit]');
    if (submitButton) submitButton.disabled = true;
    try {
        await postJson(
            `/api/messages/${encodeURIComponent(messageId)}/paper_recommendation_feedback`,
            {
                assertion_concept_id: row?.assertion_concept_id,
                paper_concept_id: row?.paper_concept_id,
                recommendation_usefulness: recommendation_usefulness || undefined,
                explanation_usefulness: explanation_usefulness || undefined,
                feedback_text: feedback_text || undefined,
            },
        );
        showToast('Recommendation feedback saved', 'success');
        panel.dataset.reviewLoaded = 'false';
        await loadMessageRecommendationReview(messageId, { forceReload: true });
    } catch (err) {
        console.error('[messagePanel] Failed to save recommendation feedback:', err);
        showToast('Failed to save recommendation feedback', 'error');
    } finally {
        if (submitButton) submitButton.disabled = false;
    }
}

function appendMessageRecommendationFeedbackControls({ card, row, messageId }) {
    if (!card || !row?.assertion_concept_id) return;

    card.dataset.assertionConceptId = row.assertion_concept_id;
    const latestFeedback = normaliseLatestFeedback(row?.latest_feedback);

    const section = document.createElement('div');
    section.className = 'message-recommendation-feedback';

    const summary = document.createElement('div');
    summary.className = 'speech-settings-note';
    summary.textContent = formatFeedbackSummary(latestFeedback);
    section.appendChild(summary);

    const controls = document.createElement('div');
    controls.className = 'message-recommendation-feedback-controls';
    controls.innerHTML = `
        <label class="message-recommendation-feedback-field">
            <span>Recommendation</span>
            <select data-feedback-field="recommendation_usefulness">
                <option value="">Not set</option>
                <option value="useful">Useful</option>
                <option value="partly_useful">Partly useful</option>
                <option value="not_useful">Not useful</option>
            </select>
        </label>
        <label class="message-recommendation-feedback-field">
            <span>Explanation</span>
            <select data-feedback-field="explanation_usefulness">
                <option value="">Not set</option>
                <option value="useful">Useful</option>
                <option value="partly_useful">Partly useful</option>
                <option value="not_useful">Not useful</option>
            </select>
        </label>
        <label class="message-recommendation-feedback-field message-recommendation-feedback-field-wide">
            <span>Note</span>
            <textarea rows="2" data-feedback-field="feedback_text" placeholder="Optional note about usefulness or explanation quality"></textarea>
        </label>
    `;
    section.appendChild(controls);

    const recommendationSelect = controls.querySelector('[data-feedback-field="recommendation_usefulness"]');
    const explanationSelect = controls.querySelector('[data-feedback-field="explanation_usefulness"]');
    const noteInput = controls.querySelector('[data-feedback-field="feedback_text"]');
    if (recommendationSelect) recommendationSelect.value = latestFeedback.recommendation_usefulness;
    if (explanationSelect) explanationSelect.value = latestFeedback.explanation_usefulness;
    if (noteInput) noteInput.value = latestFeedback.free_text_feedback;

    const actions = document.createElement('div');
    actions.className = 'message-recommendation-actions';
    const submitButton = document.createElement('button');
    submitButton.type = 'button';
    submitButton.className = 'btn btn-secondary btn-sm';
    submitButton.textContent = row?.feedback_count > 0 ? 'Update feedback' : 'Submit feedback';
    submitButton.dataset.feedbackSubmit = '1';
    submitButton.addEventListener('click', () => {
        void submitMessageRecommendationFeedback({ messageId, row });
    });
    actions.appendChild(submitButton);
    section.appendChild(actions);

    card.appendChild(section);
}

async function loadMessageRecommendationReview(messageId, { forceReload = false } = {}) {
    const panel = findRecommendationPanel(messageId);
    if (!panel) return;

    const summaryElement = panel.querySelector('[data-message-recommendation-summary]');
    const resultsElement = panel.querySelector('[data-message-recommendation-results]');
    const statusElement = panel.querySelector('[data-message-recommendation-status]');
    const reviewBody = panel.querySelector('[data-message-recommendation-review]');
    const toggleButton = panel.querySelector('[data-message-recommendation-toggle]');
    if (!summaryElement || !resultsElement || !statusElement || !reviewBody || !toggleButton) return;

    if (!forceReload && panel.dataset.reviewLoaded === 'true') {
        reviewBody.classList.toggle('hidden');
        toggleButton.textContent = reviewBody.classList.contains('hidden')
            ? 'Review recommendations'
            : 'Hide review';
        return;
    }

    reviewBody.classList.remove('hidden');
    toggleButton.textContent = 'Hide review';
    statusElement.textContent = 'Loading recommendation review...';
    clearRecommendationReviewResults(summaryElement, resultsElement, 'Loading recommendation review...');

    try {
        const payload = await getJson(
            `/api/messages/${encodeURIComponent(messageId)}/paper_recommendation_review`,
        );
        renderRecommendationReviewPayload({
            summaryElement,
            containerElement: resultsElement,
            payload,
            cardEnhancer: ({ card, row }) => {
                appendMessageRecommendationFeedbackControls({ card, row, messageId });
            },
        });
        panel.dataset.reviewLoaded = 'true';
        statusElement.textContent = payload?.success
            ? 'Recommendation review ready.'
            : String(payload?.recommendation_report?.message || 'Recommendation review returned diagnostics.');
    } catch (err) {
        console.error('[messagePanel] Failed to load recommendation review:', err);
        panel.dataset.reviewLoaded = 'false';
        clearRecommendationReviewResults(
            summaryElement,
            resultsElement,
            'Recommendation review is unavailable for this message.',
        );
        statusElement.textContent = 'Failed to load recommendation review.';
    }
}

/**
 * Render messages in the conversation view.
 */
async function renderMessages() {
    const contentEl = _messagesContainer?.querySelector('#messageViewContent');
    if (!contentEl) return;

    if (_currentMessages.length === 0) {
        contentEl.innerHTML = `
            <div class="message-empty-state" role="status" aria-live="polite">
                <span class="message-empty-icon">${renderMessagePanelIcon('chat')}</span>
                <p class="message-empty-title">No messages yet</p>
                <p class="message-empty-hint">Send the first message!</p>
            </div>
        `;
        return;
    }

    let html = '<div class="message-list">';

    _currentMessages.forEach(msg => {
        const senderId = msg.relationships?.['#V#has_sender']?.[0] || '';
        const content = msg.concept_data?.content_fallback || '';
        const subject = typeof msg.concept_data?.subject === 'string'
            ? msg.concept_data.subject.trim()
            : '';
        const timestamp = msg.created_at ? new Date(msg.created_at) : null;
        const isRecommendationMessage = isPaperRecommendationMessage(msg);

        const isSent = _currentUserId
            ? senderId === _currentUserId
            : senderId !== _currentConversationUserId;

        html += `
            <div class="message-bubble ${isSent ? 'sent' : 'received'}" data-contribution-id="${escapeHtml(msg.concept_id || '')}">
                <div class="message-author" data-participant-id="${escapeHtml(senderId)}">${escapeHtml(formatUserName(senderId))}</div>
                ${subject ? `<div class="message-subject">${escapeHtml(subject)}</div>` : ''}
                <div class="message-content">${simpleMarkdownToHtml(content)}</div>
                ${isRecommendationMessage ? `
                <div class="message-recommendation-panel" data-message-recommendation-panel="1" data-message-id="${escapeHtml(msg.concept_id || '')}">
                    <div class="message-recommendation-header">
                        <span class="message-recommendation-label">Paper recommendations</span>
                        <button type="button" class="message-recommendation-toggle-btn" data-message-recommendation-toggle="1">
                            Review recommendations
                        </button>
                    </div>
                    <div class="message-recommendation-note">
                        Inspect the delivered recommendation cards here and record feedback directly on the message.
                    </div>
                    <div class="message-recommendation-status" data-message-recommendation-status></div>
                    <div class="message-recommendation-review hidden" data-message-recommendation-review>
                        <div class="runtime-hint message-recommendation-summary" data-message-recommendation-summary>
                            Open the review to inspect recommendation rationale and provenance.
                        </div>
                        <div class="recommendation-review-results" data-message-recommendation-results></div>
                    </div>
                </div>
                ` : ''}
                <div class="message-meta">
                    <span class="message-time">${timestamp ? formatTime(timestamp) : ''}</span>
                    <button type="button" class="message-copy-markdown" data-message-id="${escapeHtml(msg.concept_id || '')}" aria-label="Copy message as Markdown">Copy</button>
                    ${msg.concept_id ? `<button type="button" class="message-discuss-btn"
                        data-concept-id="${escapeHtml(msg.concept_id)}"
                        title="Discuss this message with Von">Discuss with Von</button>` : ''}
                </div>
            </div>
        `;
    });

    html += '</div>';
    contentEl.innerHTML = html;
    contentEl.querySelectorAll('.message-author[data-participant-id]').forEach(author => {
        const id = author.dataset.participantId;
        const profile = _exchange?.participant_profiles?.find(profile => profile.concept_id === id) || { display_name: formatUserName(id) };
        author.textContent = profile.display_name;
        author.prepend(participantAvatar(profile));
    });

    const messageContentEls = Array.from(contentEl.querySelectorAll('.message-list .message-content'));
    messageContentEls.forEach((messageContentEl) => {
        // Preserve Markdown structure while decorating text nodes.
        const walker = document.createTreeWalker(messageContentEl, NodeFilter.SHOW_TEXT);
        const nodes = [];
        while (walker.nextNode()) if (walker.currentNode.textContent.includes('#V#') && !walker.currentNode.parentElement.closest('code, pre, a, button')) nodes.push(walker.currentNode);
        nodes.forEach(node => node.replaceWith(createCartoucheFragment(node.textContent)));
    });
    contentEl.querySelectorAll('[data-message-recommendation-toggle]').forEach((button) => {
        button.addEventListener('click', () => {
            const panel = button.closest('[data-message-recommendation-panel]');
            const messageId = panel?.dataset?.messageId;
            if (messageId) {
                void loadMessageRecommendationReview(messageId);
            }
        });
    });
    contentEl.querySelectorAll('.message-copy-markdown').forEach(button => { button.onclick = async () => { const message = _currentMessages.find(m => m.concept_id === button.dataset.messageId); if (message) { const copied = await copyTextWithClipboardFallback(message.concept_data?.content_fallback || ''); button.textContent = copied ? 'Copied' : 'Copy failed'; } }; });
    contentEl.querySelectorAll('.message-discuss-btn').forEach((button) => {
        button.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            const conceptId = String(event.currentTarget.dataset.conceptId || '').trim();
            if (!conceptId) return;
            document.dispatchEvent(new CustomEvent('von:discussConcept', {
                detail: {
                    conceptId,
                    conceptName: 'Message',
                    source: 'message'
                }
            }));
        });
    });
    await hydrateConceptCartouchesInRoot(contentEl);

    // Scroll to bottom
    contentEl.scrollTop = contentEl.scrollHeight;
}

function isComposePending(scope) {
    if (scope === COMPOSE_SCOPE_REPLY) {
        return _replySendPending;
    }
    if (scope === COMPOSE_SCOPE_NEW_MESSAGE) {
        return _newMessageSendPending;
    }
    return false;
}

function renderComposePendingUi(scope) {
    if (!_messagesContainer) return;

    const button = scope === COMPOSE_SCOPE_REPLY
        ? _messagesContainer.querySelector('#sendMessageBtn')
        : _messagesContainer.querySelector('#sendNewMessage');
    if (!button) return;

    const pending = isComposePending(scope);
    button.disabled = pending || (scope === COMPOSE_SCOPE_REPLY && _exchange?.other_participant_ids?.length > 1);
    button.textContent = pending ? 'Sending…' : 'Send';
    button.setAttribute('aria-busy', pending ? 'true' : 'false');

    if (scope === COMPOSE_SCOPE_NEW_MESSAGE) {
        const recipientInput = _messagesContainer.querySelector('#newMessageRecipient');
        const contentInput = _messagesContainer.querySelector('#newMessageContent');
        const closeButton = _messagesContainer.querySelector('#closeNewMessageModal');
        const cancelButton = _messagesContainer.querySelector('#cancelNewMessage');
        if (recipientInput) recipientInput.disabled = pending;
        if (contentInput) contentInput.disabled = pending;
        if (closeButton) closeButton.disabled = pending;
        if (cancelButton) cancelButton.disabled = pending;
    }
}

function setComposePending(scope, pending) {
    const nextPending = Boolean(pending);
    if (scope === COMPOSE_SCOPE_REPLY) {
        _replySendPending = nextPending;
    } else if (scope === COMPOSE_SCOPE_NEW_MESSAGE) {
        _newMessageSendPending = nextPending;
    }
    renderComposePendingUi(scope);
}

function getComposeFailureState(scope) {
    if (scope === COMPOSE_SCOPE_REPLY) {
        return _replySendFailureState;
    }
    if (scope === COMPOSE_SCOPE_NEW_MESSAGE) {
        return _newMessageSendFailureState;
    }
    return null;
}

function getDeliveryAttempt(scope) {
    if (scope === COMPOSE_SCOPE_REPLY) {
        return _replyDeliveryAttempt;
    }
    if (scope === COMPOSE_SCOPE_NEW_MESSAGE) {
        return _newMessageDeliveryAttempt;
    }
    return null;
}

function setDeliveryAttempt(scope, attempt) {
    if (scope === COMPOSE_SCOPE_REPLY) {
        _replyDeliveryAttempt = attempt;
        if (attempt) {
            persistReplyDeliveryAttempt(attempt);
        } else {
            clearPersistedReplyDeliveryAttempt();
        }
    } else if (scope === COMPOSE_SCOPE_NEW_MESSAGE) {
        _newMessageDeliveryAttempt = attempt;
        if (attempt) {
            persistNewMessageDeliveryAttempt(attempt);
        } else {
            clearPersistedNewMessageDeliveryAttempt();
        }
    }
}

function normaliseDeliveryConceptId(value) {
    const cleaned = String(value || '').trim();
    if (!cleaned) return '';
    return cleaned.startsWith('#V#') ? cleaned : `#V#${cleaned}`;
}

function createDeliveryIdempotencyKey() {
    try {
        if (typeof globalThis.crypto?.randomUUID === 'function') {
            return globalThis.crypto.randomUUID();
        }
    } catch (_) {
        // Fall back to a process-local, time-based key in older browser shells.
    }

    _deliveryKeySequence += 1;
    const randomPart = Math.random().toString(36).slice(2, 12);
    return `dm-${Date.now().toString(36)}-${_deliveryKeySequence.toString(36)}-${randomPart}`;
}

function resolveDeliveryOrganisationId(explicitOrganisationConceptId) {
    return normaliseDeliveryConceptId(
        explicitOrganisationConceptId || getSessionScopedOrgId() || '',
    );
}

function readSessionActorConceptId() {
    try {
        const raw = sessionStorage.getItem('von_current_user');
        if (!raw) return '';
        let storedUser = raw;
        try {
            storedUser = JSON.parse(raw);
        } catch (_) {
            // Legacy storage may contain the concept ID directly.
        }
        if (typeof storedUser === 'string') {
            return normaliseDeliveryConceptId(storedUser);
        }
        return normaliseDeliveryConceptId(
            storedUser?.concept_id || storedUser?.id || '',
        );
    } catch (_) {
        return '';
    }
}

function resolveCurrentReplyActorId() {
    return normaliseDeliveryConceptId(_currentUserId || readSessionActorConceptId());
}

function resolveCurrentReplyThreadId() {
    const threadIds = new Set();
    _currentMessages.forEach((message) => {
        const values = message?.relationships?.[MESSAGE_THREAD_PREDICATE_ID];
        if (!Array.isArray(values)) return;
        values.forEach((value) => {
            const conceptId = normaliseDeliveryConceptId(value);
            if (conceptId) threadIds.add(conceptId);
        });
    });
    return threadIds.size === 1 ? Array.from(threadIds)[0] : '';
}

function buildDeliveryScope({
    recipientIds,
    organisationConceptId,
    actorUserId,
    threadId,
}) {
    const normalisedRecipients = recipientIds
        .map(normaliseDeliveryConceptId)
        .filter(Boolean)
        .sort((left, right) => left.localeCompare(right));
    return {
        actor_user_id: normaliseDeliveryConceptId(
            actorUserId || resolveCurrentReplyActorId(),
        ),
        organisation_concept_id: resolveDeliveryOrganisationId(organisationConceptId),
        recipient_ids: normalisedRecipients,
        thread_id: normaliseDeliveryConceptId(threadId || ''),
    };
}

function buildDeliveryFingerprintFromScope(deliveryScope, content) {
    return JSON.stringify({
        scope: deliveryScope,
        content: String(content || '').trim(),
    });
}

function deliveryScopesMatch(left, right) {
    return JSON.stringify(left || null) === JSON.stringify(right || null);
}

function buildCurrentComposeSessionScope() {
    return {
        actor_user_id: readSessionActorConceptId(),
        organisation_concept_id: resolveDeliveryOrganisationId(''),
    };
}

function composeSessionScopesEqual(left, right) {
    return JSON.stringify(left || null) === JSON.stringify(right || null);
}

function isBoundComposeSessionScope(scope) {
    return Boolean(
        scope?.actor_user_id
        && scope?.organisation_concept_id
    );
}

function clearPersistedReplyDeliveryAttempt() {
    try {
        sessionStorage.removeItem(REPLY_DELIVERY_ATTEMPT_STORAGE_KEY);
    } catch (_) {
        // Storage can be unavailable in restricted browser contexts.
    }
}

function persistReplyDeliveryAttempt(attempt) {
    if (
        !attempt?.key
        || !attempt?.fingerprint
        || !String(attempt?.draft || '').trim()
        || !isBoundComposeSessionScope(attempt?.sessionScope)
    ) {
        clearPersistedReplyDeliveryAttempt();
        return;
    }
    try {
        sessionStorage.setItem(REPLY_DELIVERY_ATTEMPT_STORAGE_KEY, JSON.stringify({
            schema_version: REPLY_DELIVERY_ATTEMPT_SCHEMA_VERSION,
            key: attempt.key,
            fingerprint: attempt.fingerprint,
            draft: attempt.draft,
            scope: attempt.scope,
            session_scope: attempt.sessionScope,
            recovery_state: attempt.recoveryState || null,
        }));
    } catch (_) {
        // The in-memory attempt still protects the current rendered tab.
    }
}

function readPersistedReplyDeliveryAttempt() {
    let stored;
    try {
        stored = JSON.parse(
            sessionStorage.getItem(REPLY_DELIVERY_ATTEMPT_STORAGE_KEY) || 'null',
        );
    } catch (_) {
        clearPersistedReplyDeliveryAttempt();
        return null;
    }

    const draft = typeof stored?.draft === 'string' ? stored.draft : '';
    const key = typeof stored?.key === 'string' ? stored.key.trim() : '';
    const fingerprint = typeof stored?.fingerprint === 'string'
        ? stored.fingerprint
        : '';
    const scope = stored?.scope;
    const recipientIds = Array.isArray(scope?.recipient_ids)
        ? scope.recipient_ids.map(normaliseDeliveryConceptId).filter(Boolean)
        : [];
    const normalisedScope = {
        actor_user_id: normaliseDeliveryConceptId(scope?.actor_user_id || ''),
        organisation_concept_id: normaliseDeliveryConceptId(
            scope?.organisation_concept_id || '',
        ),
        recipient_ids: recipientIds.sort((left, right) => left.localeCompare(right)),
        thread_id: normaliseDeliveryConceptId(scope?.thread_id || ''),
    };
    const expectedFingerprint = buildDeliveryFingerprintFromScope(normalisedScope, draft);
    const rawSessionScope = stored?.session_scope;
    const sessionScope = {
        actor_user_id: normaliseDeliveryConceptId(rawSessionScope?.actor_user_id || ''),
        organisation_concept_id: normaliseDeliveryConceptId(
            rawSessionScope?.organisation_concept_id || '',
        ),
    };
    const recoveryState = normaliseComposeFailureState(stored?.recovery_state);
    const requiresExplicitRecoveryOrganisation = Boolean(
        normalisedScope.organisation_concept_id
        && normalisedScope.organisation_concept_id
            !== sessionScope.organisation_concept_id
    );

    if (
        stored?.schema_version !== REPLY_DELIVERY_ATTEMPT_SCHEMA_VERSION
        || !key
        || !draft.trim()
        || !normalisedScope.actor_user_id
        || normalisedScope.recipient_ids.length === 0
        || !isBoundComposeSessionScope(sessionScope)
        || (
            requiresExplicitRecoveryOrganisation
            && recoveryState?.selectedOrganisationConceptId
                !== normalisedScope.organisation_concept_id
        )
        || fingerprint !== expectedFingerprint
    ) {
        clearPersistedReplyDeliveryAttempt();
        return null;
    }

    return {
        key,
        fingerprint,
        draft,
        scope: normalisedScope,
        sessionScope,
        recoveryState,
    };
}

function clearPersistedNewMessageDeliveryAttempt() {
    try {
        sessionStorage.removeItem(NEW_MESSAGE_DELIVERY_ATTEMPT_STORAGE_KEY);
    } catch (_) {
        // Storage can be unavailable in restricted browser contexts.
    }
}

function persistNewMessageDeliveryAttempt(attempt) {
    const recipient = typeof attempt?.recipient === 'string' ? attempt.recipient : '';
    const draft = typeof attempt?.draft === 'string' ? attempt.draft : '';
    const sessionScope = attempt?.sessionScope;
    if (
        !attempt?.key
        || !attempt?.fingerprint
        || !recipient.trim()
        || !draft.trim()
        || !sessionScope?.actor_user_id
        || !sessionScope?.organisation_concept_id
    ) {
        clearPersistedNewMessageDeliveryAttempt();
        return;
    }

    try {
        sessionStorage.setItem(NEW_MESSAGE_DELIVERY_ATTEMPT_STORAGE_KEY, JSON.stringify({
            schema_version: NEW_MESSAGE_DELIVERY_ATTEMPT_SCHEMA_VERSION,
            key: attempt.key,
            fingerprint: attempt.fingerprint,
            recipient,
            draft,
            scope: sessionScope,
            delivery_scope: attempt.scope,
            recovery_state: attempt.recoveryState || null,
        }));
    } catch (_) {
        // The in-memory attempt still protects the current rendered tab.
    }
}

function readPersistedNewMessageDeliveryAttempt() {
    let stored;
    try {
        stored = JSON.parse(
            sessionStorage.getItem(NEW_MESSAGE_DELIVERY_ATTEMPT_STORAGE_KEY) || 'null',
        );
    } catch (_) {
        clearPersistedNewMessageDeliveryAttempt();
        return null;
    }

    const recipient = typeof stored?.recipient === 'string' ? stored.recipient : '';
    const draft = typeof stored?.draft === 'string' ? stored.draft : '';
    const key = typeof stored?.key === 'string' ? stored.key.trim() : '';
    const fingerprint = typeof stored?.fingerprint === 'string'
        ? stored.fingerprint
        : '';
    const sessionScope = {
        actor_user_id: normaliseDeliveryConceptId(stored?.scope?.actor_user_id || ''),
        organisation_concept_id: normaliseDeliveryConceptId(
            stored?.scope?.organisation_concept_id || '',
        ),
    };
    const rawDeliveryScope = stored?.delivery_scope;
    const recipientIds = Array.isArray(rawDeliveryScope?.recipient_ids)
        ? rawDeliveryScope.recipient_ids
            .map(normaliseDeliveryConceptId)
            .filter(Boolean)
            .sort((left, right) => left.localeCompare(right))
        : [];
    const deliveryScope = {
        actor_user_id: normaliseDeliveryConceptId(rawDeliveryScope?.actor_user_id || ''),
        organisation_concept_id: normaliseDeliveryConceptId(
            rawDeliveryScope?.organisation_concept_id || '',
        ),
        recipient_ids: recipientIds,
        thread_id: normaliseDeliveryConceptId(rawDeliveryScope?.thread_id || ''),
    };
    const expectedFingerprint = buildDeliveryFingerprintFromScope(deliveryScope, draft);
    const recoveryState = normaliseComposeFailureState(stored?.recovery_state);
    const requiresExplicitRecoveryOrganisation = Boolean(
        deliveryScope.organisation_concept_id
        && deliveryScope.organisation_concept_id
            !== sessionScope.organisation_concept_id
    );

    if (
        stored?.schema_version !== NEW_MESSAGE_DELIVERY_ATTEMPT_SCHEMA_VERSION
        || !key
        || !recipient.trim()
        || !draft.trim()
        || !sessionScope.actor_user_id
        || !sessionScope.organisation_concept_id
        || deliveryScope.recipient_ids.length !== 1
        || (
            requiresExplicitRecoveryOrganisation
            && recoveryState?.selectedOrganisationConceptId
                !== deliveryScope.organisation_concept_id
        )
        || normaliseDeliveryConceptId(recipient) !== deliveryScope.recipient_ids[0]
        || fingerprint !== expectedFingerprint
    ) {
        clearPersistedNewMessageDeliveryAttempt();
        return null;
    }

    return {
        key,
        fingerprint,
        recipient,
        draft,
        scope: deliveryScope,
        sessionScope,
        recoveryState,
    };
}

function buildCurrentReplyDeliveryScope(organisationConceptId) {
    if (!_currentConversationUserId) return null;
    const selectedOrganisationConceptId = organisationConceptId === undefined
        ? getSelectedRecoveryOrganisationConceptId(COMPOSE_SCOPE_REPLY)
        : organisationConceptId;
    return buildDeliveryScope({
        recipientIds: [_currentConversationUserId],
        organisationConceptId: selectedOrganisationConceptId,
        actorUserId: _currentUserId,
        threadId: resolveCurrentReplyThreadId(),
    });
}

function replaceVisibleDeliveryAttempt(scope, {
    deliveryScope,
    draft,
    recipient = '',
    sessionScope,
}) {
    const fingerprint = buildDeliveryFingerprintFromScope(deliveryScope, draft);
    setDeliveryAttempt(scope, {
        fingerprint,
        key: createDeliveryIdempotencyKey(),
        draft,
        scope: deliveryScope,
        sessionScope,
        recoveryState: getPersistableComposeRecoveryState(scope),
        ...(scope === COMPOSE_SCOPE_NEW_MESSAGE ? { recipient } : {}),
    });
}

function resizeVisibleReplyComposer() {
    resizeCompactDraft(_messagesContainer?.querySelector('#messageInput'));
}

function syncVisibleReplyDeliveryAttempt() {
    const replyInput = _messagesContainer?.querySelector('#messageInput');
    if (!replyInput) return;

    resizeCompactDraft(replyInput);
    const draft = replyInput.value;
    const currentAttempt = getDeliveryAttempt(COMPOSE_SCOPE_REPLY);
    if (!currentAttempt) {
        // A newly typed visible draft supersedes any off-screen attempt from a
        // different conversation. Do not retain its invisible key.
        if (draft.trim()) {
            clearPersistedReplyDeliveryAttempt();
        }
        return;
    }

    if (!draft.trim()) {
        setDeliveryAttempt(COMPOSE_SCOPE_REPLY, null);
        return;
    }

    const deliveryScope = buildCurrentReplyDeliveryScope();
    const fingerprint = deliveryScope
        ? buildDeliveryFingerprintFromScope(deliveryScope, draft)
        : '';
    const sessionScope = buildCurrentComposeSessionScope();
    if (!composeSessionScopesEqual(sessionScope, currentAttempt.sessionScope)) {
        setDeliveryAttempt(COMPOSE_SCOPE_REPLY, null);
        return;
    }
    if (
        !deliveryScope
        || fingerprint !== currentAttempt.fingerprint
        || !deliveryScopesMatch(deliveryScope, currentAttempt.scope)
    ) {
        if (!deliveryScope) {
            setDeliveryAttempt(COMPOSE_SCOPE_REPLY, null);
            return;
        }
        replaceVisibleDeliveryAttempt(COMPOSE_SCOPE_REPLY, {
            deliveryScope,
            draft,
            sessionScope,
        });
        return;
    }

    setDeliveryAttempt(COMPOSE_SCOPE_REPLY, {
        ...currentAttempt,
        draft,
        scope: deliveryScope,
        sessionScope,
        recoveryState: getPersistableComposeRecoveryState(COMPOSE_SCOPE_REPLY),
    });
}

function syncVisibleNewMessageDeliveryAttempt() {
    const recipientInput = _messagesContainer?.querySelector('#newMessageRecipient');
    const contentInput = _messagesContainer?.querySelector('#newMessageContent');
    if (!recipientInput || !contentInput) return;

    const recipient = recipientInput.value;
    const draft = contentInput.value;
    const currentAttempt = getDeliveryAttempt(COMPOSE_SCOPE_NEW_MESSAGE);
    if (!currentAttempt) {
        if (recipient.trim() || draft.trim()) {
            clearPersistedNewMessageDeliveryAttempt();
        }
        return;
    }

    if (!recipient.trim() || !draft.trim()) {
        setDeliveryAttempt(COMPOSE_SCOPE_NEW_MESSAGE, null);
        return;
    }

    const failureState = getComposeFailureState(COMPOSE_SCOPE_NEW_MESSAGE);
    const selectedOrganisationConceptId = String(
        failureState?.selectedOrganisationConceptId || '',
    ).trim();
    const deliveryScope = buildDeliveryScope({
        recipientIds: [recipient],
        organisationConceptId: selectedOrganisationConceptId,
    });
    const fingerprint = buildDeliveryFingerprintFromScope(deliveryScope, draft);
    const sessionScope = buildCurrentComposeSessionScope();
    if (!composeSessionScopesEqual(sessionScope, currentAttempt.sessionScope)) {
        setDeliveryAttempt(COMPOSE_SCOPE_NEW_MESSAGE, null);
        return;
    }
    if (
        fingerprint !== currentAttempt.fingerprint
        || !deliveryScopesMatch(deliveryScope, currentAttempt.scope)
    ) {
        replaceVisibleDeliveryAttempt(COMPOSE_SCOPE_NEW_MESSAGE, {
            deliveryScope,
            recipient,
            draft,
            sessionScope,
        });
        return;
    }

    setDeliveryAttempt(COMPOSE_SCOPE_NEW_MESSAGE, {
        ...currentAttempt,
        recipient,
        draft,
        scope: deliveryScope,
        sessionScope,
        recoveryState: getPersistableComposeRecoveryState(COMPOSE_SCOPE_NEW_MESSAGE),
    });
}

function restoreNewMessageDeliveryAttemptForCurrentScope() {
    const persistedAttempt = readPersistedNewMessageDeliveryAttempt();
    const sessionScope = buildCurrentComposeSessionScope();
    if (
        !persistedAttempt
        || !isBoundComposeSessionScope(sessionScope)
        || !composeSessionScopesEqual(persistedAttempt.sessionScope, sessionScope)
    ) {
        _newMessageDeliveryAttempt = null;
        return false;
    }

    const modal = _messagesContainer?.querySelector('#newMessageModal');
    const recipientInput = modal?.querySelector('#newMessageRecipient');
    const contentInput = modal?.querySelector('#newMessageContent');
    if (!modal || !recipientInput || !contentInput) {
        _newMessageDeliveryAttempt = null;
        return false;
    }

    _newMessageDeliveryAttempt = persistedAttempt;
    recipientInput.value = persistedAttempt.recipient;
    contentInput.value = persistedAttempt.draft;
    modal.classList.remove('hidden');
    setComposeFailureState(COMPOSE_SCOPE_NEW_MESSAGE, persistedAttempt.recoveryState);
    return true;
}

function restoreReplyDeliveryAttemptForCurrentScope() {
    const replyInput = _messagesContainer?.querySelector('#messageInput');
    if (!replyInput) return false;

    const persistedAttempt = readPersistedReplyDeliveryAttempt();
    const sessionScope = buildCurrentComposeSessionScope();
    const deliveryScope = persistedAttempt
        ? buildCurrentReplyDeliveryScope(
            persistedAttempt.scope.organisation_concept_id,
        )
        : null;
    if (
        !persistedAttempt
        || !isBoundComposeSessionScope(sessionScope)
        || !composeSessionScopesEqual(persistedAttempt.sessionScope, sessionScope)
        || !deliveryScope
        || !deliveryScopesMatch(persistedAttempt.scope, deliveryScope)
        || persistedAttempt.fingerprint
            !== buildDeliveryFingerprintFromScope(deliveryScope, persistedAttempt.draft)
    ) {
        _replyDeliveryAttempt = null;
        return false;
    }

    _replyDeliveryAttempt = persistedAttempt;
    replyInput.value = persistedAttempt.draft;
    resizeCompactDraft(replyInput);
    setComposeFailureState(COMPOSE_SCOPE_REPLY, persistedAttempt.recoveryState);
    return true;
}

function resolveStoredReplyAttemptAutoOpenUserId() {
    const persistedAttempt = readPersistedReplyDeliveryAttempt();
    if (!persistedAttempt) return null;

    const sessionScope = buildCurrentComposeSessionScope();
    if (
        !isBoundComposeSessionScope(sessionScope)
        || !composeSessionScopesEqual(persistedAttempt.sessionScope, sessionScope)
        || persistedAttempt.scope.recipient_ids.length !== 1
    ) {
        return null;
    }

    const recipientId = persistedAttempt.scope.recipient_ids[0];
    const matchingThread = _threads.some(
        (thread) => normaliseDeliveryConceptId(getThreadUserId(thread)) === recipientId,
    );
    return matchingThread ? recipientId : null;
}

function getOrCreateDeliveryIdempotencyKey({
    scope,
    recipientIds,
    content,
    visibleDraft,
    visibleRecipient,
    organisationConceptId,
    actorUserId,
    threadId,
}) {
    const deliveryScope = buildDeliveryScope({
        recipientIds,
        organisationConceptId,
        actorUserId,
        threadId,
    });
    const fingerprint = buildDeliveryFingerprintFromScope(deliveryScope, content);
    const draft = String(visibleDraft ?? content ?? '');
    const recipient = String(visibleRecipient ?? recipientIds[0] ?? '');
    const sessionScope = buildCurrentComposeSessionScope();
    const recoveryState = getPersistableComposeRecoveryState(scope);
    const currentAttempt = getDeliveryAttempt(scope);
    const sameSessionScope = composeSessionScopesEqual(
        sessionScope,
        currentAttempt?.sessionScope,
    );
    if (
        currentAttempt?.fingerprint === fingerprint
        && currentAttempt?.key
        && sameSessionScope
    ) {
        setDeliveryAttempt(scope, {
            ...currentAttempt,
            draft,
            sessionScope,
            recoveryState,
            ...(scope === COMPOSE_SCOPE_NEW_MESSAGE ? { recipient } : {}),
            scope: deliveryScope,
        });
        return currentAttempt.key;
    }

    const key = createDeliveryIdempotencyKey();
    setDeliveryAttempt(scope, {
        fingerprint,
        key,
        draft,
        sessionScope,
        recoveryState,
        ...(scope === COMPOSE_SCOPE_NEW_MESSAGE ? { recipient } : {}),
        scope: deliveryScope,
    });
    return key;
}

function getComposeUiElements(scope) {
    if (!_messagesContainer) return {};

    if (scope === COMPOSE_SCOPE_REPLY) {
        return {
            errorEl: _messagesContainer.querySelector('#messageComposeFailure'),
            recoveryEl: _messagesContainer.querySelector('#messageComposeRecovery'),
            recoveryLabelEl: _messagesContainer.querySelector('#messageComposeRecoveryLabel'),
            recoverySelectEl: _messagesContainer.querySelector('#messageComposeRecoverySelect'),
            recoveryNoteEl: _messagesContainer.querySelector('#messageComposeRecoveryNote'),
        };
    }

    if (scope === COMPOSE_SCOPE_NEW_MESSAGE) {
        return {
            errorEl: _messagesContainer.querySelector('#newMessageFailure'),
            recoveryEl: _messagesContainer.querySelector('#newMessageRecovery'),
            recoveryLabelEl: _messagesContainer.querySelector('#newMessageRecoveryLabel'),
            recoverySelectEl: _messagesContainer.querySelector('#newMessageRecoverySelect'),
            recoveryNoteEl: _messagesContainer.querySelector('#newMessageRecoveryNote'),
        };
    }

    return {};
}

function normaliseRecoveryOrganisationOption(option) {
    if (!option || typeof option !== 'object') {
        return null;
    }

    const conceptId = normaliseDeliveryConceptId(
        option.concept_id || option.conceptId || '',
    );
    if (!conceptId) {
        return null;
    }

    const rawName = String(option.name || '').trim();
    const role = String(option.role || '').trim();
    return {
        conceptId,
        name: rawName || formatUserName(conceptId),
        role,
    };
}

function buildMessageSendFailureState(err) {
    const payload = (err && typeof err === 'object' && err.payload && typeof err.payload === 'object')
        ? err.payload
        : null;
    const explicitError = String(payload?.error || '').trim();
    const errMessage = String(err?.message || '').trim();
    const organisationOptions = Array.isArray(payload?.common_organisation_options)
        ? payload.common_organisation_options
            .map(normaliseRecoveryOrganisationOption)
            .filter(Boolean)
        : [];

    return {
        errorMessage: explicitError || (/^HTTP \d+$/u.test(errMessage) ? 'Failed to send message' : errMessage) || 'Failed to send message',
        organisationOptions,
        selectedOrganisationConceptId: organisationOptions.length === 1
            ? organisationOptions[0].conceptId
            : '',
    };
}

function normaliseComposeFailureState(nextState) {
    if (!nextState || typeof nextState !== 'object') {
        return null;
    }

    const organisationOptions = Array.isArray(nextState.organisationOptions)
        ? nextState.organisationOptions
            .map(normaliseRecoveryOrganisationOption)
            .filter(Boolean)
        : [];
    const selectedOrganisationConceptId = normaliseDeliveryConceptId(
        nextState.selectedOrganisationConceptId || '',
    );
    const knownOrganisationIds = new Set(
        organisationOptions.map((option) => option.conceptId),
    );

    return {
        errorMessage: String(nextState.errorMessage || '').trim() || 'Failed to send message',
        organisationOptions,
        selectedOrganisationConceptId: organisationOptions.length === 1
            ? organisationOptions[0].conceptId
            : (knownOrganisationIds.has(selectedOrganisationConceptId)
                ? selectedOrganisationConceptId
                : ''),
    };
}

function getPersistableComposeRecoveryState(scope) {
    const failureState = normaliseComposeFailureState(getComposeFailureState(scope));
    if (
        !failureState?.selectedOrganisationConceptId
        || failureState.organisationOptions.length === 0
    ) {
        return null;
    }
    return failureState;
}

function getSelectedRecoveryOrganisationConceptId(scope) {
    return normaliseDeliveryConceptId(
        getComposeFailureState(scope)?.selectedOrganisationConceptId || '',
    );
}

function buildMessageSendFailureStatePreservingRecovery(scope, err) {
    const nextState = buildMessageSendFailureState(err);
    if (nextState.organisationOptions.length > 0) {
        return nextState;
    }

    const retainedRecoveryState = getPersistableComposeRecoveryState(scope)
        || normaliseComposeFailureState(getDeliveryAttempt(scope)?.recoveryState);
    if (!retainedRecoveryState?.selectedOrganisationConceptId) {
        return nextState;
    }

    return {
        ...nextState,
        organisationOptions: retainedRecoveryState.organisationOptions,
        selectedOrganisationConceptId:
            retainedRecoveryState.selectedOrganisationConceptId,
    };
}

function recordComposeSendFailure(scope, err) {
    setComposeFailureState(
        scope,
        buildMessageSendFailureStatePreservingRecovery(scope, err),
    );
    if (scope === COMPOSE_SCOPE_REPLY) {
        syncVisibleReplyDeliveryAttempt();
    } else if (scope === COMPOSE_SCOPE_NEW_MESSAGE) {
        syncVisibleNewMessageDeliveryAttempt();
    }
}

function renderComposeFailureUi(scope) {
    const failureState = getComposeFailureState(scope);
    const {
        errorEl,
        recoveryEl,
        recoveryLabelEl,
        recoverySelectEl,
        recoveryNoteEl,
    } = getComposeUiElements(scope);

    if (!errorEl || !recoveryEl || !recoverySelectEl || !recoveryNoteEl) {
        return;
    }

    if (!failureState) {
        errorEl.textContent = '';
        errorEl.classList.add('hidden');
        recoveryEl.classList.add('hidden');
        recoverySelectEl.innerHTML = '';
        recoverySelectEl.removeAttribute('aria-invalid');
        recoveryNoteEl.textContent = '';
        return;
    }

    errorEl.textContent = failureState.errorMessage;
    errorEl.classList.remove('hidden');

    const organisationOptions = Array.isArray(failureState.organisationOptions)
        ? failureState.organisationOptions
        : [];
    if (organisationOptions.length === 0) {
        recoveryEl.classList.add('hidden');
        recoverySelectEl.innerHTML = '';
        recoverySelectEl.removeAttribute('aria-invalid');
        recoveryNoteEl.textContent = '';
        return;
    }

    recoveryEl.classList.remove('hidden');
    if (recoveryLabelEl) {
        recoveryLabelEl.textContent = 'Send as member of:';
    }

    recoverySelectEl.innerHTML = '';
    if (organisationOptions.length > 1) {
        const placeholderOption = document.createElement('option');
        placeholderOption.value = '';
        placeholderOption.textContent = 'Choose an organisation';
        recoverySelectEl.appendChild(placeholderOption);
    }

    organisationOptions.forEach((option) => {
        const optionEl = document.createElement('option');
        optionEl.value = option.conceptId;
        optionEl.textContent = option.role
            ? `${option.name} (${option.role})`
            : option.name;
        recoverySelectEl.appendChild(optionEl);
    });

    recoverySelectEl.value = failureState.selectedOrganisationConceptId || '';
    recoverySelectEl.removeAttribute('aria-invalid');
    recoveryNoteEl.textContent = organisationOptions.length > 1
        ? 'Choose a shared organisation and send again. This will not change the window organisation.'
        : 'Retry in this shared organisation. This will not change the window organisation.';
}

function setComposeFailureState(scope, nextState) {
    const normalisedState = normaliseComposeFailureState(nextState);

    if (scope === COMPOSE_SCOPE_REPLY) {
        _replySendFailureState = normalisedState;
    } else if (scope === COMPOSE_SCOPE_NEW_MESSAGE) {
        _newMessageSendFailureState = normalisedState;
    }

    renderComposeFailureUi(scope);
}

function resetComposeFailureUi(scope) {
    setComposeFailureState(scope, null);
}

function updateComposeRecoverySelection(scope, organisationConceptId) {
    const failureState = getComposeFailureState(scope);
    if (!failureState) {
        return;
    }

    setComposeFailureState(scope, {
        ...failureState,
        selectedOrganisationConceptId: organisationConceptId,
    });
}

function buildSendPayload({
    scope,
    recipientIds,
    content,
    visibleDraft = content,
    visibleRecipient,
    threadId = '',
}) {
    const failureState = getComposeFailureState(scope);
    const organisationOptions = Array.isArray(failureState?.organisationOptions)
        ? failureState.organisationOptions
        : [];
    const selectedOrganisationConceptId = String(
        failureState?.selectedOrganisationConceptId || (scope === COMPOSE_SCOPE_REPLY ? _exchange?.organisation_concept_id : '') || '',
    ).trim();

    if (organisationOptions.length > 0 && !selectedOrganisationConceptId) {
        const { recoverySelectEl } = getComposeUiElements(scope);
        if (recoverySelectEl) {
            recoverySelectEl.setAttribute('aria-invalid', 'true');
            recoverySelectEl.focus();
        }
        return null;
    }

    const deliveryIdempotencyKey = getOrCreateDeliveryIdempotencyKey({
        scope,
        recipientIds,
        content,
        visibleDraft,
        visibleRecipient,
        organisationConceptId: selectedOrganisationConceptId,
        threadId,
    });

    return {
        recipient_ids: recipientIds,
        content,
        delivery_idempotency_key: deliveryIdempotencyKey,
        ...(selectedOrganisationConceptId
            ? { organisation_concept_id: selectedOrganisationConceptId }
            : {}),
    };
}

/**
 * Handle sending a reply in the current conversation.
 */
async function handleSendReply() {
    if (isComposePending(COMPOSE_SCOPE_REPLY)) {
        return;
    }

    if (_exchange?.other_participant_ids?.length > 1) return;
    if (!_currentConversationUserId) {
        showToast('No conversation selected', 'warning');
        return;
    }

    const msgInput = _messagesContainer?.querySelector('#messageInput');
    if (!msgInput) return;

    const visibleDraft = msgInput.value;
    const content = visibleDraft.trim();
    if (!content) {
        showToast('Please enter a message', 'warning');
        return;
    }

    const payload = buildSendPayload({
        scope: COMPOSE_SCOPE_REPLY,
        recipientIds: [_currentConversationUserId],
        content,
        visibleDraft,
        threadId: resolveCurrentReplyThreadId(),
    });
    if (!payload) {
        return;
    }

    const submittedConversationUserId = _currentConversationUserId;
    const submittedGeneration = _exchangeGeneration;
    setComposePending(COMPOSE_SCOPE_REPLY, true);
    try {
        const result = await postJsonDetailed('/api/messages/', payload);
        if (submittedGeneration !== _exchangeGeneration) { document.dispatchEvent(new CustomEvent('von:conversation-contribution')); return; }

        resetComposeFailureUi(COMPOSE_SCOPE_REPLY);
        setDeliveryAttempt(COMPOSE_SCOPE_REPLY, null);
        const activeReplyInput = _messagesContainer?.querySelector('#messageInput');
        if (
            _currentConversationUserId === submittedConversationUserId
            && activeReplyInput?.value.trim() === content
        ) {
            activeReplyInput.value = '';
            resizeCompactDraft(activeReplyInput);
        }
        _exchangeDrafts.delete(exchangeScope());
        document.dispatchEvent(new CustomEvent('von:conversation-contribution'));
        showToast('Message sent', 'success');

        if (_exchange && result.data?.conversation && result.data.conversation.session_id !== _exchange.session_id) {
            // An explicit organisation recovery starts its own exchange; it
            // must not rewrite the identity of the earlier conversation.
            const { selectMessageConversation } = await import('./conversationCatalogue.js');
            await selectMessageConversation(result.data.conversation);
        } else await loadConversation(_currentConversationUserId);

    } catch (err) {
        console.error('[messagePanel] Failed to send message:', err);
        if (submittedGeneration === _exchangeGeneration) recordComposeSendFailure(COMPOSE_SCOPE_REPLY, err);
    } finally {
        setComposePending(COMPOSE_SCOPE_REPLY, false);
    }
}

/**
 * Show the new message modal.
 */
function showNewMessageModal() {
    const modal = _messagesContainer?.querySelector('#newMessageModal');
    if (modal) {
        resetComposeFailureUi(COMPOSE_SCOPE_NEW_MESSAGE);
        modal.classList.remove('hidden');
        const recipientInput = modal.querySelector('#newMessageRecipient');
        if (recipientInput) {
            recipientInput.focus();
        }
    }
}

/**
 * Hide the new message modal.
 */
function hideNewMessageModal({ force = false } = {}) {
    if (_newMessageSendPending && !force) {
        return;
    }

    if (composerOpenedFromChat) {
        document.getElementById('conversationWorkspace')?.classList.remove('show-message-exchange');
        document.body.classList.remove('viewing-message-exchange');
        composerOpenedFromChat = false;
    }

    const modal = _messagesContainer?.querySelector('#newMessageModal');
    if (modal) {
        modal.classList.add('hidden');
        resetComposeFailureUi(COMPOSE_SCOPE_NEW_MESSAGE);
        // Clear inputs
        const recipientInput = modal.querySelector('#newMessageRecipient');
        const contentInput = modal.querySelector('#newMessageContent');
        if (recipientInput) recipientInput.value = '';
        if (contentInput) contentInput.value = '';
        if (_newMessageDeliveryAttempt) {
            setDeliveryAttempt(COMPOSE_SCOPE_NEW_MESSAGE, null);
        }
    }
}

/**
 * Handle sending a new message from the modal.
 */
async function handleSendNewMessage() {
    if (isComposePending(COMPOSE_SCOPE_NEW_MESSAGE)) {
        return;
    }

    const recipientInput = _messagesContainer?.querySelector('#newMessageRecipient');
    const contentInput = _messagesContainer?.querySelector('#newMessageContent');

    if (!recipientInput || !contentInput) return;

    const visibleRecipient = recipientInput.value;
    const visibleDraft = contentInput.value;
    const recipient = visibleRecipient.trim();
    const content = visibleDraft.trim();

    if (!recipient) {
        showToast('Please enter a recipient', 'warning');
        recipientInput.focus();
        return;
    }

    if (!content) {
        showToast('Please enter a message', 'warning');
        contentInput.focus();
        return;
    }

    // Ensure recipient has #V# prefix
    const recipientId = recipient.startsWith('#V#') ? recipient : `#V#${recipient}`;
    const payload = buildSendPayload({
        scope: COMPOSE_SCOPE_NEW_MESSAGE,
        recipientIds: [recipientId],
        content,
        visibleDraft,
        visibleRecipient,
    });
    if (!payload) {
        return;
    }

    const submittedGeneration = _exchangeGeneration;
    setComposePending(COMPOSE_SCOPE_NEW_MESSAGE, true);
    try {
        const result = await postJsonDetailed('/api/messages/', payload);
        if (submittedGeneration !== _exchangeGeneration) { document.dispatchEvent(new CustomEvent('von:conversation-contribution')); return; }

        resetComposeFailureUi(COMPOSE_SCOPE_NEW_MESSAGE);
        setDeliveryAttempt(COMPOSE_SCOPE_NEW_MESSAGE, null);
        document.dispatchEvent(new CustomEvent('von:conversation-contribution'));
        showToast('Message sent', 'success');
        hideNewMessageModal({ force: true });

        if (result.data?.conversation) {
            const { selectMessageConversation } = await import('./conversationCatalogue.js');
            await selectMessageConversation(result.data.conversation);
        } else {
            // Compatibility with an older server response.
            _exchange = null;
            await loadMessageThreads();
            await selectConversation(recipientId);
        }

    } catch (err) {
        console.error('[messagePanel] Failed to send new message:', err);
        if (submittedGeneration === _exchangeGeneration) recordComposeSendFailure(COMPOSE_SCOPE_NEW_MESSAGE, err);
    } finally {
        setComposePending(COMPOSE_SCOPE_NEW_MESSAGE, false);
    }
}

/**
 * Load unread message count and update badge.
 */
export async function loadUnreadCount() {
    try {
        const response = await getJson('/api/messages/unread/count');
        _unreadCount = response.unread_count || 0;
        updateUnreadBadge();
    } catch (err) {
        console.error('[messagePanel] Failed to load unread count:', err);
    }
}

/**
 * Update the unread message badge.
 */
function updateUnreadBadge() {
    const badge = document.getElementById('unreadMessageBadge');
    if (badge) {
        if (_unreadCount > 0) {
            badge.textContent = _unreadCount > 99 ? '99+' : String(_unreadCount);
            badge.classList.remove('hidden');
        } else {
            badge.classList.add('hidden');
        }
    }
}

/**
 * Get the current unread count.
 */
export function getUnreadCount() {
    return _unreadCount;
}

export function __testOnly_resetMessagesViewportPosition() {
    resetMessagesViewportPosition();
}

// Utility functions

/**
 * Format a user concept ID for display.
 */
function formatUserName(userId) {
    if (!userId) return 'Unknown';
    // Remove #V# prefix and format
    let name = userId.replace(/^#V#/, '');
    // Replace underscores with spaces and title case
    name = name.replace(/_/g, ' ');
    return name.split(' ').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
}

/**
 * Get initials from a display name.
 */
function getInitials(name) {
    if (!name) return '?';
    const parts = name.split(' ');
    if (parts.length >= 2) {
        return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
    }
    return name.substring(0, 2).toUpperCase();
}

/**
 * Format a timestamp for display.
 */
function formatTime(date) {
    if (!date || !(date instanceof Date) || isNaN(date.getTime())) return '';

    const now = new Date();
    const diffMs = now - date;
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

    if (diffDays === 0) {
        // Today - show time only
        return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } else if (diffDays === 1) {
        return 'Yesterday';
    } else if (diffDays < 7) {
        // This week - show day name
        return date.toLocaleDateString([], { weekday: 'short' });
    } else {
        // Older - show date
        return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
    }
}

/**
 * Simple HTML escape.
 */
function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}
