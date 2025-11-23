// Chat Tab Module
import { annotateTurn, getUserContext } from './apiService.js';
import { elements, renderSpanSuggestions } from './domUtils.js';
import { annotateElementText } from './utils/textDecorator.js';

// Store LLM debug data for each turn
const llmDebugData = new Map();
// Track conversation turns for Markdown export and state resets
const transcriptTurns = [];

// Persist sender/message pairs for transcript exports
function recordTranscriptTurn(sender, message, options = {}) {
    if (!sender && !message) {
        return;
    }

    transcriptTurns.push({
        sender,
        message,
        turnId: options.turnId || null,
        isHistory: !!options.isHistory,
        timestamp: options.timestamp || new Date().toISOString()
    });
}

function indicateClipboardResult(button, originalContent, isSuccess) {
    if (!button) {
        return;
    }

    const successClass = 'success-feedback';
    const errorClass = 'error-feedback';
    button.classList.remove(successClass, errorClass);
    button.innerHTML = isSuccess ? '<span class="btn-icon">✓</span>' : '<span class="btn-icon">!</span>';
    button.classList.add(isSuccess ? successClass : errorClass);
    setTimeout(() => {
        button.innerHTML = originalContent;
        button.classList.remove(successClass, errorClass);
    }, 1500);
}

function copyTextFallback(text) {
    if (typeof document === 'undefined') {
        return false;
    }

    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    textarea.setAttribute('readonly', '');
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();

    let successful = false;
    try {
        if (typeof document.execCommand === 'function') {
            successful = document.execCommand('copy');
        }
    } catch (err) {
        console.error('[chatTab] Failed to copy text via fallback:', err);
        successful = false;
    } finally {
        document.body.removeChild(textarea);
    }

    return successful;
}

async function updateHistoryLength() {
    try {
        const response = await fetch('/von/history/length');
        const data = await response.json();

        if (response.ok) {
            const historyLength = data.history_length || 0;
            const authenticated = data.authenticated !== undefined ? data.authenticated : true; // Default to true for backward compatibility
            const historyLengthElement = document.getElementById('chat-history-length');
            if (historyLengthElement) {
                // Display "unauthenticated" if not authenticated, otherwise show the count
                if (!authenticated) {
                    historyLengthElement.textContent = 'History: unauthenticated';
                } else {
                    historyLengthElement.textContent = `History: ${historyLength}`;
                }
            }
        } else {
            console.error('Failed to load chat history length:', data.error);
        }
    } catch (error) {
        console.error('Error loading chat history length:', error);
    }
}

async function loadChatHistory() {
    try {
        const response = await fetch('/von/history');
        const data = await response.json();

        if (response.ok && data.history && Array.isArray(data.history)) {
            const scrollableField = document.getElementById('scrollableField');
            if (!scrollableField) {
                console.error('scrollableField not found');
                return;
            }

            // Clear existing messages and cached state before rehydration
            scrollableField.innerHTML = '';
            transcriptTurns.length = 0;
            llmDebugData.clear();

            // Append historical messages
            data.history.forEach((msg, index) => {
                if (msg.role === 'user' || msg.role === 'assistant') {
                    const turnId = `history-${msg.role}-${index}`;
                    const label = msg.role === 'user' ? 'User' : 'Von';
                    appendMessage(label, msg.content, turnId, false, true, msg.timestamp); // false = no LLM debug, true = isHistory
                }
            });

            console.log(`Loaded ${data.history.length} historical messages`);

            // Scroll to bottom after loading history
            setTimeout(() => {
                scrollableField.scrollTop = scrollableField.scrollHeight;
            }, 0);
        } else {
            console.log('No chat history to load or empty history');
        }
    } catch (error) {
        console.error('Error loading chat history:', error);
    }
}

