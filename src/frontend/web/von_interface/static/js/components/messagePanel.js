/**
 * Message Panel Component (JVNAUTOSCI-1071)
 *
 * Provides UI for viewing and sending direct messages between users.
 * Messages are stored as Vontology concepts and accessed via REST API.
 */

import { getJson, postJson } from '../apiService.js';
import { showToast } from '../utils/toast.js';

// Message panel state
let _messagesContainer = null;
let _messageListEl = null;
let _threads = [];
let _currentMessages = [];
let _currentConversationUserId = null;
let _isLoading = false;
let _unreadCount = 0;

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

    // Load messages/threads
    await loadMessageThreads();
    await loadUnreadCount();
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
                    <h3>Conversations</h3>
                    <button id="newMessageBtn" class="new-message-btn" title="New message">✏️</button>
                </div>
                <div id="messageThreadList" class="message-thread-list">
                    <div class="loading">Loading conversations...</div>
                </div>
            </div>
            <div class="messages-main">
                <div id="messageViewHeader" class="message-view-header hidden">
                    <span id="conversationTitle" class="conversation-title">Select a conversation</span>
                    <button id="refreshMessagesBtn" class="message-refresh-btn" title="Refresh">🔄</button>
                </div>
                <div id="messageViewContent" class="message-view-content">
                    <div class="message-empty-state">
                        <span class="message-empty-icon">✉️</span>
                        <p>Select a conversation to view messages</p>
                        <p class="message-empty-hint">Or start a new conversation</p>
                    </div>
                </div>
                <div id="messageComposeArea" class="message-compose-area hidden">
                    <textarea id="messageInput" class="message-input" placeholder="Type your message..." rows="3"></textarea>
                    <button id="sendMessageBtn" class="send-message-btn">Send</button>
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
                </div>
                <div class="message-modal-footer">
                    <button id="cancelNewMessage" class="modal-cancel-btn">Cancel</button>
                    <button id="sendNewMessage" class="modal-send-btn">Send</button>
                </div>
            </div>
        </div>
    `;

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
}

/**
 * Load message threads (conversations) for the current user.
 */
async function loadMessageThreads() {
    if (_isLoading) return;

    _isLoading = true;
    const threadListEl = _messagesContainer?.querySelector('#messageThreadList');

    if (threadListEl) {
        threadListEl.innerHTML = '<div class="loading">Loading conversations...</div>';
    }

    try {
        const response = await getJson('/api/messages/threads?limit=20');
        _threads = response.threads || [];
        renderThreadList();

    } catch (err) {
        console.error('[messagePanel] Failed to load threads:', err);
        if (threadListEl) {
            threadListEl.innerHTML = '<div class="message-error">Failed to load conversations</div>';
        }
    } finally {
        _isLoading = false;
    }
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
        const preview = lastContent.length > 50
            ? lastContent.substring(0, 50) + '...'
            : lastContent;

        // Format time
        const lastTime = lastMessage.created_at
            ? formatTime(new Date(lastMessage.created_at))
            : '';

        const userId = Array.isArray(otherUsers) ? otherUsers[0] : otherUsers;

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
                    <span class="thread-count">${messageCount}</span>
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
        renderMessages();

        // Mark messages as read
        const unreadIds = _currentMessages
            .filter(m => {
                const readBy = m.concept_data?.read_by || [];
                // Check if current user hasn't read it yet
                // We need to get current user ID - for now, check if not sender
                const senderId = m.relationships?.['#V#has_sender']?.[0];
                return senderId !== userId; // Simplified: assume we're the other party
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

/**
 * Render messages in the conversation view.
 */
function renderMessages() {
    const contentEl = _messagesContainer?.querySelector('#messageViewContent');
    if (!contentEl) return;

    if (_currentMessages.length === 0) {
        contentEl.innerHTML = `
            <div class="message-empty-state">
                <span class="message-empty-icon">💬</span>
                <p>No messages yet</p>
                <p class="message-empty-hint">Send the first message!</p>
            </div>
        `;
        return;
    }

    let html = '<div class="message-list">';

    _currentMessages.forEach(msg => {
        const senderId = msg.relationships?.['#V#has_sender']?.[0] || '';
        const content = msg.concept_data?.content_fallback || '';
        const timestamp = msg.created_at ? new Date(msg.created_at) : null;

        // Determine if this is a sent or received message
        // For now, compare with the other user in conversation
        const isSent = senderId !== _currentConversationUserId;

        html += `
            <div class="message-bubble ${isSent ? 'sent' : 'received'}">
                <div class="message-content">${escapeHtml(content)}</div>
                <div class="message-meta">
                    <span class="message-time">${timestamp ? formatTime(timestamp) : ''}</span>
                </div>
            </div>
        `;
    });

    html += '</div>';
    contentEl.innerHTML = html;

    // Scroll to bottom
    contentEl.scrollTop = contentEl.scrollHeight;
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

    try {
        await postJson('/api/messages/', {
            recipient_ids: [_currentConversationUserId],
            content: content,
        });

        msgInput.value = '';
        showToast('Message sent', 'success');

        // Reload conversation
        await loadConversation(_currentConversationUserId);

    } catch (err) {
        console.error('[messagePanel] Failed to send message:', err);
        showToast('Failed to send message', 'error');
    }
}

/**
 * Show the new message modal.
 */
function showNewMessageModal() {
    const modal = _messagesContainer?.querySelector('#newMessageModal');
    if (modal) {
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

    try {
        await postJson('/api/messages/', {
            recipient_ids: [recipientId],
            content: content,
        });

        showToast('Message sent', 'success');
        hideNewMessageModal();

        // Reload threads and select the new conversation
        await loadMessageThreads();
        selectConversation(recipientId);

    } catch (err) {
        console.error('[messagePanel] Failed to send new message:', err);
        showToast('Failed to send message', 'error');
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
