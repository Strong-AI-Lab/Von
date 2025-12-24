// Chat Tab Module
import { annotateTurn, getUserContext } from './apiService.js';
import { initializeConceptAutocomplete } from './components/conceptAutocomplete.js';
import { initializePromptCartoucheOverlay, normaliseVontologyIdsForBackend } from './components/promptCartoucheOverlay.js';
import { elements, renderSpanSuggestions } from './domUtils.js';
import { detectMarkdown, renderMarkdownViaServer } from './markdownUtils.js';
import { selectBestNameForContext } from './utils/nameSelection.js';
import { cartouchifyElementText, cartouchifyVontologyTokensInElement } from './utils/textDecorator.js';

// Store LLM debug data for each turn
const llmDebugData = new Map();
// Track conversation turns for Markdown export and state resets
const transcriptTurns = [];
let historySegmentsShown = 1;
let totalHistorySegments = 1;

function escapeHtml(value) {
    const text = String(value ?? '');
    return text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// Cache concept metadata used for cartouches in chat transcript.
// Map<fullId, { name: string, kind: string } | null>
const chatConceptMetaCache = new Map();
// Map<fullId, Promise<meta|null>> for in-flight lookups.
const chatConceptMetaPending = new Map();

function isMarkdownProducingModel(model) {
    if (!model) {
        return false;
    }
    const modelName = String(model).trim().toLowerCase();
    return modelName.startsWith('gpt-5.2');
}

function shouldRenderMarkdownForAssistant(message, debugData) {
    const text = String(message ?? '');

    if (isMarkdownProducingModel(debugData?.model)) {
        // Treat as markdown-friendly even when detection is ambiguous.
        return true;
    }

    return detectMarkdown(text);
}

function updateChatRenderModeBadge(badgeEl, messageTextEl, details = {}) {
    if (!badgeEl || !messageTextEl) {
        return;
    }

    const renderMode = String(messageTextEl?.dataset?.renderMode || 'text');
    const modeLabel = renderMode === 'rendered' ? 'Rendered' : 'Text';
    badgeEl.textContent = `View: ${modeLabel}`;

    const shouldRenderMarkdown = details.shouldRenderMarkdown === true;
    const canRenderMarkdown = details.canRenderMarkdown === true;
    const model = details.model ? String(details.model) : '';

    badgeEl.classList.toggle('rendered', renderMode === 'rendered');
    badgeEl.classList.toggle('text', renderMode !== 'rendered');

    const reasons = [];
    if (model) {
        reasons.push(`model=${model}`);
    }
    reasons.push(`renderMode=${renderMode}`);

    if (renderMode !== 'rendered') {
        if (!canRenderMarkdown) {
            reasons.push('no-toggle (markdown not detected)');
        }
        if (!shouldRenderMarkdown) {
            reasons.push('shouldRenderMarkdownForAssistant=false');
        }
    }

    badgeEl.title = reasons.join(' • ');
}

async function renderChatMarkdownIntoContainer(container, text) {
    const markdownText = String(text ?? '');
    const html = await renderMarkdownViaServer(markdownText);

    // Cache rendered HTML so we can toggle without re-fetching.
    try {
        container.dataset.renderedHtml = html;
    } catch (_) {
        // Ignore dataset failures.
    }

    // If the user has toggled to raw text while this request was in-flight, do not overwrite.
    if (container?.dataset?.renderMode === 'text') {
        return;
    }

    container.innerHTML = html;
    try {
        container.dataset.renderMode = 'rendered';
    } catch (_) {
        // Ignore.
    }

    // Preserve clickable #V# tokens, but never inside code blocks.
    cartouchifyVontologyTokensInElement(container, { skipSelectors: ['pre', 'code', 'a'], allowStandaloneCodeTokens: true });
    hydrateChatConceptCartouches(container);
}

function setVonMessageRenderMode(messageTextEl, mode, originalText, debugData) {
    if (!messageTextEl) {
        return;
    }

    // Hard override: chat transcript containers can inherit centring/boldness.
    // Keep message text consistently left-aligned in both raw and rendered modes.
    messageTextEl.style.textAlign = 'left';
    messageTextEl.style.fontWeight = '400';

    const raw = String(originalText ?? '');
    const nextMode = mode === 'text' ? 'text' : 'rendered';

    try {
        messageTextEl.dataset.originalText = raw;
        messageTextEl.dataset.renderMode = nextMode;
    } catch (_) {
        // Ignore.
    }

    if (nextMode === 'text') {
        // Raw view: show the original model output exactly, no markdown and no cartouches.
        messageTextEl.classList.remove('markdown-rendered', 'chat-markdown');
        messageTextEl.style.whiteSpace = 'pre-wrap';
        messageTextEl.textContent = raw;
        return;
    }

    // Rendered view: use cached HTML if available, else re-render via server.
    messageTextEl.classList.add('markdown-rendered', 'chat-markdown');
    messageTextEl.style.whiteSpace = 'normal';

    const cachedHtml = messageTextEl?.dataset?.renderedHtml;
    if (cachedHtml) {
        messageTextEl.innerHTML = cachedHtml;
        cartouchifyVontologyTokensInElement(messageTextEl, { skipSelectors: ['pre', 'code', 'a'], allowStandaloneCodeTokens: true });
        hydrateChatConceptCartouches(messageTextEl);
        return;
    }

    // Keep a safe plaintext fallback while the request is in-flight.
    messageTextEl.textContent = raw;
    void renderChatMarkdownIntoContainer(messageTextEl, raw).catch((err) => {
        console.error('[chatTab] Server markdown render failed; falling back to plain text:', err);
        if (messageTextEl?.dataset?.renderMode === 'rendered') {
            messageTextEl.textContent = raw;
        }
    });
}

function renderAssistantMessageContent(container, message, debugData) {
    const text = String(message ?? '');
    const shouldRenderMarkdown = shouldRenderMarkdownForAssistant(text, debugData);

    // Hard override: chat transcript containers can inherit centring/boldness.
    container.style.textAlign = 'left';
    container.style.fontWeight = '400';

    if (!shouldRenderMarkdown) {
        try {
            container.dataset.originalText = text;
            container.dataset.renderMode = 'text';
        } catch (_) {
            // Ignore.
        }
        container.classList.remove('markdown-rendered', 'chat-markdown');
        container.style.whiteSpace = 'pre-wrap';
        cartouchifyElementText(container, text);
        hydrateChatConceptCartouches(container);
        return;
    }

    container.classList.add('markdown-rendered', 'chat-markdown');
    container.style.whiteSpace = 'normal';

    try {
        container.dataset.originalText = text;
        container.dataset.renderMode = 'rendered';
    } catch (_) {
        // Ignore.
    }

    // Render asynchronously so we can rely on the server-side markdown/sanitisation.
    // Keep a safe plaintext fallback in-place while the request is in-flight.
    container.textContent = text;
    void renderChatMarkdownIntoContainer(container, text).catch((err) => {
        console.error('[chatTab] Server markdown render failed; falling back to plain text:', err);
        container.textContent = text;
    });
}

function formatKindLabel(kind) {
    const k = (kind || '').toString().toLowerCase();
    if (k === 'predicate') return 'Predicate';
    if (k === 'individual') return 'Individual';
    return 'Type';
}

function normaliseKindClass(kind) {
    const k = (kind || '').toString().toLowerCase();
    if (k === 'predicate' || k === 'individual' || k === 'type') {
        return k;
    }
    return 'type';
}

async function fetchConceptMetaForChat(fullId) {
    try {
        // Prefer an exact lookup rather than fuzzy search: avoids incorrect labels.
        const nodeUrl = `/vontology/api/vontology/node_content?identifier=${encodeURIComponent(fullId)}`;
        const nodeRes = await fetch(nodeUrl, { cache: 'no-store' });
        if (nodeRes.ok) {
            const node = await nodeRes.json();
            const preferredLanguage = getUserContext()?.language || 'en-NZ';
            const rawNames =
                node?.raw_doc?.names ||
                node?.node?.raw_doc?.names ||
                node?.names ||
                node?.node?.names ||
                null;
            const bestName = selectBestNameForContext(rawNames, preferredLanguage);
            const name =
                bestName ||
                node?.display_name ||
                node?.name ||
                node?.node?.display_name ||
                node?.node?.name ||
                fullId;
            const kind = node?.kind || node?.node?.kind || 'type';
            return { name: String(name), kind: String(kind) };
        }

        // Fallback to search endpoint if node_content is unavailable.
        const url = `/von/api/search?q=${encodeURIComponent(fullId)}&limit=8`;
        const res = await fetch(url, { cache: 'no-store' });
        if (!res.ok) return null;

        const data = await res.json();
        const results = Array.isArray(data?.results) ? data.results : [];
        const match = results.find(r => r && r.id === fullId);
        if (!match || !match.id) return null;

        return {
            name: match.name || match.id,
            kind: match.kind || 'type'
        };
    } catch (err) {
        console.debug('[chatTab] fetchConceptMetaForChat failed', err);
        return null;
    }
}

function updateCartoucheElement(cartoucheEl, meta) {
    if (!cartoucheEl) return;
    if (!meta) {
        cartoucheEl.classList.add('unresolved');
        return;
    }

    const nameEl = cartoucheEl.querySelector('.vontology-cartouche-name');
    const kindEl = cartoucheEl.querySelector('.vontology-cartouche-kind');

    if (nameEl) {
        nameEl.textContent = meta.name || (cartoucheEl.dataset.fullConceptId || '');
    }
    if (kindEl) {
        const kindClass = normaliseKindClass(meta.kind);
        kindEl.className = `vontology-cartouche-kind ${kindClass}`;
        kindEl.textContent = formatKindLabel(meta.kind);
    }
}

function hydrateChatConceptCartouches(root) {
    if (!root || typeof root.querySelectorAll !== 'function') {
        return;
    }

    const cartouches = Array.from(root.querySelectorAll('.vontology-cartouche[data-full-concept-id]'));
    if (cartouches.length === 0) {
        return;
    }

    const uniqueIds = new Set();
    for (const el of cartouches) {
        const fullId = el.dataset.fullConceptId;
        if (fullId) {
            uniqueIds.add(fullId);
        }
    }

    for (const fullId of uniqueIds) {
        if (chatConceptMetaCache.has(fullId)) {
            const cached = chatConceptMetaCache.get(fullId);
            cartouches
                .filter(el => el.dataset.fullConceptId === fullId)
                .forEach(el => updateCartoucheElement(el, cached));
            continue;
        }

        if (!chatConceptMetaPending.has(fullId)) {
            const p = fetchConceptMetaForChat(fullId).then((meta) => {
                chatConceptMetaCache.set(fullId, meta);
                chatConceptMetaPending.delete(fullId);
                return meta;
            });
            chatConceptMetaPending.set(fullId, p);
        }

        chatConceptMetaPending.get(fullId)
            .then((meta) => {
                cartouches
                    .filter(el => el.dataset.fullConceptId === fullId)
                    .forEach(el => updateCartoucheElement(el, meta));
            })
            .catch(() => {
                // Ignore lookup failures; leave placeholders.
            });
    }
}

function deriveLlmDebugWarnings(debugData) {
    const warnings = [];
    if (!debugData || typeof debugData !== 'object') {
        return warnings;
    }

    if (typeof debugData.error === 'string' && debugData.error.trim()) {
        warnings.push(`Backend error: ${debugData.error.trim()}`);
    }

    const auxCalls = Array.isArray(debugData.aux_llm_calls) ? debugData.aux_llm_calls : [];
    for (const call of auxCalls) {
        if (!call || typeof call !== 'object') {
            continue;
        }

        const callType = typeof call.type === 'string' ? call.type : '';

        if (callType === 'missing_tool_call_classifier') {
            const injectionMode = typeof call.prompt_injection_mode === 'string' ? call.prompt_injection_mode : '';
            if (injectionMode === 'append') {
                warnings.push(
                    'Missing tool-call detector prompt did not include `{response}` placeholder; response was appended.'
                );
            }

            const verdict = typeof call.response_preview === 'string' ? call.response_preview.trim().toLowerCase() : '';
            if (verdict && !(verdict.startsWith('yes') || verdict.startsWith('no'))) {
                warnings.push('Missing tool-call classifier returned an unexpected verdict (not yes/no).');
            }

            const modelRaw = typeof call.model_raw === 'string' ? call.model_raw : '';
            const modelResolved = typeof call.model_resolved === 'string' ? call.model_resolved : '';
            if (modelRaw.startsWith('#V#') && !modelResolved) {
                warnings.push('Missing tool-call classifier model could not be resolved from ontology ID.');
            }
        }

        if (typeof call.error === 'string' && call.error.trim()) {
            warnings.push(call.error.trim());
        }
    }

    return Array.from(new Set(warnings));
}

function createChatDebugWarningIndicator(warnings) {
    if (!Array.isArray(warnings) || warnings.length === 0) {
        return null;
    }

    const container = document.createElement('span');
    container.className = 'llm-debug-warning-container';
    container.setAttribute('role', 'img');
    container.setAttribute('aria-label', 'Warnings available for this LLM debug turn');

    const indicator = document.createElement('span');
    indicator.className = 'llm-debug-warning-indicator';
    indicator.innerHTML = `
        <svg class="llm-debug-warning-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M1 21h22L12 2 1 21z" />
            <path d="M12 9v5" class="llm-debug-warning-icon-mark" />
            <circle cx="12" cy="17" r="1" class="llm-debug-warning-icon-mark" />
        </svg>
    `;

    // Create custom tooltip
    const tooltip = document.createElement('div');
    tooltip.className = 'llm-debug-warning-tooltip';
    tooltip.textContent = warnings.join('\n');

    // Show/hide tooltip on hover
    container.addEventListener('mouseenter', () => {
        tooltip.style.display = 'block';
    });
    container.addEventListener('mouseleave', () => {
        tooltip.style.display = 'none';
    });

    container.appendChild(indicator);
    container.appendChild(tooltip);
    return container;
}

let activeChatRequest = null;

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

function updateHistoryBanner() {
    const banner = document.getElementById('historyBanner');
    const bannerText = document.getElementById('historyBannerText');
    const loadButton = document.getElementById('loadOlderHistoryBtn');

    if (!banner || !bannerText || !loadButton) {
        return;
    }

    const remainingSegments = Math.max(totalHistorySegments - historySegmentsShown, 0);

    if (remainingSegments > 0) {
        const segmentLabel = remainingSegments === 1 ? 'segment' : 'segments';
        bannerText.textContent = `Earlier conversation ${segmentLabel} available (${remainingSegments})`;
        banner.classList.remove('hidden');
        loadButton.disabled = false;
    } else {
        banner.classList.add('hidden');
        loadButton.disabled = true;
    }
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

// Delete exchange (top-level so event handlers can access it)
function deleteExchange(turnId, isUserMessage) {
    const scrollableField = document.getElementById('scrollableField');
    if (!scrollableField) return;

    // Find message containers (may be paired user + assistant)
    const messageContainers = scrollableField.querySelectorAll(`[data-turn-id="${turnId}"]`);
    if (messageContainers.length === 0) return;

    // Get message text for warning check
    let totalLength = 0;
    messageContainers.forEach(container => {
        const text = container.textContent || '';
        totalLength += text.length;
    });

    const isSignificant = totalLength > 200;
    const exchangeType = isUserMessage ? 'user message' : 'Von response';
    let confirmMessage = `Delete this ${exchangeType}?`;

    if (isSignificant) {
        confirmMessage += `\n\nWARNING: This exchange contains significant content (${totalLength} characters).`;
    }

    if (!confirm(confirmMessage)) {
        return;
    }

    messageContainers.forEach(container => {
        container.remove();
    });

    // Remove from transcript
    const index = transcriptTurns.findIndex(t => t.turnId === turnId);
    if (index !== -1) {
        transcriptTurns.splice(index, 1);
    }

    // If it was the only exchange, reload previous context
    const remainingMessages = scrollableField.querySelectorAll('.message-container').length;
    if (remainingMessages === 0) {
        console.log('[chatTab] Last exchange deleted, reloading previous context...');
        loadChatHistory({ segments: historySegmentsShown });
    }

    // Update history counts
    updateHistoryBanner();
    updateHistoryLength();
}

async function updateHistoryLength() {
    try {
        const response = await fetch('/von/history/length');
        const data = await response.json();

        if (response.ok) {
            const historyLength = data.history_length || 0;
            const authenticated = data.authenticated !== undefined ? data.authenticated : true;
            const historyLengthElement = document.getElementById('chat-history-length');
            if (historyLengthElement) {
                if (!authenticated) {
                    historyLengthElement.textContent = 'History: unauthenticated';
                } else {
                    const contextCount = transcriptTurns.length;
                    historyLengthElement.textContent = `History: ${contextCount} | ${historyLength}`;
                }
            }
        } else {
            console.error('Failed to load chat history length:', data.error);
        }
    } catch (error) {
        console.error('Error loading chat history length:', error);
    }
}

async function loadChatHistory(options = {}) {
    const {
        segments,
        scrollToBottom = true,
        preserveScroll = false,
        showResetNotice = false
    } = options;

    const scrollableField = document.getElementById('scrollableField');
    if (!scrollableField) {
        console.error('scrollableField not found');
        return false;
    }

    const requestedSegments = Number.isInteger(segments) && segments > 0 ? segments : historySegmentsShown;
    const segmentCount = Math.max(requestedSegments || 1, 1);

    // Get user context to ensure we can load history even if session is new
    const userContext = getUserContext();
    const params = new URLSearchParams({ segments: segmentCount.toString() });
    if (userContext && userContext.user_id) {
        params.append('user_id', userContext.user_id);
    }
    try {
        const response = await fetch(`/von/history?${params.toString()}`);
        const data = await response.json();

        console.log(`[chatTab] loadChatHistory response: ok=${response.ok}, segments=${data.segments_returned}, total=${data.total_segments}, history_len=${data.history ? data.history.length : 'undefined'}`);

        if (response.ok && data.history && Array.isArray(data.history)) {
            historySegmentsShown = Math.max(data.segments_returned || segmentCount, 0);
            totalHistorySegments = Math.max(data.total_segments || historySegmentsShown, historySegmentsShown);

            rehydrateHistory(scrollableField, data.history, {
                scrollToBottom,
                preserveScroll,
                showResetNotice
            });

            updateHistoryBanner();
            console.log(`Loaded ${data.history.length} historical messages across ${historySegmentsShown} segment(s)`);
            return true;
        }

        historySegmentsShown = Math.max(data?.segments_returned || 0, 0);
        totalHistorySegments = Math.max(data?.total_segments || historySegmentsShown, historySegmentsShown);
        updateHistoryBanner();
        console.log('No chat history to load or empty history');
        return false;
    } catch (error) {
        console.error('Error loading chat history:', error);
        updateHistoryBanner();
        return false;
    }
}

function rehydrateHistory(scrollableField, historyMessages, options = {}) {
    const {
        scrollToBottom = true,
        preserveScroll = false,
        showResetNotice = false
    } = options;

    const previousScrollHeight = scrollableField.scrollHeight;
    const previousScrollTop = scrollableField.scrollTop;

    scrollableField.innerHTML = '';
    transcriptTurns.length = 0;
    llmDebugData.clear();

    historyMessages.forEach((msg, index) => {
        if (msg.role === 'user' || msg.role === 'assistant') {
            const turnId = `history-${msg.role}-${index}`;
            const label = msg.role === 'user' ? 'User' : 'Von';

            // Restore debug data before rendering so markdown gating can see model info.
            const hasDebugData = msg.role === 'assistant' && !!msg.llm_debug_data;
            if (hasDebugData) {
                llmDebugData.set(turnId, msg.llm_debug_data);
            }

            appendMessage(label, msg.content, turnId, hasDebugData, true, msg.timestamp);
        }
    });

    if (showResetNotice) {
        appendResetNotice(scrollableField);
    }

    const requestFrame = (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function')
        ? window.requestAnimationFrame.bind(window)
        : (cb) => setTimeout(cb, 0);

    requestFrame(() => {
        if (preserveScroll) {
            const newScrollHeight = scrollableField.scrollHeight;
            const delta = newScrollHeight - previousScrollHeight;
            scrollableField.scrollTop = previousScrollTop + Math.max(delta, 0);
        } else if (scrollToBottom) {
            scrollableField.scrollTop = scrollableField.scrollHeight;
        }
        // Update history display to reflect new context count
        updateHistoryLength();
    });
}

function initializeHistoryControls() {
    const loadButton = document.getElementById('loadOlderHistoryBtn');
    if (!loadButton) {
        return;
    }

    loadButton.addEventListener('click', async () => {
        if (totalHistorySegments === 0 || historySegmentsShown >= totalHistorySegments) {
            return;
        }

        loadButton.disabled = true;
        loadButton.classList.add('loading');

        try {
            const targetSegments = totalHistorySegments > 0
                ? Math.min(historySegmentsShown + 1, totalHistorySegments)
                : historySegmentsShown + 1;
            await loadChatHistory({
                segments: targetSegments,
                scrollToBottom: false,
                preserveScroll: true
            });
        } catch (error) {
            console.error('Error loading older history:', error);
        } finally {
            loadButton.classList.remove('loading');
            updateHistoryBanner();
        }
    });
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
    initializeHistoryControls();
    updateHistoryBanner();

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

    ensureAbortButtonBound();

    // Add Enter key support for prompt input
    promptInput.addEventListener('keypress', function (event) {
        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            handleSendPrompt();
        }
    });

    // Initialize concept autocomplete for #V# trigger
    initializeConceptAutocomplete(promptInput);

    // Render non-trigger (#V\u200B#...) concept tokens as cartouches in the prompt.
    initializePromptCartoucheOverlay(promptInput);

    loadChatHistory();
    updateHistoryLength();
    console.log("Chat tab initialized successfully");
}

function setThinkingState(isThinking) {
    const loadingIndicator = document.getElementById('loadingIndicator');
    const abortButton = document.getElementById('abortButton');
    const sendButton = document.getElementById('sendButton');

    if (loadingIndicator) {
        loadingIndicator.style.display = isThinking ? 'inline-flex' : 'none';
        loadingIndicator.setAttribute('aria-hidden', isThinking ? 'false' : 'true');
    }

    if (sendButton) {
        sendButton.disabled = !!isThinking;
    }

    if (abortButton) {
        abortButton.setAttribute('aria-hidden', isThinking ? 'false' : 'true');
    }
}

function restorePromptEditingState(request) {
    const promptInput = document.getElementById('promptInput');
    if (!promptInput || !request) {
        return;
    }

    promptInput.value = request.promptRaw || '';
    promptInput.dispatchEvent(new Event('input', { bubbles: true }));

    try {
        const valueLength = promptInput.value.length;
        const start = Number.isInteger(request.selectionStart) ? request.selectionStart : valueLength;
        const end = Number.isInteger(request.selectionEnd) ? request.selectionEnd : start;
        promptInput.setSelectionRange(Math.min(start, valueLength), Math.min(end, valueLength));
    } catch (err) {
        // Selection range is best-effort; some environments may not support it.
    }

    try {
        promptInput.focus();
    } catch (_) {
        // Ignore focus errors.
    }
}

function abortActiveChatRequest() {
    if (!activeChatRequest) {
        return;
    }

    const request = activeChatRequest;
    request.aborted = true;
    activeChatRequest = null;

    try {
        request.abortController?.abort();
    } catch (_) {
        // Ignore abort errors.
    }

    setThinkingState(false);
    restorePromptEditingState(request);
}

function ensureAbortButtonBound() {
    const abortButton = document.getElementById('abortButton');
    if (!abortButton) {
        return;
    }

    if (abortButton.dataset.bound === '1') {
        return;
    }
    abortButton.dataset.bound = '1';

    abortButton.addEventListener('click', () => {
        abortActiveChatRequest();
    });
}

async function handleSendPrompt() {
    const promptInput = document.getElementById('promptInput');
    const scrollableField = document.getElementById('scrollableField');
    const sendButton = document.getElementById('sendButton');

    if (activeChatRequest) {
        return;
    }

    ensureAbortButtonBound();

    const promptRaw = promptInput.value;
    const selectionStart = typeof promptInput.selectionStart === 'number' ? promptInput.selectionStart : null;
    const selectionEnd = typeof promptInput.selectionEnd === 'number' ? promptInput.selectionEnd : null;
    const promptForSend = normaliseVontologyIdsForBackend(promptRaw);
    const promptText = promptForSend.trim();

    if (!promptText) {
        alert('Please enter a prompt.');
        return;
    }

    // Show loading indicator and disable send button
    setThinkingState(true);

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
    promptInput.value = '';
    promptInput.dispatchEvent(new Event('input', { bubbles: true }));
    let request = null;
    try {
        request = {
            abortController: new AbortController(),
            promptRaw,
            selectionStart,
            selectionEnd,
            aborted: false
        };
        activeChatRequest = request;

        // Get user context from localStorage to send to backend
        const userContext = getUserContext();

        const response = await fetch('/von/generate', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            signal: request.abortController.signal,
            body: JSON.stringify({
                prompt: promptText,
                user_id: userContext.user_id,
                org_id: userContext.org_id,
                language: userContext.language,
                gmail_profile: userContext.gmail_profile
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

        if (request.aborted) {
            return;
        }

        if (response.ok) {
            // Store LLM debug data if available
            if (data.llm_debug) {
                llmDebugData.set(assistantTurnId, data.llm_debug);
                console.log('[chatTab] Stored LLM debug data for turn:', assistantTurnId);
            }

            const fastpathMeta = data.fastpath || (data.llm_debug && data.llm_debug.fastpath) || null;

            // Append assistant message with turnId and llm_debug flag
            appendMessage('Von', data.response, assistantTurnId, !!data.llm_debug, false, null, fastpathMeta);
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
        if (request && (request.aborted || (error && error.name === 'AbortError'))) {
            return;
        }
        console.error('Error:', error);
        appendMessage('Error', 'Network error occurred');
    } finally {
        const isStillActive = activeChatRequest === request;
        if (isStillActive) {
            activeChatRequest = null;
            setThinkingState(false);
        }
        updateHistoryLength();
    }
}

async function handleResetContext() {
    try {
        const response = await fetch('/von/reset', {
            method: 'POST'
        });

        const data = await response.json();

        if (response.ok) {
            historySegmentsShown = 1;
            const loaded = await loadChatHistory({
                segments: 1,
                scrollToBottom: false,
                showResetNotice: true
            });

            if (!loaded) {
                const scrollableField = document.getElementById('scrollableField');
                appendResetNotice(scrollableField);
            }

            transcriptTurns.length = 0;
            llmDebugData.clear();
            updateHistoryLength();

            // Trigger immediate health poll to update RAG cartouche with new session context
            // Dispatch custom event that main.js health polling can listen for
            document.dispatchEvent(new CustomEvent('von:contextReset', {
                detail: { trigger: 'chat_reset' }
            }));
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

    // Check if it was yesterday
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    const isYesterday = yesterday.getDate() === date.getDate() &&
        yesterday.getMonth() === date.getMonth() &&
        yesterday.getFullYear() === date.getFullYear();

    if (isToday) {
        // Today: "Today HH:MM"
        return 'Today ' + date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } else if (isYesterday) {
        // Yesterday: "Yesterday HH:MM"
        return 'Yesterday ' + date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } else if (diffDays < 7) {
        // Within a week: Day + Time
        return date.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
    } else {
        // Older: Date + Time
        return date.toLocaleString([], { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    }
}

function appendMessage(sender, message, turnId, hasLlmDebug = false, isHistory = false, timestampStr = null, fastpathMeta = null) {
    const scrollableField = document.getElementById('scrollableField');
    if (!scrollableField) {
        console.error('[chatTab] appendMessage: scrollableField not found!');
        return;
    }

    try {
        const displayTimestamp = formatChatTimestamp(timestampStr);
        const historySuffix = isHistory ? ' (history)' : '';

        if (sender === 'Von' || (isHistory && sender === 'assistant')) {
            // Create a container for Von's response with image
            const messageContainer = document.createElement('div');
            messageContainer.style.cssText = 'display: flex; align-items: flex-start; margin-bottom: 15px; padding: 10px; background-color: #f8f9fa; border-radius: 8px; border-left: 4px solid #007bff;';
            messageContainer.className = 'message-container';
            // Add Von's image
            const vonImage = document.createElement('img');
            vonImage.src = '/static/VonImageBig.png';
            vonImage.alt = 'Von';
            vonImage.style.cssText = 'width: 40px; height: 40px; border-radius: 50%; margin-right: 12px; flex-shrink: 0; object-fit: cover;';

            // Add message content
            const messageContent = document.createElement('div');
            messageContent.style.cssText = 'flex: 1; line-height: 1.5;';

            const rawText = String(message ?? '');

            const messageHeader = document.createElement('div');
            messageHeader.style.cssText = 'font-weight: bold; color: #007bff; margin-bottom: 5px; font-size: 0.9em; display: flex; align-items: center; gap: 8px;';

            const headerText = document.createElement('span');
            headerText.textContent = `Von • ${displayTimestamp}${historySuffix}`;
            messageHeader.appendChild(headerText);

            // Add fast-path indicator if the server bypassed the LLM.
            const fastpath = fastpathMeta;
            if (fastpath && fastpath.bypassed_llm) {
                const badge = document.createElement('span');
                const fastpathName = fastpath.name ? String(fastpath.name) : 'fast-path';
                badge.textContent = `Fast-path: ${fastpathName}`;
                badge.title = 'Deterministic fast-path used; LLM was bypassed.';
                badge.style.cssText = 'display: inline-flex; align-items: center; padding: 1px 6px; border-radius: 10px; font-size: 0.8em; background: #fff3cd; border: 1px solid #ffeeba; color: #856404;';
                messageHeader.appendChild(badge);
            }

            const copyMarkdownButton = document.createElement('button');
            copyMarkdownButton.className = 'btn-mini chat-copy-markdown';
            copyMarkdownButton.textContent = 'MD';
            copyMarkdownButton.title = 'Copy this agent message as Markdown to clipboard';
            copyMarkdownButton.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();

                const originalContent = copyMarkdownButton.innerHTML;
                const markdownString = rawText.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
                if (!markdownString.trim()) {
                    indicateClipboardResult(copyMarkdownButton, originalContent, false);
                    return;
                }

                const markSuccess = () => indicateClipboardResult(copyMarkdownButton, originalContent, true);
                const markFailure = (err) => {
                    console.error('[chatTab] Failed to copy agent Markdown:', err);
                    indicateClipboardResult(copyMarkdownButton, originalContent, false);
                };

                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(markdownString)
                        .then(markSuccess)
                        .catch((err) => {
                            if (copyTextFallback(markdownString)) {
                                markSuccess();
                            } else {
                                markFailure(err);
                            }
                        });
                } else if (copyTextFallback(markdownString)) {
                    markSuccess();
                } else {
                    markFailure(new Error('Clipboard unsupported'));
                }
            });

            let copyButtonAppended = false;

            // Add LLM debug button if debug data available
            if (hasLlmDebug && turnId) {
                const debugData = llmDebugData.get(turnId);

                // Compact model badge (visible at-a-glance)
                if (debugData && debugData.model) {
                    const modelBadge = document.createElement('span');
                    modelBadge.className = 'chat-llm-model-badge';
                    modelBadge.textContent = String(debugData.model);
                    modelBadge.title = 'LLM model used for this turn';
                    messageHeader.appendChild(modelBadge);
                }

                const llmDebugButton = document.createElement('button');
                llmDebugButton.className = 'btn-mini llm-debug-button';
                llmDebugButton.textContent = 'LLM ⓘ';
                llmDebugButton.title = 'Show LLM interaction details';
                llmDebugButton.dataset.turnId = turnId;
                llmDebugButton.addEventListener('click', () => showLlmDebugPopup(turnId));
                messageHeader.appendChild(llmDebugButton);

                messageHeader.appendChild(copyMarkdownButton);
                copyButtonAppended = true;

                const warnings = deriveLlmDebugWarnings(debugData);
                const warningIndicator = createChatDebugWarningIndicator(warnings);
                if (warningIndicator) {
                    messageHeader.appendChild(warningIndicator);
                }
            }

            if (!copyButtonAppended) {
                messageHeader.appendChild(copyMarkdownButton);
            }

            const rightControls = document.createElement('span');
            rightControls.className = 'chat-message-controls';
            rightControls.style.cssText = 'margin-left: auto; display: inline-flex; align-items: center; gap: 6px;';

            // Render-mode badge: shows whether this message is in Rendered/Text mode.
            const renderModeBadge = document.createElement('span');
            renderModeBadge.className = 'chat-render-mode-badge';
            rightControls.appendChild(renderModeBadge);

            const messageText = document.createElement('div');
            messageText.style.cssText = 'color: #333; white-space: pre-wrap; text-align: left; font-weight: 400;';
            try {
                const debugData = turnId ? llmDebugData.get(turnId) : null;
                const canRenderMarkdown = shouldRenderMarkdownForAssistant(rawText, debugData);

                if (canRenderMarkdown) {
                    const toggleButton = document.createElement('button');
                    toggleButton.className = 'btn-mini chat-render-toggle';
                    toggleButton.textContent = 'Text';
                    toggleButton.title = 'Show the original (raw) text';
                    toggleButton.addEventListener('click', (e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        const currentMode = messageText?.dataset?.renderMode || 'rendered';
                        const nextMode = currentMode === 'text' ? 'rendered' : 'text';
                        setVonMessageRenderMode(messageText, nextMode, rawText, debugData);
                        toggleButton.textContent = nextMode === 'text' ? 'Rendered' : 'Text';
                        toggleButton.title = nextMode === 'text'
                            ? 'Show the rendered markdown view'
                            : 'Show the original (raw) text';

                        updateChatRenderModeBadge(renderModeBadge, messageText, {
                            canRenderMarkdown: true,
                            shouldRenderMarkdown: shouldRenderMarkdownForAssistant(rawText, debugData),
                            model: debugData?.model
                        });
                    });
                    rightControls.appendChild(toggleButton);
                }

                // Add delete button after the toggle so the close (✕) is right-most.
                if (turnId) {
                    const deleteButton = document.createElement('button');
                    deleteButton.className = 'btn-mini btn-delete-exchange';
                    deleteButton.type = 'button';
                    deleteButton.textContent = '✕';
                    deleteButton.title = 'Delete this exchange';
                    deleteButton.addEventListener('click', (e) => {
                        e.stopPropagation();
                        deleteExchange(turnId, false);
                    });
                    rightControls.appendChild(deleteButton);
                }

                renderAssistantMessageContent(messageText, rawText, debugData);

                updateChatRenderModeBadge(renderModeBadge, messageText, {
                    canRenderMarkdown,
                    shouldRenderMarkdown: shouldRenderMarkdownForAssistant(rawText, debugData),
                    model: debugData?.model
                });
            } catch (e) {
                console.error('[chatTab] Failed to render Von message:', e);
                messageText.textContent = String(message);
            }

            messageHeader.appendChild(rightControls);

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
            messageContainer.className = 'message-container';

            if (sender === 'Error') {
                messageContainer.style.borderLeftColor = '#dc3545';
                messageContainer.style.backgroundColor = '#fff5f5';
            }

            const messageHeader = document.createElement('div');
            messageHeader.style.cssText = 'font-weight: bold; margin-bottom: 5px; font-size: 0.9em; display: flex; align-items: center; gap: 8px;';
            messageHeader.style.color = sender === 'Error' ? '#dc3545' : '#28a745';

            const headerText = document.createElement('span');
            headerText.textContent = `${sender} • ${displayTimestamp}${historySuffix}`;
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

                const debugData = llmDebugData.get(turnId);
                const warnings = deriveLlmDebugWarnings(debugData);
                const warningIndicator = createChatDebugWarningIndicator(warnings);
                if (warningIndicator) {
                    messageHeader.appendChild(warningIndicator);
                }
            }

            // Add delete button for user messages
            if (turnId) {
                const deleteButton = document.createElement('button');
                deleteButton.className = 'btn-mini btn-delete-exchange';
                deleteButton.type = 'button';
                deleteButton.textContent = '✕';
                deleteButton.title = 'Delete this exchange';
                deleteButton.addEventListener('click', (e) => {
                    e.stopPropagation();
                    deleteExchange(turnId, true);
                });
                messageHeader.appendChild(deleteButton);
            }

            const messageText = document.createElement('div');
            messageText.style.cssText = 'color: #333; white-space: pre-wrap; text-align: left; font-weight: 400;';
            const userText = String(message ?? '');
            const shouldRenderUserMarkdown = sender === 'User' && detectMarkdown(userText);
            if (shouldRenderUserMarkdown) {
                messageText.classList.add('markdown-rendered', 'chat-markdown');
                messageText.style.whiteSpace = 'normal';
                messageText.textContent = userText;
                void renderChatMarkdownIntoContainer(messageText, userText).catch((err) => {
                    console.error('[chatTab] Server markdown render failed for user message; falling back to plain text:', err);
                    messageText.textContent = userText;
                });
            } else {
                try {
                    cartouchifyElementText(messageText, userText);
                    hydrateChatConceptCartouches(messageText);
                } catch (e) {
                    console.error('[chatTab] cartouchifyElementText failed for User/Error message:', e);
                    messageText.textContent = userText;
                }
            }

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
    } catch (err) {
        console.error('[chatTab] appendMessage crashed:', err);
    }
}

function appendResetNotice(scrollableField) {
    if (!scrollableField) {
        return;
    }

    const resetMessage = document.createElement('div');
    resetMessage.className = 'reset-notice';
    resetMessage.textContent = 'Context reset successfully. You can start a new conversation.';
    scrollableField.appendChild(resetMessage);
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
    const auxSection = document.getElementById('chatLlmDebugAuxSection');
    const auxPre = document.getElementById('chatLlmDebugAux');

    if (!popup || !metaDiv || !messagesPre || !responsePre || !toolsSection || !toolsPre || !auxSection || !auxPre) {
        console.error('[chatTab] LLM debug popup elements missing');
        return;
    }

    // Display metadata
    const hasError = debugData.error !== undefined;

    // Build metadata object (not HTML) so it's included in JSON structure
    const metadata = {
        model: debugData.model || 'Unknown',
        message_count: debugData.messages?.length || 0
    };

    // Add context statistics if available
    if (debugData.context_stats) {
        const sentStats = debugData.context_stats.sent_to_llm;
        const storedStats = debugData.context_stats.stored_context;

        if (sentStats) {
            metadata.context_sent_to_llm = {
                total_messages: sentStats.total_messages,
                total_chars: sentStats.total_chars,
                largest_message: sentStats.largest_message
            };
        }

        if (storedStats) {
            metadata.stored_context = {
                total_messages: storedStats.total_messages,
                total_chars: storedStats.total_chars
            };
        }
    }

    // Add tool statistics if available
    if (debugData.tool_stats) {
        metadata.mcp_tools_used = {
            tool_count: debugData.tool_stats.tool_count,
            total_chars: debugData.tool_stats.total_chars,
            truncated_count: debugData.tool_stats.truncated_count
        };
    }

    if (hasError) {
        metadata.error = debugData.error;
    }

    let workflowExecutionTrace = null;
    if (Array.isArray(debugData.aux_llm_calls)) {
        workflowExecutionTrace = debugData.aux_llm_calls.find((entry) => {
            if (!entry || typeof entry !== 'object') {
                return false;
            }
            if (entry.type !== 'workflow_execution_trace') {
                return false;
            }
            return typeof entry.execution_id === 'string' && entry.execution_id.trim().length > 0;
        }) || null;
    }

    // Build metadata HTML display
    let metadataHtml = '';
    if (workflowExecutionTrace) {
        const executionId = String(workflowExecutionTrace.execution_id).trim();
        const href = `/api/workflows/executions/${encodeURIComponent(executionId)}`;
        const workflowSummary = {
            workflow_id: workflowExecutionTrace.workflow_id ?? null,
            execution_id: executionId,
            stored: workflowExecutionTrace.stored ?? null,
            status: workflowExecutionTrace.status ?? null
        };

        metadataHtml += '<div class="llm-debug-metadata-section">';
        metadataHtml += '<strong>Workflow execution</strong>';
        metadataHtml += '<div>';
        metadataHtml += `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(executionId)}</a>`;
        metadataHtml += '</div>';
        metadataHtml += '<pre>';
        metadataHtml += escapeHtml(JSON.stringify(workflowSummary, null, 2));
        metadataHtml += '</pre>';
        metadataHtml += '</div>';
    }

    metadataHtml += '<div class="llm-debug-metadata-section"><strong>Metadata</strong><pre>';
    metadataHtml += JSON.stringify(metadata, null, 2);
    metadataHtml += '</pre></div>';

    const warnings = deriveLlmDebugWarnings(debugData).filter(w => !String(w).startsWith('Backend error:'));
    if (warnings.length > 0) {
        const warningItems = warnings.map(warning => `<li>${warning}</li>`).join('');
        metadataHtml = `
            <div class="llm-debug-warning-box">
                <div class="llm-debug-warning-box-title">Warnings</div>
                <ul class="llm-debug-warning-list">${warningItems}</ul>
            </div>
        ` + metadataHtml;
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

    // Display auxiliary LLM calls if any
    if (debugData.aux_llm_calls && debugData.aux_llm_calls.length > 0) {
        auxSection.classList.remove('hidden');
        try {
            auxPre.textContent = JSON.stringify(debugData.aux_llm_calls, null, 2);
        } catch (e) {
            auxPre.textContent = 'Error formatting auxiliary LLM calls';
        }
    } else {
        auxSection.classList.add('hidden');
    }

    // Store full data for copy function, including computed metadata
    const enhancedDebugData = {
        ...debugData,
        metadata: metadata,  // Add computed metadata to the structure
        workflow_execution_trace: workflowExecutionTrace || undefined
    };
    popup.dataset.currentDebugData = JSON.stringify(enhancedDebugData, null, 2);

    // Show popup - update aria-hidden BEFORE showing to avoid accessibility warning
    popup.setAttribute('aria-hidden', 'false');
    popup.classList.remove('hidden');

    // Focus close button for accessibility
    const closeBtn = document.getElementById('closeChatLlmDebug');
    if (closeBtn) {
        // Small delay to ensure popup is visible before focusing
        setTimeout(() => closeBtn.focus(), 10);
    }
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
// Export for testing
export const setLlmDebugDataForTurn = (turnId, debugData) => {
    llmDebugData.set(turnId, debugData);
};
export { formatChatTimestamp, showLlmDebugPopup, updateHistoryLength };