export function initializeChatTab() {
    console.log("Initializing chat tab...");

    const sendButton = document.getElementById('sendButton');
    const resetButton = document.getElementById('resetButton');
    const promptInput = document.getElementById('promptInput');
    const scrollableField = document.getElementById('scrollableField');
    const annotationToggle = document.getElementById('annotationToggle');
    const exportConversationJsonBtn = document.getElementById('exportConversationJsonBtn');
    const exportConversationMarkdownBtn = document.getElementById('exportConversationMarkdownBtn');

    if (!sendButton || !resetButton || !promptInput) {
        console.error("Chat tab elements not found");
        return;
    }

    // Initialize LLM debug popup handlers
    initializeLlmDebugPopup();

    // Initialize export conversation button
    if (exportConversationJsonBtn) {
        exportConversationJsonBtn.addEventListener('click', handleExportConversationJson);
    }
    if (exportConversationMarkdownBtn) {
        exportConversationMarkdownBtn.addEventListener('click', handleExportConversationMarkdown);
    }

    // Load annotation toggle state from localStorage (default: false)
    const savedState = localStorage.getItem('annotationToggleEnabled');
    if (annotationToggle) {
        annotationToggle.checked = savedState === 'true';
        annotationToggle.addEventListener('change', (e) => {
            localStorage.setItem('annotationToggleEnabled', e.target.checked);
            console.log('[annotations] Toggle changed to:', e.target.checked);
        });
    }

    // Add event listeners
    sendButton.addEventListener('click', handleSendPrompt);
    resetButton.addEventListener('click', handleResetContext);

    // Add Enter key support for prompt input
    promptInput.addEventListener('keypress', function (event) {
        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            handleSendPrompt();
        }
    });

    loadChatHistory();
    updateHistoryLength();
    console.log("Chat tab initialized successfully");
}

async function handleSendPrompt() {
    const promptInput = document.getElementById('promptInput');
    const scrollableField = document.getElementById('scrollableField');
    const loadingIndicator = document.getElementById('loadingIndicator');
    const sendButton = document.getElementById('sendButton');

    const promptText = promptInput.value.trim();

    if (!promptText) {
        alert('Please enter a prompt.');
        return;
    }

    // Show loading indicator and disable send button
    if (loadingIndicator) {
        loadingIndicator.style.display = 'inline-flex';
        loadingIndicator.setAttribute('aria-hidden', 'false');
    }
    sendButton.disabled = true;

    // Create turn IDs for user and assistant
    const userTurnId = `u-${Date.now()}`;
    const assistantTurnId = `a-${Date.now()}`;

    // Add user message to chat with turnId
    appendMessage('User', promptText, userTurnId);
    // Fire-and-forget annotate user turn (do not await) - only if toggle is enabled
    const annotationToggle = document.getElementById('annotationToggle');
    if (annotationToggle && annotationToggle.checked) {
        try {
            annotateTurn({
                conversation_id: elements.conversationId || 'local',
                turn_id: userTurnId,
                speaker: 'user',
                text: promptText
            }).catch(e => console.info('[annotations] user annotate error', e));
        } catch (e) { console.info('[annotations] annotate user failed', e); }
    }

    // Clear input
    promptInput.value = ''; try {
        // Get user context from localStorage to send to backend
        const userContext = getUserContext();

        const response = await fetch('/von/generate', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                prompt: promptText,
                user_id: userContext.user_id,
                org_id: userContext.org_id,
                language: userContext.language
            })
        });

        // Defensive: some tests or environments may provide a non-standard fetch
        // mock that doesn't return a Response-like object. Guard before calling
        // `response.json()` so we surface a friendly server error message instead
        // of falling through to the network-error catch path.
        if (!response || typeof response.json !== 'function') {
            appendMessage('Error', 'Server error');
            return;
        }

        const data = await response.json();
        console.log('[chatTab] fetch response.ok=', response.ok, 'data=', data);

        if (response.ok) {
            // Store LLM debug data if available
            if (data.llm_debug) {
                llmDebugData.set(assistantTurnId, data.llm_debug);
                console.log('[chatTab] Stored LLM debug data for turn:', assistantTurnId);
            }

            // Append assistant message with turnId and llm_debug flag
            appendMessage('Von', data.response, assistantTurnId, !!data.llm_debug);
            // Annotate assistant turn and render suggestions when returned - only if toggle is enabled
            const annotationToggle = document.getElementById('annotationToggle');
            if (annotationToggle && annotationToggle.checked) {
                try {
                    annotateTurn({
                        conversation_id: elements.conversationId || 'local',
                        turn_id: assistantTurnId,
                        speaker: 'assistant',
                        text: data.response
                    }).then((resp) => {
                        console.info('[annotations] annotateTurn response (chatTab)', resp);
                        if (resp && resp.suggestions) {
                            renderSpanSuggestions(assistantTurnId, resp.suggestions);
                        }
                    }).catch(e => console.info('[annotations] assistant annotate error', e));
                } catch (e) { console.info('[annotations] annotate assistant failed', e); }
            }
        } else {
            // Store LLM debug data if available even on error
            const errorTurnId = `e-${Date.now()}`;
            if (data.llm_debug) {
                llmDebugData.set(errorTurnId, data.llm_debug);
                console.log('[chatTab] Stored LLM debug data for error turn:', errorTurnId);
            }
            appendMessage('Error', data.error || 'An error occurred', errorTurnId, !!data.llm_debug);
        }
    } catch (error) {
        console.error('Error:', error);
        appendMessage('Error', 'Network error occurred');
    } finally {
        if (loadingIndicator) {
            loadingIndicator.style.display = 'none';
            loadingIndicator.setAttribute('aria-hidden', 'true');
        }
        sendButton.disabled = false;
        updateHistoryLength();
    }
}

