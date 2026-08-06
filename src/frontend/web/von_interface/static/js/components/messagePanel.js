/**
 * Message Panel Component (JVNAUTOSCI-1071)
 *
 * Provides UI for viewing and sending direct messages between users.
 * Messages are stored as Vontology concepts and accessed via REST API.
 */

import { getJson, postJson, postJsonDetailed } from '../apiService.js';
import { hydrateConceptCartouchesInRoot } from '../utils/selectConceptByIdHandler.js';
import { cartouchifyElementText } from '../utils/textDecorator.js';
import { showToast } from '../utils/toast.js';
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

const COMPOSE_SCOPE_REPLY = 'reply';
const COMPOSE_SCOPE_NEW_MESSAGE = 'newMessage';

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
                    <span id="conversationTitle" class="conversation-title">Select a conversation</span>
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
                        <textarea id="messageInput" class="message-input" placeholder="Type your message..." rows="3"></textarea>
                        <button id="sendMessageBtn" class="send-message-btn">Send</button>
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
                           placeholder="Enter recipient concept ID (e.g., #V#username)">
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
                    <button id="sendNewMessage" class="modal-send-btn">Send</button>
                </div>
            </div>
        </div>
    `;

    _replySendFailureState = null;
    _newMessageSendFailureState = null;

    // Attach event listeners
    attachMessagesEventListeners();
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
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                handleSendReply();
            }
        });
    }

    const replyRecoverySelect = _messagesContainer.querySelector('#messageComposeRecoverySelect');
    if (replyRecoverySelect) {
        replyRecoverySelect.addEventListener('change', (event) => {
            updateComposeRecoverySelection(COMPOSE_SCOPE_REPLY, event.target?.value || '');
        });
    }

    // New message modal
    const closeModalBtn = _messagesContainer.querySelector('#closeNewMessageModal');
    if (closeModalBtn) {
        closeModalBtn.addEventListener('click', hideNewMessageModal);
    }

    const cancelBtn = _messagesContainer.querySelector('#cancelNewMessage');
    if (cancelBtn) {
        cancelBtn.addEventListener('click', hideNewMessageModal);
    }

    const sendNewBtn = _messagesContainer.querySelector('#sendNewMessage');
    if (sendNewBtn) {
        sendNewBtn.addEventListener('click', handleSendNewMessage);
    }

    const newMessageRecoverySelect = _messagesContainer.querySelector('#newMessageRecoverySelect');
    if (newMessageRecoverySelect) {
        newMessageRecoverySelect.addEventListener('change', (event) => {
            updateComposeRecoverySelection(COMPOSE_SCOPE_NEW_MESSAGE, event.target?.value || '');
        });
    }

    const recipientInput = _messagesContainer.querySelector('#newMessageRecipient');
    if (recipientInput) {
        recipientInput.addEventListener('input', () => {
            resetComposeFailureUi(COMPOSE_SCOPE_NEW_MESSAGE);
        });
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
        renderThreadList();
        autoOpenUserId = resolveAutoOpenThreadUserId();

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
            <div class="message-thread-item" data-user-id="${escapeHtml(userId)}"
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
        item.addEventListener('click', () => {
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

    // Load the conversation
    await loadConversation(userId);
}

/**
 * Load messages for a conversation with a specific user.
 */
async function loadConversation(userId) {
    if (_isLoading) return;

    _isLoading = true;
    const contentEl = _messagesContainer?.querySelector('#messageViewContent');

    if (contentEl) {
        contentEl.innerHTML = '<div class="loading">Loading messages...</div>';
    }

    try {
        const response = await getJson(`/api/messages/conversation/${encodeURIComponent(userId)}?limit=50`);
        _currentMessages = response.messages || [];
        _currentUserId = response.current_user_id || null;
        await renderMessages();

        // Mark messages as read
        const unreadIds = _currentMessages
            .filter(m => {
                const readBy = m.concept_data?.read_by || [];
                const recipientIds = m.relationships?.['#V#has_recipient'] || [];
                return Boolean(
                    _currentUserId
                    && recipientIds.includes(_currentUserId)
                    && !readBy.includes(_currentUserId)
                );
            })
            .map(m => m.concept_id);

        if (unreadIds.length > 0) {
            try {
                await postJson('/api/messages/read/bulk', { message_ids: unreadIds });
                await loadUnreadCount();
            } catch (e) {
                console.warn('[messagePanel] Failed to mark messages as read:', e);
            }
        }

    } catch (err) {
        console.error('[messagePanel] Failed to load conversation:', err);
        if (contentEl) {
            contentEl.innerHTML = '<div class="message-error">Failed to load messages</div>';
        }
    } finally {
        _isLoading = false;
    }
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
            <div class="message-bubble ${isSent ? 'sent' : 'received'}">
                ${subject ? `<div class="message-subject">${escapeHtml(subject)}</div>` : ''}
                <div class="message-content">${escapeHtml(content)}</div>
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
                </div>
            </div>
        `;
    });

    html += '</div>';
    contentEl.innerHTML = html;

    const messageContentEls = Array.from(contentEl.querySelectorAll('.message-list .message-content'));
    messageContentEls.forEach((messageContentEl, index) => {
        const content = _currentMessages[index]?.concept_data?.content_fallback || '';
        cartouchifyElementText(messageContentEl, content);
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
    await hydrateConceptCartouchesInRoot(contentEl);

    // Scroll to bottom
    contentEl.scrollTop = contentEl.scrollHeight;
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

    const conceptId = String(option.concept_id || option.conceptId || '').trim();
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
    let normalisedState = null;

    if (nextState && typeof nextState === 'object') {
        const organisationOptions = Array.isArray(nextState.organisationOptions)
            ? nextState.organisationOptions.filter(Boolean)
            : [];
        const selectedOrganisationConceptId = String(nextState.selectedOrganisationConceptId || '').trim();
        const knownOrganisationIds = new Set(
            organisationOptions.map((option) => String(option.conceptId || '').trim()).filter(Boolean),
        );

        normalisedState = {
            errorMessage: String(nextState.errorMessage || '').trim() || 'Failed to send message',
            organisationOptions,
            selectedOrganisationConceptId: organisationOptions.length === 1
                ? organisationOptions[0].conceptId
                : (knownOrganisationIds.has(selectedOrganisationConceptId) ? selectedOrganisationConceptId : ''),
        };
    }

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

function buildSendPayload({ scope, recipientIds, content }) {
    const failureState = getComposeFailureState(scope);
    const organisationOptions = Array.isArray(failureState?.organisationOptions)
        ? failureState.organisationOptions
        : [];
    const selectedOrganisationConceptId = String(
        failureState?.selectedOrganisationConceptId || '',
    ).trim();

    if (organisationOptions.length > 0 && !selectedOrganisationConceptId) {
        const { recoverySelectEl } = getComposeUiElements(scope);
        if (recoverySelectEl) {
            recoverySelectEl.setAttribute('aria-invalid', 'true');
            recoverySelectEl.focus();
        }
        return null;
    }

    return {
        recipient_ids: recipientIds,
        content,
        ...(selectedOrganisationConceptId
            ? { organisation_concept_id: selectedOrganisationConceptId }
            : {}),
    };
}

/**
 * Handle sending a reply in the current conversation.
 */
async function handleSendReply() {
    if (!_currentConversationUserId) {
        showToast('No conversation selected', 'warning');
        return;
    }

    const msgInput = _messagesContainer?.querySelector('#messageInput');
    if (!msgInput) return;

    const content = msgInput.value.trim();
    if (!content) {
        showToast('Please enter a message', 'warning');
        return;
    }

    const payload = buildSendPayload({
        scope: COMPOSE_SCOPE_REPLY,
        recipientIds: [_currentConversationUserId],
        content,
    });
    if (!payload) {
        return;
    }

    try {
        await postJsonDetailed('/api/messages/', payload);

        resetComposeFailureUi(COMPOSE_SCOPE_REPLY);
        msgInput.value = '';
        showToast('Message sent', 'success');

        // Reload conversation
        await loadConversation(_currentConversationUserId);

    } catch (err) {
        console.error('[messagePanel] Failed to send message:', err);
        setComposeFailureState(COMPOSE_SCOPE_REPLY, buildMessageSendFailureState(err));
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
function hideNewMessageModal() {
    const modal = _messagesContainer?.querySelector('#newMessageModal');
    if (modal) {
        modal.classList.add('hidden');
        resetComposeFailureUi(COMPOSE_SCOPE_NEW_MESSAGE);
        // Clear inputs
        const recipientInput = modal.querySelector('#newMessageRecipient');
        const contentInput = modal.querySelector('#newMessageContent');
        if (recipientInput) recipientInput.value = '';
        if (contentInput) contentInput.value = '';
    }
}

/**
 * Handle sending a new message from the modal.
 */
async function handleSendNewMessage() {
    const recipientInput = _messagesContainer?.querySelector('#newMessageRecipient');
    const contentInput = _messagesContainer?.querySelector('#newMessageContent');

    if (!recipientInput || !contentInput) return;

    const recipient = recipientInput.value.trim();
    const content = contentInput.value.trim();

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
    });
    if (!payload) {
        return;
    }

    try {
        await postJsonDetailed('/api/messages/', payload);

        resetComposeFailureUi(COMPOSE_SCOPE_NEW_MESSAGE);
        showToast('Message sent', 'success');
        hideNewMessageModal();

        // Reload threads and select the new conversation
        await loadMessageThreads();
        await selectConversation(recipientId);

    } catch (err) {
        console.error('[messagePanel] Failed to send new message:', err);
        setComposeFailureState(COMPOSE_SCOPE_NEW_MESSAGE, buildMessageSendFailureState(err));
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