async function handleResetContext() {
    const scrollableField = document.getElementById('scrollableField');
    try {
        const response = await fetch('/von/reset', {
            method: 'POST'
        });

        const data = await response.json();

        if (response.ok) {
            // Clear the scrollable field with new format
            scrollableField.innerHTML = '';

            // Add a success message
            const resetMessage = document.createElement('div');
            resetMessage.style.cssText = 'text-align: center; padding: 20px; color: #28a745; font-style: italic; background-color: #f8f9fa; border-radius: 8px; margin-bottom: 10px;';
            resetMessage.textContent = 'Context reset successfully. You can start a new conversation.';
            scrollableField.appendChild(resetMessage);

            transcriptTurns.length = 0;
            llmDebugData.clear();
        } else {
            alert('Error resetting context: ' + (data.error || 'Unknown error'));
        }
    } catch (error) {
        console.error('Error:', error);
        alert('Network error occurred while resetting context');
    }
}

function formatChatTimestamp(isoString) {
    if (!isoString) return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const date = new Date(isoString);
    const now = new Date();
    const diffMs = now - date;
    const diffDays = diffMs / (1000 * 60 * 60 * 24);

    // Check if it's the same calendar day
    const isToday = now.getDate() === date.getDate() &&
        now.getMonth() === date.getMonth() &&
        now.getFullYear() === date.getFullYear();

    if (isToday) {
        // Today: Time only
        return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } else if (diffDays < 7) {
        // Within a week: Day + Time
        return date.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
    } else {
        // Older: Date + Time
        return date.toLocaleString([], { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    }
}

function appendMessage(sender, message, turnId, hasLlmDebug = false, isHistory = false, timestampStr = null) {
    const scrollableField = document.getElementById('scrollableField');
    const displayTimestamp = formatChatTimestamp(timestampStr);

    if (sender === 'Von' || (isHistory && sender === 'assistant')) {
        // Create a container for Von's response with image
        const messageContainer = document.createElement('div');
        messageContainer.style.cssText = 'display: flex; align-items: flex-start; margin-bottom: 15px; padding: 10px; background-color: #f8f9fa; border-radius: 8px; border-left: 4px solid #007bff;';

        // Add Von's image
        const vonImage = document.createElement('img');
        vonImage.src = '/static/VonImageBig.png';
        vonImage.alt = 'Von';
        vonImage.style.cssText = 'width: 40px; height: 40px; border-radius: 50%; margin-right: 12px; flex-shrink: 0; object-fit: cover;';

        // Add message content
        const messageContent = document.createElement('div');
        messageContent.style.cssText = 'flex: 1; line-height: 1.5;';

        const messageHeader = document.createElement('div');
        messageHeader.style.cssText = 'font-weight: bold; color: #007bff; margin-bottom: 5px; font-size: 0.9em; display: flex; align-items: center; gap: 8px;';

        const headerText = document.createElement('span');
        headerText.textContent = `Von • ${displayTimestamp}`;
        messageHeader.appendChild(headerText);

        // Add LLM debug button if debug data available
        if (hasLlmDebug && turnId) {
            const llmDebugButton = document.createElement('button');
            llmDebugButton.className = 'btn-mini llm-debug-button';
            llmDebugButton.textContent = 'LLM ⓘ';
            llmDebugButton.title = 'Show LLM interaction details';
            llmDebugButton.dataset.turnId = turnId;
            llmDebugButton.addEventListener('click', () => showLlmDebugPopup(turnId));
            messageHeader.appendChild(llmDebugButton);
        }

        const messageText = document.createElement('div');
        messageText.style.cssText = 'color: #333; white-space: pre-wrap;';
        annotateElementText(messageText, message);

        messageContent.appendChild(messageHeader);
        messageContent.appendChild(messageText);
        messageContainer.appendChild(vonImage);
        messageContainer.appendChild(messageContent);

        if (turnId) messageContainer.dataset.turnId = turnId;
        scrollableField.appendChild(messageContainer);
    } else {
        // For user messages and errors, use simpler styling
        const messageContainer = document.createElement('div');
        messageContainer.style.cssText = 'margin-bottom: 15px; padding: 10px; background-color: #fff; border-radius: 8px; border-left: 4px solid #28a745;';

        if (sender === 'Error') {
            messageContainer.style.borderLeftColor = '#dc3545';
            messageContainer.style.backgroundColor = '#fff5f5';
        }

        const messageHeader = document.createElement('div');
        messageHeader.style.cssText = 'font-weight: bold; margin-bottom: 5px; font-size: 0.9em; display: flex; align-items: center; gap: 8px;';
        messageHeader.style.color = sender === 'Error' ? '#dc3545' : '#28a745';

        const headerText = document.createElement('span');
        headerText.textContent = `${sender} • ${displayTimestamp}`;
        messageHeader.appendChild(headerText);

        // Add LLM debug button for errors if debug data available
        if (sender === 'Error' && hasLlmDebug && turnId) {
            const llmDebugButton = document.createElement('button');
            llmDebugButton.className = 'btn-mini llm-debug-button';
            llmDebugButton.textContent = 'LLM ⓘ';
            llmDebugButton.title = 'Show what was sent to LLM before error';
            llmDebugButton.dataset.turnId = turnId;
            llmDebugButton.addEventListener('click', () => showLlmDebugPopup(turnId));
            messageHeader.appendChild(llmDebugButton);
        }

        const messageText = document.createElement('div');
        messageText.style.cssText = 'color: #333; white-space: pre-wrap;';
        annotateElementText(messageText, message);

        messageContainer.appendChild(messageHeader);
        messageContainer.appendChild(messageText);
        if (turnId) messageContainer.dataset.turnId = turnId;
        scrollableField.appendChild(messageContainer);
    }

    recordTranscriptTurn(sender, message, { turnId, isHistory, timestamp: timestampStr || new Date().toISOString() });

    // Auto-scroll to bottom
    if (!isHistory) {
        scrollableField.scrollTop = scrollableField.scrollHeight;
    }
}

// Initialize LLM debug popup handlers
function initializeLlmDebugPopup() {
    const popup = document.getElementById('chatLlmDebugPopup');
    const closeBtn = document.getElementById('closeChatLlmDebug');
    const copyBtn = document.getElementById('copyChatLlmDebugJson');

    if (!popup || !closeBtn || !copyBtn) {
        console.warn('[chatTab] LLM debug popup elements not found');
        return;
    }

    // Close button handler
    closeBtn.addEventListener('click', () => {
        popup.classList.add('hidden');
        popup.setAttribute('aria-hidden', 'true');
    });

    // Copy JSON button handler
    copyBtn.addEventListener('click', () => {
        const currentDebugData = popup.dataset.currentDebugData;
        if (currentDebugData) {
            navigator.clipboard.writeText(currentDebugData)
                .then(() => {
                    const originalText = copyBtn.textContent;
                    copyBtn.textContent = 'Copied!';
                    setTimeout(() => { copyBtn.textContent = originalText; }, 1500);
                })
                .catch(err => console.error('[chatTab] Failed to copy:', err));
        }
    });

    // Close on escape key
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !popup.classList.contains('hidden')) {
            closeBtn.click();
        }
    });
}

// Show LLM debug popup for a specific turn
function showLlmDebugPopup(turnId) {
    const debugData = llmDebugData.get(turnId);
    if (!debugData) {
        console.warn('[chatTab] No debug data for turn:', turnId);
        return;
    }

    const popup = document.getElementById('chatLlmDebugPopup');
    const metaDiv = document.getElementById('chatLlmDebugMeta');
    const messagesPre = document.getElementById('chatLlmDebugMessages');
    const responsePre = document.getElementById('chatLlmDebugResponse');
    const toolsSection = document.getElementById('chatLlmDebugToolsSection');
    const toolsPre = document.getElementById('chatLlmDebugTools');

    if (!popup || !metaDiv || !messagesPre || !responsePre || !toolsSection || !toolsPre) {
        console.error('[chatTab] LLM debug popup elements missing');
        return;
    }

    // Display metadata
    const hasError = debugData.error !== undefined;

    // Build metadata HTML with context stats
    let metadataHtml = `<strong>Model:</strong> ${debugData.model || 'Unknown'}<br>`;
    metadataHtml += `<strong>Message Count:</strong> ${debugData.messages?.length || 0}`;

    // Add context statistics if available
    if (debugData.context_stats) {
        const sentStats = debugData.context_stats.sent_to_llm;
        const storedStats = debugData.context_stats.stored_context;

        if (sentStats) {
            metadataHtml += `<br><strong>Context Sent to LLM:</strong> ${sentStats.total_messages} msgs, ${sentStats.total_chars.toLocaleString()} chars`;
            if (sentStats.largest_message?.chars > 0) {
                metadataHtml += ` (largest: ${sentStats.largest_message.role}, ${sentStats.largest_message.chars.toLocaleString()} chars)`;
            }
        }

        if (storedStats) {
            metadataHtml += `<br><strong>Stored Context:</strong> ${storedStats.total_messages} msgs, ${storedStats.total_chars.toLocaleString()} chars`;
        }
    }

    // Add tool statistics if available
    if (debugData.tool_stats) {
        const toolStats = debugData.tool_stats;
        metadataHtml += `<br><strong>MCP Tools Used:</strong> ${toolStats.tool_count}`;
        if (toolStats.tool_count > 0) {
            metadataHtml += ` (${toolStats.total_chars.toLocaleString()} chars)`;
            if (toolStats.truncated_count > 0) {
                metadataHtml += ` <span style="color: #fd7e14;">⚠️ ${toolStats.truncated_count} truncated</span>`;
            }
        }
    }

    if (hasError) {
        metadataHtml += `<br><strong style="color: #dc3545;">Error:</strong> ${debugData.error}`;
    }

    metaDiv.innerHTML = metadataHtml;

    // Display messages
    try {
        messagesPre.textContent = JSON.stringify(debugData.messages || [], null, 2);
    } catch (e) {
        messagesPre.textContent = 'Error formatting messages';
    }

    // Display response or error message
    if (hasError) {
        responsePre.textContent = '(Error occurred before response was generated)';
        responsePre.style.color = '#dc3545';
    } else {
        responsePre.textContent = debugData.response || '(no response)';
        responsePre.style.color = '';
    }

    // Display tool invocations if any
    if (debugData.tool_invocations && debugData.tool_invocations.length > 0) {
        toolsSection.classList.remove('hidden');
        try {
            toolsPre.textContent = JSON.stringify(debugData.tool_invocations, null, 2);
        } catch (e) {
            toolsPre.textContent = 'Error formatting tool invocations';
        }
    } else {
        toolsSection.classList.add('hidden');
    }

    // Store full data for copy function
    popup.dataset.currentDebugData = JSON.stringify(debugData, null, 2);

    // Show popup
    popup.classList.remove('hidden');
    popup.setAttribute('aria-hidden', 'false');
}

// Handle export full conversation JSON
function handleExportConversationJson() {
    const button = document.getElementById('exportConversationJsonBtn');
    if (!button) return;
    const originalContent = button.innerHTML;

    // Collect all debug data from the Map
    const conversationData = {
        metadata: {
            exported_at: new Date().toISOString(),
            total_turns: llmDebugData.size,
            format_version: '1.0'
        },
        turns: []
    };

    // Convert Map entries to array and sort by turnId timestamp
    const sortedEntries = Array.from(llmDebugData.entries()).sort((a, b) => {
        // Extract timestamp from turnId (format: 'a-1234567890' or 'u-1234567890')
        const getTimestamp = (turnId) => {
            const parts = turnId.split('-');
            return parts.length > 1 ? parseInt(parts[1], 10) : 0;
        };
        return getTimestamp(a[0]) - getTimestamp(b[0]);
    });

    // Build conversation data
    for (const [turnId, debugData] of sortedEntries) {
        conversationData.turns.push({
            turn_id: turnId,
            timestamp: new Date(debugData.timestamp || Date.now()).toISOString(),
            debug_data: debugData
        });
    }

    // Convert to JSON string
    const jsonString = JSON.stringify(conversationData, null, 2);

    const markSuccess = (message) => {
        console.log(message, conversationData.turns.length, 'turns');
        indicateClipboardResult(button, originalContent, true);
    };

    const markFailure = (message, err) => {
        console.error(message, err);
        indicateClipboardResult(button, originalContent, false);
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(jsonString)
            .then(() => {
                markSuccess('[chatTab] Exported conversation JSON to clipboard:');
            })
            .catch((err) => {
                console.error('[chatTab] Failed to copy conversation JSON via clipboard API:', err);
                if (copyTextFallback(jsonString)) {
                    markSuccess('[chatTab] Exported conversation JSON (fallback):');
                } else {
                    markFailure('[chatTab] Failed to copy conversation JSON (fallback):', err);
                }
            });
    } else if (copyTextFallback(jsonString)) {
        markSuccess('[chatTab] Exported conversation JSON (fallback):');
    } else {
        markFailure('[chatTab] Clipboard export unavailable and fallback failed:', new Error('Clipboard unsupported'));
    }
}

function handleExportConversationMarkdown() {
    const button = document.getElementById('exportConversationMarkdownBtn');
    if (!button) {
        return;
    }

    const originalContent = button.innerHTML;

    if (transcriptTurns.length === 0) {
        console.warn('[chatTab] No conversation turns available for Markdown export');
        indicateClipboardResult(button, originalContent, false);
        return;
    }

    const markdownLines = [];
    markdownLines.push('# Conversation with Von');
    markdownLines.push('');
    markdownLines.push(`_Exported at ${new Date().toISOString()}_`);
    markdownLines.push('');

    transcriptTurns.forEach((turn, index) => {
        const label = turn.sender || 'Message';
        const timestampSuffix = turn.timestamp ? ` _(at ${new Date(turn.timestamp).toLocaleString()})_` : '';
        const content = typeof turn.message === 'string'
            ? turn.message.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
            : String(turn.message ?? '');

        markdownLines.push(`**${label}:**${timestampSuffix}`);
        markdownLines.push('');
        markdownLines.push(content);
        if (index < transcriptTurns.length - 1) {
            markdownLines.push('');
        }
    });

    const markdownString = markdownLines.join('\n');

    const markSuccess = (message) => {
        console.log(message, transcriptTurns.length, 'turns');
        indicateClipboardResult(button, originalContent, true);
    };

    const markFailure = (message, err) => {
        console.error(message, err);
        indicateClipboardResult(button, originalContent, false);
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(markdownString)
            .then(() => {
                markSuccess('[chatTab] Exported conversation Markdown to clipboard:');
            })
            .catch((err) => {
                console.error('[chatTab] Failed to copy conversation Markdown via clipboard API:', err);
                if (copyTextFallback(markdownString)) {
                    markSuccess('[chatTab] Exported conversation Markdown (fallback):');
                } else {
                    markFailure('[chatTab] Failed to copy conversation Markdown (fallback):', err);
                }
            });
    } else if (copyTextFallback(markdownString)) {
        markSuccess('[chatTab] Exported conversation Markdown (fallback):');
    } else {
        markFailure('[chatTab] Clipboard export unavailable and fallback failed:', new Error('Clipboard unsupported'));
    }
}

// Expose updateHistoryLength globally so it can be called after login
if (typeof window !== 'undefined') {
    window.updateHistoryLength = updateHistoryLength;
}

// Export functions for testing
export const sendMessage = handleSendPrompt;
export const resetChat = handleResetContext;
export const handleChatResponse = appendMessage;
export const exportConversationJson = handleExportConversationJson;
export const exportConversationMarkdown = handleExportConversationMarkdown;
export { updateHistoryLength };

