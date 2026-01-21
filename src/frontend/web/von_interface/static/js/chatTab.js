// Chat Tab Module
import { annotateTurn, getUserContext } from './apiService.js';
import { initializeConceptAutocomplete } from './components/conceptAutocomplete.js';
import { initializePromptCartoucheOverlay, normaliseVontologyIdsForBackend } from './components/promptCartoucheOverlay.js';
import { elements, renderSpanSuggestions } from './domUtils.js';
import { isAnnotationEnabled } from './featureFlags.js';
import { detectMarkdown, renderMarkdownViaServer } from './markdownUtils.js';
import {
    getSpeechSynthesisVoices,
    isSpeechRecognitionSupported,
    isTextToSpeechSupported,
    speakText,
    startSpeechRecognition,
    stopSpeaking
} from './speech.js';
import { getPreferredLanguage, selectBestNameForContext } from './utils/nameSelection.js';
import { cartouchifyElementText, cartouchifyVontologyTokensInElement, linkifyVontologyTokensInElement } from './utils/textDecorator.js';
import { showToast } from './utils/toast.js';

// Store LLM debug data for each turn
const llmDebugData = new Map();
const llmDebugFetchInFlight = new Map();
// Track conversation turns for Markdown export and state resets
const transcriptTurns = [];
let historySegmentsShown = 1;
let totalHistorySegments = 1;
let activeChatSessionId = null;
let activeChatSessionName = null;
let sessionTabsCache = [];
const sessionHistoryCache = new Map();
let loadingChatSessionId = null;
const SESSION_TABS_REFRESH_COOLDOWN_MS = 15_000;
let lastSessionTabsRefreshMs = 0;
let pendingSessionTabsRefresh = null;
let lastRenderedSessionCount = 0;

// JVNAUTOSCI-982: Per-session metadata (programme/project/activity/modality links)
const CHAT_SESSION_LINK_KEYS = [
    { key: 'programmes', label: 'Programmes', placeholder: 'Add a programme…', typeConceptId: '#V#programme' },
    { key: 'projects', label: 'Projects', placeholder: 'Add a project…', typeConceptId: '#V#project' },
    // Note: there is no canonical #V#activity type concept in the ontology.
    { key: 'activities', label: 'Activities', placeholder: 'Add an activity…', typeConceptId: '#V#work_activity' },
    { key: 'modalities', label: 'Modalities', placeholder: 'Add a modality…', typeConceptId: '#V#conversation_modality' }
];
const chatSessionLinksCache = new Map();
let activeChatSessionLinks = null;
let chatSessionLinksLoadInFlight = null;
let chatSessionLinksSaveInFlight = null;
let chatSessionLinksSaveDebounceId = null;
let chatSessionLinksDirtySessionId = null;
let chatSessionLinksDirtyPayload = null;
let chatSessionMetadataOpenKey = null;
const LS_CHAT_SESSION_METADATA_COLLAPSED = 'von:chatSessionMetadataCollapsed';
let chatSessionMetadataCollapsed = null;

const chatSessionConceptMetaCache = new Map();
const chatSessionConceptMetaInFlight = new Map();

// Lightweight client-side telemetry for chat session tab loading (elapsed + ETA).
// Stored locally only; intended to feed future introspection.
const LS_CHAT_TABS_LOAD_STATS = 'von:chatSessionTabsLoadStats';
let chatTabsFirstLoadStartPerfMs = null;
let chatTabsFirstLoadStartEpochMs = null;
let chatTabsLoadingTickerId = null;
let chatTabsLoadingTickerLastRenderedSec = -1;
let chatSessionMenuEl = null;
let activeHistoryRequest = null;
let historyRequestCounter = 0;
const HISTORY_SEGMENT_SIZE = 200;
const HISTORY_TAIL_SEGMENT_SIZE = 30;

// JVNAUTOSCI-942: Tool-use progress while "Thinking..."
const DEFAULT_THINKING_TEXT = 'Thinking...';
let showToolUseDuringThinkingSetting = true;
let lastToolUseSettingRefreshMs = 0;
const TOOL_USE_SETTING_REFRESH_COOLDOWN_MS = 30_000;

function getLoadingIndicatorTextEl() {
    const loadingIndicator = document.getElementById('loadingIndicator');
    if (!loadingIndicator) {
        return null;
    }
    return loadingIndicator.querySelector('.loading-indicator-text');
}

function getLoadingIndicatorEl() {
    return document.getElementById('loadingIndicator');
}

function getLoadingIndicatorDetailEl() {
    return document.getElementById('loadingIndicatorDetail');
}

function setLoadingIndicatorText(text) {
    const el = getLoadingIndicatorTextEl();
    if (!el) {
        return;
    }
    el.textContent = String(text ?? '').trim() || DEFAULT_THINKING_TEXT;
}

function setLoadingIndicatorTooltip(text) {
    const detailEl = getLoadingIndicatorDetailEl();
    if (detailEl) {
        return;
    }

    const value = String(text ?? '').trim();
    const wrapper = getLoadingIndicatorEl();
    if (wrapper) {
        // Opt out of suppressTooltips.js for this element.
        // Important: set before `title`, otherwise the MutationObserver may strip it.
        wrapper.setAttribute('data-keep-title', 'true');
        wrapper.title = value;
        wrapper.setAttribute('aria-label', value);
        wrapper.setAttribute('data-original-title', value);
    }

    const textEl = getLoadingIndicatorTextEl();
    if (textEl) {
        textEl.setAttribute('data-keep-title', 'true');
        textEl.title = value;
        textEl.setAttribute('aria-label', value);
        textEl.setAttribute('data-original-title', value);
    }
}

function setLoadingIndicatorDetailText(text) {
    const detailEl = getLoadingIndicatorDetailEl();
    if (!detailEl) {
        return;
    }
    const value = String(text ?? '').trim();
    detailEl.textContent = value;
    detailEl.setAttribute('aria-hidden', value ? 'false' : 'true');

    const promptInput = document.getElementById('promptInput');
    const wrapper = promptInput?.closest?.('.prompt-input-wrapper');
    if (wrapper) {
        if (value) {
            wrapper.classList.add('has-loading-detail');
        } else {
            wrapper.classList.remove('has-loading-detail');
        }
    }
}

function createClientRequestId() {
    try {
        if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
            return crypto.randomUUID();
        }
    } catch (_) {
        // Ignore.
    }
    return `req-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function updateSessionHistoryCache(sessionId, history, meta = {}) {
    const sid = String(sessionId || '').trim();
    if (!sid || !Array.isArray(history)) {
        return;
    }
    sessionHistoryCache.set(sid, {
        session_id: sid,
        history,
        message_count: meta.message_count ?? null,
        last_message_at: meta.last_message_at ?? null,
        segments: meta.segments ?? null,
        total_segments: meta.total_segments ?? null,
        cached_at_ms: Date.now()
    });
}

function getSessionHistoryCache(sessionId) {
    const sid = String(sessionId || '').trim();
    if (!sid) {
        return null;
    }
    return sessionHistoryCache.get(sid) || null;
}

function canReuseSessionHistory(sessionId) {
    const sid = String(sessionId || '').trim();
    if (!sid) {
        return false;
    }
    const cached = getSessionHistoryCache(sid);
    if (!cached) {
        return false;
    }
    const sessionMeta = sessionTabsCache.find(
        session => String(session?.session_id || '') === sid
    );
    if (!sessionMeta) {
        return false;
    }
    const cachedCount = cached.message_count;
    const cachedLast = cached.last_message_at;
    const currentCount = sessionMeta.message_count;
    const currentLast = sessionMeta.last_message_at;
    if (cachedCount == null || cachedLast == null || currentCount == null || currentLast == null) {
        return false;
    }
    return cachedCount === currentCount && cachedLast === currentLast;
}

function rehydrateFromCache(scrollableField, cached) {
    if (!scrollableField || !cached || !Array.isArray(cached.history)) {
        return false;
    }
    historySegmentsShown = Number.isInteger(cached.segments) && cached.segments > 0 ? cached.segments : 1;
    totalHistorySegments = Number.isInteger(cached.total_segments) && cached.total_segments > 0
        ? cached.total_segments
        : historySegmentsShown;
    rehydrateHistory(scrollableField, cached.history, {
        scrollToBottom: true,
        preserveScroll: false,
        showResetNotice: false,
        forceScrollToBottom: true
    });
    updateHistoryBanner();
    return true;
}

async function refreshToolUseDuringThinkingSetting(force = false) {
    const now = Date.now();
    if (!force && now - lastToolUseSettingRefreshMs < TOOL_USE_SETTING_REFRESH_COOLDOWN_MS) {
        return showToolUseDuringThinkingSetting;
    }

    lastToolUseSettingRefreshMs = now;
    try {
        const resp = await fetch('/api/settings/', { method: 'GET' });
        if (!resp || !resp.ok) {
            return showToolUseDuringThinkingSetting;
        }
        const data = await resp.json();
        if (data && typeof data.show_tool_use_during_thinking !== 'undefined') {
            showToolUseDuringThinkingSetting = !!data.show_tool_use_during_thinking;
            if (!showToolUseDuringThinkingSetting && activeChatRequest) {
                stopToolUseProgressPolling(activeChatRequest);
                setLoadingIndicatorText(DEFAULT_THINKING_TEXT);
            }
        }
    } catch (_) {
        // Ignore.
    }
    return showToolUseDuringThinkingSetting;
}

function formatToolUseProgressText(progress, request = null) {
    if (!progress || typeof progress !== 'object') {
        return DEFAULT_THINKING_TEXT;
    }

    const status = typeof progress.status === 'string' ? progress.status : 'thinking';
    const tool = typeof progress.tool === 'string' ? progress.tool : null;
    const workflowTask = typeof progress.workflow_task === 'string' ? progress.workflow_task : null;
    const batchSize = Number.isFinite(progress.batch_size) ? Number(progress.batch_size) : null;
    const done = Number.isFinite(progress.tool_calls_done) ? Number(progress.tool_calls_done) : null;
    const cap = Number.isFinite(progress.tool_calls_cap) ? Number(progress.tool_calls_cap) : null;
    const remaining = Number.isFinite(progress.tool_calls_remaining) ? Number(progress.tool_calls_remaining) : null;

    const bits = [];

    const thinkingStartedAtMs = request && Number.isFinite(request.thinkingStartedAtMs)
        ? Number(request.thinkingStartedAtMs)
        : null;
    const elapsedMs = thinkingStartedAtMs === null ? null : Date.now() - thinkingStartedAtMs;
    if (elapsedMs !== null) {
        bits.push(`for ${formatThinkingDuration(elapsedMs)}`);
    }

    if (tool) {
        bits.push(`tool: ${tool}`);
    }

    if (workflowTask) {
        bits.push(`task: ${workflowTask}`);
    }

    if (batchSize !== null) {
        bits.push(`batch ${batchSize}`);
    }

    if (done !== null && cap !== null) {
        bits.push(`${done}/${cap} used`);
    }

    if (remaining !== null) {
        bits.push(`${remaining} remaining`);
    }

    if (status === 'tool_failed' || status === 'error') {
        const error = typeof progress.error === 'string' ? progress.error.trim() : '';
        if (error) {
            bits.push(`error: ${error}`);
        } else {
            bits.push('error');
        }
    }

    if (!bits.length) {
        return DEFAULT_THINKING_TEXT;
    }

    return `${DEFAULT_THINKING_TEXT} (${bits.join(', ')})`;
}

function recordToolUseHistory(request, progress) {
    if (!request || !progress || typeof progress !== 'object') {
        return;
    }

    const tool = typeof progress.tool === 'string' ? progress.tool.trim() : '';
    const workflowTask = typeof progress.workflow_task === 'string' ? progress.workflow_task.trim() : '';
    if (!tool && !workflowTask) {
        return;
    }

    const batchSize = Number.isFinite(progress.batch_size) ? Number(progress.batch_size) : null;

    if (!Array.isArray(request.toolUseProgressHistory)) {
        request.toolUseProgressHistory = [];
    }

    const history = request.toolUseProgressHistory;
    const last = history.length ? history[history.length - 1] : null;
    const lastTool = last && typeof last.tool === 'string' ? last.tool : null;
    const lastTask = last && typeof last.workflowTask === 'string' ? last.workflowTask : null;
    const lastBatch = last && Number.isFinite(last.batchSize) ? Number(last.batchSize) : null;

    if (lastTool === tool && lastTask === workflowTask && lastBatch === batchSize) {
        return;
    }

    history.push({ tool, workflowTask, batchSize });
}

function formatThinkingDuration(elapsedMs) {
    const ms = Number.isFinite(elapsedMs) ? Math.max(0, Number(elapsedMs)) : 0;
    const totalSeconds = Math.floor(ms / 1000);
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;

    if (minutes > 0) {
        return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
    }
    return `${totalSeconds}s`;
}

function formatToolUseHistoryTooltip(request) {
    if (!request) {
        return '';
    }

    const thinkingStartedAtMs = Number.isFinite(request.thinkingStartedAtMs)
        ? Number(request.thinkingStartedAtMs)
        : null;
    const elapsedMs = thinkingStartedAtMs === null ? null : Date.now() - thinkingStartedAtMs;

    const history = Array.isArray(request.toolUseProgressHistory) ? request.toolUseProgressHistory : [];

    if (!history.length) {
        const lines = ['No tool use yet.'];
        if (elapsedMs !== null) {
            lines.push(`Thinking for ${formatThinkingDuration(elapsedMs)}.`);
        }
        return lines.join('\n');
    }

    const lines = [];
    if (elapsedMs !== null) {
        lines.push(`Thinking for ${formatThinkingDuration(elapsedMs)}`);
    }
    lines.push('Tools this turn:');
    for (const entry of history) {
        const tool = entry && typeof entry.tool === 'string' ? entry.tool : '';
        const workflowTask = entry && typeof entry.workflowTask === 'string' ? entry.workflowTask : '';
        if (!tool) {
            if (!workflowTask) {
                continue;
            }
        }
        const batchSize = entry && Number.isFinite(entry.batchSize) ? Number(entry.batchSize) : null;
        if (batchSize === null) {
            lines.push(`- ${tool || workflowTask}`);
        } else {
            lines.push(`- ${tool || workflowTask} (batch ${batchSize})`);
        }
    }

    return lines.length > 1 ? lines.join('\n') : '';
}

function stopToolUseProgressPolling(request) {
    if (!request) {
        return;
    }

    const poll = request.toolUseProgressPoll;
    if (!poll) {
        return;
    }

    request.toolUseProgressPoll = null;

    try {
        if (poll.intervalId) {
            clearInterval(poll.intervalId);
        }
    } catch (_) {
        // Ignore.
    }

    try {
        if (poll.timeoutId) {
            clearTimeout(poll.timeoutId);
        }
    } catch (_) {
        // Ignore.
    }

    try {
        poll.abortController?.abort();
    } catch (_) {
        // Ignore.
    }
}

function stopThinkingTooltipTicker(request) {
    if (!request) {
        return;
    }

    try {
        if (request.thinkingTooltipIntervalId) {
            clearInterval(request.thinkingTooltipIntervalId);
        }
    } catch (_) {
        // Ignore.
    }

    request.thinkingTooltipIntervalId = null;
}

function startThinkingTooltipTicker(request) {
    if (!request || request.aborted) {
        return;
    }

    stopThinkingTooltipTicker(request);

    // Update once per second so the elapsed time stays current,
    // even if tool-progress polling backs off (e.g., repeated 404s).
    request.thinkingTooltipIntervalId = setInterval(() => {
        if (request.aborted || activeChatRequest !== request) {
            stopThinkingTooltipTicker(request);
            return;
        }
        setLoadingIndicatorDetailText(formatToolUseHistoryTooltip(request));
    }, 1000);
}

function startToolUseProgressPolling(request) {
    if (!request || request.aborted) {
        return;
    }

    if (!showToolUseDuringThinkingSetting) {
        return;
    }

    const requestId = request.clientRequestId;
    if (!requestId) {
        return;
    }

    stopToolUseProgressPolling(request);
    const abortController = new AbortController();

    const poll = {
        abortController,
        timeoutId: null,
        intervalId: null,
        nextDelayMs: 350,
        consecutiveNotFound: 0
    };

    const scheduleNextPoll = (delayMs) => {
        if (request.aborted || activeChatRequest !== request) {
            return;
        }
        poll.timeoutId = setTimeout(() => {
            void pollOnce();
        }, Math.max(0, Number(delayMs) || 0));
    };

    const pollOnce = async () => {
        if (request.aborted || activeChatRequest !== request) {
            return;
        }

        // Keep detail text "alive" even before the server has any tool-progress state.
        setLoadingIndicatorDetailText(formatToolUseHistoryTooltip(request));

        try {
            const resp = await fetch(`/von/progress/${encodeURIComponent(requestId)}`,
                { method: 'GET', signal: abortController.signal });
            if (!resp) {
                scheduleNextPoll(Math.min(5000, poll.nextDelayMs * 1.7));
                poll.nextDelayMs = Math.min(5000, poll.nextDelayMs * 1.7);
                return;
            }

            if (resp.status === 404) {
                poll.consecutiveNotFound += 1;
                poll.nextDelayMs = Math.min(10_000, poll.nextDelayMs * 1.7);
                scheduleNextPoll(poll.nextDelayMs);
                return;
            }

            if (resp.status === 202) {
                poll.consecutiveNotFound = 0;
                poll.nextDelayMs = Math.min(5000, poll.nextDelayMs * 1.4);
                scheduleNextPoll(poll.nextDelayMs);
                return;
            }

            if (!resp.ok) {
                poll.nextDelayMs = Math.min(5000, poll.nextDelayMs * 1.7);
                scheduleNextPoll(poll.nextDelayMs);
                return;
            }
            const progress = await resp.json();

            poll.consecutiveNotFound = 0;
            poll.nextDelayMs = 350;
            setLoadingIndicatorText(formatToolUseProgressText(progress, request));
            recordToolUseHistory(request, progress);
            setLoadingIndicatorDetailText(formatToolUseHistoryTooltip(request));

            const status = typeof progress?.status === 'string' ? progress.status : null;
            if (status === 'disabled') {
                poll.nextDelayMs = 10_000;
                scheduleNextPoll(poll.nextDelayMs);
                return;
            }

            if (status === 'completed' || status === 'error') {
                stopToolUseProgressPolling(request);
                return;
            }

            scheduleNextPoll(poll.nextDelayMs);
        } catch (err) {
            if (err && err.name === 'AbortError') {
                return;
            }

            poll.nextDelayMs = Math.min(5000, poll.nextDelayMs * 1.7);
            scheduleNextPoll(poll.nextDelayMs);
        }
    };

    request.toolUseProgressPoll = poll;

    void pollOnce();
}

// Cool-down for failed history talk-track backfills so we do not spam the server.
// Map<turnId, { at: number, error: string }>
const historySpokenBackfillFailures = new Map();
const HISTORY_SPOKEN_BACKFILL_FAILURE_COOLDOWN_MS = 30_000;

function stripMarkdownForSpeech(text) {
    const input = String(text ?? '');
    if (!input.trim()) {
        return '';
    }

    let value = input;

    // Remove fenced code blocks entirely.
    value = value.replace(/```[\s\S]*?```/g, ' ');

    // Replace inline code with its content.
    value = value.replace(/`([^`]+)`/g, '$1');

    // Replace markdown links [text](url) -> text.
    value = value.replace(/\[([^\]]+)\]\([^\)]+\)/g, '$1');

    // Remove images ![alt](url) -> alt.
    value = value.replace(/!\[([^\]]*)\]\([^\)]+\)/g, '$1');

    // Remove headings/bullets/quotes markers.
    value = value
        .replace(/^\s{0,3}#{1,6}\s+/gm, '')
        .replace(/^\s{0,3}>\s?/gm, '')
        .replace(/^\s*[-*+]\s+/gm, '')
        .replace(/^\s*\d+\.[\s]+/gm, '');

    // Remove emphasis markers.
    value = value.replace(/\*\*([^*]+)\*\*/g, '$1');
    value = value.replace(/\*([^*]+)\*/g, '$1');
    value = value.replace(/__([^_]+)__/g, '$1');
    value = value.replace(/_([^_]+)_/g, '$1');

    // Remove horizontal rules.
    value = value.replace(/^\s*---+\s*$/gm, ' ');

    // Collapse whitespace.
    value = value.replace(/[ \t]+/g, ' ');
    value = value.replace(/\n{3,}/g, '\n\n');
    value = value.trim();

    return value;
}

function deriveNarrationFromScreenText(screenText, options = {}) {
    const cleaned = stripMarkdownForSpeech(screenText);
    if (!cleaned) {
        return '';
    }

    const maxChars = Number.isFinite(options.maxChars) ? options.maxChars : 900;
    if (cleaned.length <= maxChars) {
        return cleaned;
    }

    // Prefer not to cut mid-sentence if possible.
    const slice = cleaned.slice(0, maxChars);
    const lastBreak = Math.max(slice.lastIndexOf('. '), slice.lastIndexOf('? '), slice.lastIndexOf('! '));
    if (lastBreak > 200) {
        return slice.slice(0, lastBreak + 1).trim() + '…';
    }
    return slice.trim() + '…';
}

function normalisePresenterChannels(value) {
    if (!value || typeof value !== 'object') {
        return null;
    }

    const screen = typeof value.screen === 'string' ? value.screen : null;
    const spoken = typeof value.spoken === 'string' ? value.spoken : null;
    const format = typeof value.format === 'string' ? value.format : null;

    const hasAny = (screen && screen.trim()) || (spoken && spoken.trim());
    if (!hasAny) {
        return null;
    }

    return {
        screen: screen && screen.trim() ? screen : null,
        spoken: spoken && spoken.trim() ? spoken : null,
        format
    };
}

function enrichDebugDataWithSpeechPlanning(debugData, options = {}) {
    if (!debugData || typeof debugData !== 'object') {
        return debugData;
    }

    const presenterChannels = normalisePresenterChannels(
        options.presenterChannels || debugData.presenter_channels
    );

    const screenText =
        (typeof options.screenText === 'string' && options.screenText.trim())
            ? options.screenText
            : (presenterChannels?.screen || (typeof debugData.response === 'string' ? debugData.response : ''));

    const spokenText =
        (typeof options.spokenText === 'string' && options.spokenText.trim())
            ? options.spokenText
            : (presenterChannels?.spoken || '');

    const ttsText = (spokenText && spokenText.trim()) ? spokenText : screenText;
    const ttsSource = (spokenText && spokenText.trim()) ? 'spoken' : 'screen';

    const speechPlanning = {
        enabled_by_protocol: !!presenterChannels,
        presenter_format: presenterChannels?.format || null,
        tts_source: ttsSource,
        screen_chars: typeof screenText === 'string' ? screenText.length : 0,
        spoken_chars: typeof spokenText === 'string' ? spokenText.length : 0,
        tts_chars: typeof ttsText === 'string' ? ttsText.length : 0
    };

    return {
        ...debugData,
        presenter_channels: presenterChannels || debugData.presenter_channels || null,
        speech_planning: speechPlanning
    };
}

function escapeHtml(value) {
    const text = String(value ?? '');
    return text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function extractPromptConceptGroups(debugData) {
    const userPrompt = debugData && typeof debugData === 'object' ? debugData.user_prompt : null;
    const loaded = !!userPrompt?.loaded;
    const chars = Number.isFinite(userPrompt?.chars) ? Number(userPrompt?.chars) : null;
    if (!loaded || (chars !== null && chars <= 0)) {
        return [];
    }
    const seen = new Set();
    const groups = [];

    const addGroup = (label, ids) => {
        if (!Array.isArray(ids)) {
            return;
        }
        const cleaned = [];
        for (const raw of ids) {
            if (typeof raw !== 'string') {
                continue;
            }
            const trimmed = raw.trim();
            if (!trimmed || seen.has(trimmed)) {
                continue;
            }
            seen.add(trimmed);
            cleaned.push(trimmed);
        }
        if (cleaned.length > 0) {
            groups.push({ label, ids: cleaned });
        }
    };

    addGroup('Behaviour prompts', userPrompt?.behaviour_prompt_concept_ids);
    addGroup('Narration prompts', userPrompt?.narration_prompt_concept_ids);
    addGroup('Screen prompts', userPrompt?.screen_prompt_concept_ids);
    addGroup('Other prompts', userPrompt?.prompt_concept_ids);

    return groups;
}

function buildPromptConceptLinksHtml(groups) {
    if (!Array.isArray(groups) || groups.length === 0) {
        return '';
    }

    let html = '<div class="llm-debug-metadata-section">';
    html += '<strong>Prompt concepts</strong>';
    html += '<div class="llm-debug-prompt-groups">';
    for (const group of groups) {
        const label = escapeHtml(group.label);
        const links = (group.ids || []).map((id) => {
            const safeId = escapeHtml(id);
            return `<button type="button" class="llm-debug-prompt-link" data-concept-id="${safeId}">${safeId}</button>`;
        }).join(' ');
        html += `<div class="llm-debug-prompt-group"><span class="llm-debug-prompt-label">${label}:</span>${links ? ` ${links}` : ''}</div>`;
    }
    html += '</div>';
    html += '</div>';
    return html;
}

function extractBoldQuotedInstruction(text) {
    const value = String(text ?? '').trim();
    if (!value) return null;

    // Match either curly quotes or straight quotes.
    // Example: “Apply the hierarchy fix.” or "Apply the hierarchy fix.".
    const m = value.match(/^(?:“|")(.+?)(?:”|")\s*$/);
    if (!m) return null;

    const instruction = String(m[1] ?? '').trim();
    if (!instruction) return null;
    return instruction;
}

function extractQuotedInstruction(text) {
    const value = String(text ?? '').trim();
    if (!value) return null;

    // Support curly or straight quotes.
    const m = value.match(/^(?:“|")(.+?)(?:”|")\s*$/);
    if (!m) return null;

    const instruction = String(m[1] ?? '').trim();
    if (!instruction) return null;
    return instruction;
}

function isAllowedUnquotedBlockquoteInstruction(text) {
    const value = String(text ?? '').trim();
    if (!value) return false;

    // Purposefully narrow: broken-windows fix for known UI phrasing.
    // (Avoid turning arbitrary bolded blockquotes into buttons.)
    const compact = value.replace(/\s+/g, ' ');
    return /^Proceed with creation using verified parents and contribution[-‑–—]based modelling\?$/i.test(compact);
}

function shouldButtonifyInlineQuotedInstruction(instruction) {
    const value = String(instruction ?? '').trim();
    if (!value) return false;
    const compact = value.replace(/\s+/g, ' ');

    if (/^(yes|no)$/i.test(compact)) return true;
    if (/^Create the core paper representation now \(paper \+ authors \+ core contribution only\)\.?$/i.test(compact)) return true;
    if (/^Create the full representation as specified\.?$/i.test(compact)) return true;
    return false;
}

function extractBoldQuotedInstructionFromStrong(strongEl) {
    if (!strongEl) return null;

    // Reconstruct text while preserving inline-code intent by re-adding backticks.
    // This keeps the inserted prompt closer to the original markdown source.
    const parts = [];
    const nodes = Array.from(strongEl.childNodes ?? []);
    for (const node of nodes) {
        if (node.nodeType === Node.TEXT_NODE) {
            parts.push(String(node.textContent ?? ''));
            continue;
        }

        if (node.nodeType !== Node.ELEMENT_NODE) {
            return null;
        }

        const el = node;
        if (el.tagName === 'CODE') {
            // Disallow nested markup inside <code> for this transform.
            if (el.querySelector && el.querySelector('*')) return null;
            const codeText = String(el.textContent ?? '');
            parts.push('`' + codeText + '`');
            continue;
        }

        // Any other tag means it is no longer the exact **"..."** pattern.
        return null;
    }

    return extractBoldQuotedInstruction(parts.join(''));
}

function insertTextIntoChatPrompt(text) {
    const promptInput = document.getElementById('promptInput');
    if (!promptInput) return;

    const insertRaw = String(text ?? '').trim();
    if (!insertRaw) return;

    try {
        promptInput.focus();
    } catch (_) {
        // Ignore.
    }

    const base = String(promptInput.value ?? '');
    const hasSelection =
        typeof promptInput.selectionStart === 'number' &&
        typeof promptInput.selectionEnd === 'number';

    const start = hasSelection ? promptInput.selectionStart : base.length;
    const end = hasSelection ? promptInput.selectionEnd : base.length;

    const atEnd = start === base.length && end === base.length;
    const prefix = atEnd && base.trim() && !base.endsWith('\n') ? '\n' : '';
    const insertText = `${prefix}${insertRaw}`;

    if (typeof promptInput.setRangeText === 'function') {
        promptInput.setRangeText(insertText, start, end, 'end');
    } else {
        const before = base.slice(0, start);
        const after = base.slice(end);
        promptInput.value = `${before}${insertText}${after}`;
    }

    try {
        promptInput.dispatchEvent(new Event('input', { bubbles: true }));
    } catch (_) {
        // Ignore.
    }
}

function appendQuickReplyButtons(container, options) {
    if (!container || !Array.isArray(options) || options.length === 0) {
        return;
    }

    const cleaned = options
        .map((opt) => String(opt ?? '').trim())
        .filter((opt) => opt && opt.length <= 60);

    if (!cleaned.length) {
        return;
    }

    const wrapper = document.createElement('div');
    wrapper.className = 'chat-quick-replies';
    wrapper.style.cssText = 'margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center;';

    const label = document.createElement('span');
    label.textContent = 'Quick replies:';
    label.style.cssText = 'font-size: 0.85em; color: #6c757d; margin-right: 4px;';
    wrapper.appendChild(label);

    for (const optionText of cleaned) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'chat-insert-prompt-button';
        btn.textContent = optionText;
        btn.title = 'Insert into chat prompt and send (Shift inserts without sending)';
        btn.setAttribute('aria-label', `Insert into chat prompt: ${optionText}`);
        btn.addEventListener('click', (e) => {
            try {
                e.preventDefault();
                e.stopPropagation();
            } catch (_) {
                // Ignore.
            }

            insertTextIntoChatPrompt(optionText);

            const shiftHeld = !!(e && e.shiftKey);
            if (!shiftHeld) {
                submitChatPromptImmediately();
            }
        });
        wrapper.appendChild(btn);
    }

    container.appendChild(wrapper);
}

function submitChatPromptImmediately() {
    const sendButton = document.getElementById('sendButton');
    if (sendButton && typeof sendButton.click === 'function') {
        sendButton.click();
        return;
    }

    try {
        handleSendPrompt();
    } catch (_) {
        // Ignore.
    }
}

function formatBytesForUi(sizeBytes) {
    const size = Number(sizeBytes);
    if (!Number.isFinite(size) || size < 0) return '';
    if (size < 1024) return `${size} B`;
    const kb = size / 1024;
    if (kb < 1024) return `${kb.toFixed(1)} KB`;
    const mb = kb / 1024;
    if (mb < 1024) return `${mb.toFixed(1)} MB`;
    const gb = mb / 1024;
    return `${gb.toFixed(2)} GB`;
}

let uploadUiState = {
    button: null,
    statusEl: null,
    inFlight: 0,
    lastStatusTimeoutId: null,
    defaultButtonLabel: null
};

function setUploadStatus(message, type = 'info') {
    const el = uploadUiState.statusEl;
    if (!el) return;

    if (uploadUiState.lastStatusTimeoutId) {
        clearTimeout(uploadUiState.lastStatusTimeoutId);
        uploadUiState.lastStatusTimeoutId = null;
    }

    el.textContent = message || '';
    el.classList.remove('is-uploading', 'is-success', 'is-error');
    if (type === 'uploading') {
        el.classList.add('is-uploading');
    } else if (type === 'success') {
        el.classList.add('is-success');
    } else if (type === 'error') {
        el.classList.add('is-error');
    }
}

function clearUploadStatusAfterDelay(delayMs = 5000) {
    if (!uploadUiState.statusEl) return;
    if (uploadUiState.lastStatusTimeoutId) {
        clearTimeout(uploadUiState.lastStatusTimeoutId);
    }
    uploadUiState.lastStatusTimeoutId = window.setTimeout(() => {
        setUploadStatus('');
        uploadUiState.lastStatusTimeoutId = null;
    }, delayMs);
}

function setUploadButtonBusy(isBusy) {
    const btn = uploadUiState.button;
    if (!btn) return;

    if (!uploadUiState.defaultButtonLabel) {
        uploadUiState.defaultButtonLabel = btn.textContent || 'Upload File';
    }

    if (isBusy) {
        btn.disabled = true;
        btn.textContent = 'Uploading…';
    } else {
        btn.disabled = false;
        btn.textContent = uploadUiState.defaultButtonLabel;
    }
}

function isFileDragEvent(event) {
    const dt = event?.dataTransfer;
    if (!dt) return false;
    try {
        const types = Array.from(dt.types || []);
        return types.includes('Files');
    } catch (_) {
        return false;
    }
}

async function uploadSingleFileToVon(file) {
    if (!file) {
        throw new Error('No file provided');
    }

    const formData = new FormData();
    formData.append('file', file, file.name || 'uploaded_file');

    const response = await fetch('/von/api/files/upload', {
        method: 'POST',
        body: formData
    });

    let data = null;
    try {
        data = await response.json();
    } catch (_) {
        data = null;
    }

    if (!response.ok || !data || data.success !== true) {
        const detail = data?.message || data?.error || `HTTP ${response.status}`;
        throw new Error(`Upload failed: ${detail}`);
    }

    return data;
}

async function uploadFilesToVon(files) {
    const list = Array.from(files || []).filter(Boolean);
    if (!list.length) return;

    uploadUiState.inFlight += 1;
    if (uploadUiState.inFlight === 1) {
        setUploadButtonBusy(true);
    }

    const total = list.length;
    let successCount = 0;
    let failureCount = 0;
    let index = 0;

    for (const file of list) {
        index += 1;
        const sizeLabel = formatBytesForUi(file.size);
        appendMessage('User', `Uploading file: ${file.name}${sizeLabel ? ` (${sizeLabel})` : ''}`);

        setUploadStatus(`Uploading ${index}/${total}: ${file.name}`, 'uploading');

        try {
            const result = await uploadSingleFileToVon(file);
            const conceptId = result?.uploaded?.concept_id;
            const blobUri = result?.storage?.uri;
            const blobKey = result?.storage?.key;
            const blobBackend = result?.storage?.backend;
            const historyRecorded = result?.chat_history_recorded === true;

            const downloadUrl = conceptId
                ? `/von/api/files/${encodeURIComponent(conceptId)}/download`
                : null;

            const details = [];
            if (blobUri) details.push(`Blob: ${blobUri}`);
            if (blobBackend || blobKey) details.push(`Blob key: ${String(blobBackend || '')}:${String(blobKey || '')}`.replace(/^:/, ''));
            if (historyRecorded) details.push('Recorded in Conversation history.');
            if (downloadUrl) details.push(`[Download attachment](${downloadUrl})`);

            appendMessage(
                'Von',
                `File uploaded and registered as ${conceptId || '(unknown)'}${details.length ? `\n${details.join('\n')}` : ''}`
            );

            successCount += 1;

            if (conceptId) {
                insertTextIntoChatPrompt(`Attached file concept: ${conceptId}`);
            }
        } catch (error) {
            console.error('[chatTab] file upload error', error);
            appendMessage('Error', `File upload failed: ${String(error?.message || error)}`);
            failureCount += 1;
            setUploadStatus(`Upload failed: ${file.name}`, 'error');
        }
    }

    const summary = `Upload complete: ${successCount} succeeded${failureCount ? `, ${failureCount} failed` : ''}.`;
    setUploadStatus(summary, failureCount ? 'error' : 'success');
    clearUploadStatusAfterDelay();

    uploadUiState.inFlight = Math.max(0, uploadUiState.inFlight - 1);
    if (uploadUiState.inFlight === 0) {
        setUploadButtonBusy(false);
    }
}

function convertJustSayInstructionsToButtons(root) {
    if (!root || !root.querySelectorAll) return;

    const normalise = (value) => String(value ?? '').trim().replace(/\s+/g, ' ');

    const isAllowedQuickReply = (value) => {
        const compact = normalise(value);
        if (!compact) return false;

        if (compact.length > 60) return false;
        if (compact.split(' ').length > 4) return false;
        if (/[\n\r]/.test(compact)) return false;
        if (/[<>]/.test(compact)) return false;

        return true;
    };

    const createInsertButton = (text) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'chat-insert-prompt-button';
        btn.textContent = text;
        btn.title = 'Insert into chat prompt and send (Shift inserts without sending)';
        btn.setAttribute('aria-label', `Insert into chat prompt: ${text}`);
        btn.addEventListener('click', (e) => {
            try {
                e.preventDefault();
                e.stopPropagation();
            } catch (_) {
                // Ignore.
            }

            insertTextIntoChatPrompt(text);

            const shiftHeld = !!(e && e.shiftKey);
            if (!shiftHeld) {
                submitChatPromptImmediately();
            }
        });
        return btn;
    };

    const shouldSkipNode = (node) => {
        const parent = node?.parentElement;
        if (!parent?.closest) return true;
        if (parent.closest('pre, code, a, button, textarea, input')) return true;
        if (parent.closest('.chat-insert-prompt-wrapper, .chat-insert-prompt-inline-wrapper')) return true;
        return false;
    };

    const splitJustSayMatch = (fullMatch) => {
        const text = String(fullMatch ?? '');
        const pairs = [
            ['"', '"'],
            ['“', '”'],
            ["'", "'"],
            ['‘', '’']
        ];

        for (const [open, close] of pairs) {
            const openIndex = text.indexOf(open);
            const closeIndex = text.lastIndexOf(close);
            if (openIndex !== -1 && closeIndex !== -1 && closeIndex > openIndex) {
                return {
                    leading: text.slice(0, openIndex),
                    reply: text.slice(openIndex + open.length, closeIndex),
                    trailing: text.slice(closeIndex + close.length)
                };
            }
        }

        const trimmed = text.replace(/\s+$/g, '');
        const lastSpace = trimmed.lastIndexOf(' ');
        if (lastSpace === -1) {
            return { leading: '', reply: trimmed, trailing: '' };
        }
        return {
            leading: trimmed.slice(0, lastSpace + 1),
            reply: trimmed.slice(lastSpace + 1),
            trailing: ''
        };
    };

    const splitSayOrMatch = (fullMatch) => {
        const text = String(fullMatch ?? '');
        const marker = text.match(/\b(?:just\s+)?say\b/i);
        if (!marker || marker.index == null) {
            return null;
        }

        const afterMarker = text.slice(marker.index + marker[0].length);
        const orIndex = afterMarker.toLowerCase().indexOf(' or ');
        if (orIndex === -1) {
            return null;
        }

        return {
            prefix: text.slice(0, marker.index + marker[0].length),
            left: afterMarker.slice(0, orIndex),
            right: afterMarker.slice(orIndex + 4)
        };
    };

    const quickReplyWords = '(?:proceed|yes|no|continue|ok|okay|cancel)';
    const saySingleRegex = new RegExp(
        `\\b(?:just\\s+)?say\\s+(?:"[^"\\n\\r]{1,80}"|“[^”\\n\\r]{1,80}”|'[^'\\n\\r]{1,80}'|‘[^’\\n\\r]{1,80}’|\\b${quickReplyWords}\\b)`,
        'gi'
    );
    const sayOrRegex = new RegExp(
        `\\b(?:just\\s+)?say\\s+(?:"[^"\\n\\r]{1,80}"|“[^”\\n\\r]{1,80}”|'[^'\\n\\r]{1,80}'|‘[^’\\n\\r]{1,80}’|\\b${quickReplyWords}\\b)\\s+or\\s+(?:"[^"\\n\\r]{1,80}"|“[^”\\n\\r]{1,80}”|'[^'\\n\\r]{1,80}'|‘[^’\\n\\r]{1,80}’|\\b${quickReplyWords}\\b)`,
        'gi'
    );

    // Pass 1: handle cases where "say …" / "just say …" is fully contained in a single text node.
    const textNodes = [];
    try {
        const walker = document.createTreeWalker(
            root,
            NodeFilter.SHOW_TEXT,
            {
                acceptNode: (node) => {
                    if (!node || node.nodeType !== Node.TEXT_NODE) {
                        return NodeFilter.FILTER_REJECT;
                    }

                    const text = String(node.textContent ?? '');
                    const lowered = text ? text.toLowerCase() : '';
                    if (!lowered || (!lowered.includes(' just ') && !lowered.includes('just ') && !lowered.includes(' say '))) {
                        return NodeFilter.FILTER_REJECT;
                    }

                    if (shouldSkipNode(node)) {
                        return NodeFilter.FILTER_REJECT;
                    }

                    return NodeFilter.FILTER_ACCEPT;
                }
            },
            false
        );

        let current = walker.nextNode();
        while (current) {
            textNodes.push(current);
            current = walker.nextNode();
        }
    } catch (_) {
        // Ignore.
    }

    for (const node of textNodes) {
        try {
            if (!node || node.nodeType !== Node.TEXT_NODE) continue;
            const raw = String(node.textContent ?? '');
            if (!raw.trim()) continue;

            // Prefer the more specific "say X or Y" match so we can produce two buttons.
            const orMatches = Array.from(raw.matchAll(sayOrRegex));
            const singleMatches = orMatches.length === 0 ? Array.from(raw.matchAll(saySingleRegex)) : [];
            if (orMatches.length === 0 && singleMatches.length === 0) continue;

            const fragment = document.createDocumentFragment();
            let cursor = 0;

            const processSingleMatch = (match) => {
                const matchIndex = match.index;
                if (typeof matchIndex !== 'number') {
                    return;
                }

                const full = String(match[0] ?? '');
                if (!full) {
                    return;
                }

                const start = matchIndex;
                const end = matchIndex + full.length;
                if (start < cursor) {
                    return;
                }

                if (start > cursor) {
                    fragment.appendChild(document.createTextNode(raw.slice(cursor, start)));
                }

                const { leading, reply, trailing } = splitJustSayMatch(full);
                const replyText = normalise(reply);
                if (!isAllowedQuickReply(replyText)) {
                    fragment.appendChild(document.createTextNode(raw.slice(start, end)));
                    cursor = end;
                    return;
                }

                if (leading) {
                    fragment.appendChild(document.createTextNode(leading));
                }

                const wrapper = document.createElement('span');
                wrapper.className = 'chat-insert-prompt-inline-wrapper';
                wrapper.appendChild(createInsertButton(replyText));
                fragment.appendChild(wrapper);

                if (trailing) {
                    fragment.appendChild(document.createTextNode(trailing));
                }

                cursor = end;

            };

            const processOrMatch = (match) => {
                const matchIndex = match.index;
                if (typeof matchIndex !== 'number') {
                    return;
                }

                const full = String(match[0] ?? '');
                if (!full) {
                    return;
                }

                const start = matchIndex;
                const end = matchIndex + full.length;
                if (start < cursor) {
                    return;
                }

                if (start > cursor) {
                    fragment.appendChild(document.createTextNode(raw.slice(cursor, start)));
                }

                const parts = splitSayOrMatch(full);
                if (!parts) {
                    fragment.appendChild(document.createTextNode(raw.slice(start, end)));
                    cursor = end;
                    return;
                }

                const leftParts = splitJustSayMatch(parts.left);
                const rightParts = splitJustSayMatch(parts.right);
                const leftText = normalise(leftParts.reply);
                const rightText = normalise(rightParts.reply);

                if (!isAllowedQuickReply(leftText) || !isAllowedQuickReply(rightText)) {
                    fragment.appendChild(document.createTextNode(raw.slice(start, end)));
                    cursor = end;
                    return;
                }

                // Keep the original prefix, but replace options with buttons.
                const prefix = String(full).replace(/\s+or\s+[\s\S]*$/i, ' ');
                fragment.appendChild(document.createTextNode(prefix));

                const leftWrapper = document.createElement('span');
                leftWrapper.className = 'chat-insert-prompt-inline-wrapper';
                leftWrapper.appendChild(createInsertButton(leftText));
                fragment.appendChild(leftWrapper);

                fragment.appendChild(document.createTextNode(' or '));

                const rightWrapper = document.createElement('span');
                rightWrapper.className = 'chat-insert-prompt-inline-wrapper';
                rightWrapper.appendChild(createInsertButton(rightText));
                fragment.appendChild(rightWrapper);

                cursor = end;
            };

            const chosenMatches = orMatches.length > 0 ? orMatches : singleMatches;
            const handler = orMatches.length > 0 ? processOrMatch : processSingleMatch;

            for (const match of chosenMatches) {
                handler(match);
            }

            if (cursor < raw.length) {
                fragment.appendChild(document.createTextNode(raw.slice(cursor)));
            }

            node.replaceWith(fragment);
        } catch (_) {
            // Ignore.
        }
    }

    const strongEls = Array.from(root.querySelectorAll('strong'));

    // Pass 2: handle "say **X** or **Y**" where both options are separate <strong> nodes.
    for (const strong of strongEls) {
        try {
            if (!strong || !strong.closest) continue;
            if (strong.closest('pre, code, a, button')) continue;
            if (strong.closest('.chat-insert-prompt-wrapper, .chat-insert-prompt-inline-wrapper')) continue;

            const firstText = normalise(extractBoldQuotedInstructionFromStrong(strong));
            if (!firstText || !isAllowedQuickReply(firstText)) {
                continue;
            }

            const prev = strong.previousSibling;
            if (!prev || prev.nodeType !== Node.TEXT_NODE) continue;
            const prevText = String(prev.textContent ?? '');
            if (!/\b(?:just\s+)?say\s*[:\-‑–—]?\s*$/i.test(prevText)) {
                continue;
            }

            const between = strong.nextSibling;
            if (!between || between.nodeType !== Node.TEXT_NODE) continue;
            const betweenText = String(between.textContent ?? '');
            if (!/^\s*or\s*$/i.test(betweenText)) {
                continue;
            }

            const secondStrong = strong.nextElementSibling;
            if (!secondStrong || secondStrong.tagName !== 'STRONG') {
                continue;
            }

            const secondText = normalise(extractBoldQuotedInstructionFromStrong(secondStrong));
            if (!secondText || !isAllowedQuickReply(secondText)) {
                continue;
            }

            const firstWrapper = document.createElement('span');
            firstWrapper.className = 'chat-insert-prompt-inline-wrapper';
            firstWrapper.appendChild(createInsertButton(firstText));
            strong.replaceWith(firstWrapper);

            const secondWrapper = document.createElement('span');
            secondWrapper.className = 'chat-insert-prompt-inline-wrapper';
            secondWrapper.appendChild(createInsertButton(secondText));
            secondStrong.replaceWith(secondWrapper);
        } catch (_) {
            // Ignore.
        }
    }

    // Pass 3: handle markdown like "say **“Proceed”**" where the quoted reply is a separate <strong> node.
    for (const strong of strongEls) {
        try {
            if (!strong || !strong.closest) continue;
            if (strong.closest('pre, code, a, button')) continue;
            if (strong.closest('.chat-insert-prompt-wrapper, .chat-insert-prompt-inline-wrapper')) continue;
            if (strong.querySelector && strong.querySelector('.chat-insert-prompt-button')) continue;

            const replyText = normalise(extractBoldQuotedInstructionFromStrong(strong));
            if (!replyText) continue;
            if (!isAllowedQuickReply(replyText)) continue;

            const prev = strong.previousSibling;
            if (!prev || prev.nodeType !== Node.TEXT_NODE) continue;
            const prevText = String(prev.textContent ?? '');
            if (!/\b(?:just\s+)?say\s*[:\-‑–—]?\s*$/i.test(prevText)) {
                continue;
            }

            const wrapper = document.createElement('span');
            wrapper.className = 'chat-insert-prompt-inline-wrapper';
            wrapper.appendChild(createInsertButton(replyText));
            strong.replaceWith(wrapper);
        } catch (_) {
            // Ignore.
        }
    }
}

function convertReplyOptionsListsToButtons(root) {
    if (!root || !root.querySelectorAll) return;

    const normalise = (value) => String(value ?? '').trim().replace(/\s+/g, ' ');
    const markerPhrases = new Set([
        'please reply with one of:',
        'please reply with one of',
        'tell me how you want to proceed:',
        'tell me how you want to proceed'
    ]);

    const extractQuotedSegments = (value) => {
        const text = String(value ?? '');
        if (!text.trim()) {
            return [];
        }

        const results = [];

        // Straight quotes
        const straight = /"([^\n\r"]{1,400})"/g;
        for (const match of text.matchAll(straight)) {
            results.push(match[1]);
        }

        // Curly quotes
        const curly = /“([^\n\r”]{1,400})”/g;
        for (const match of text.matchAll(curly)) {
            results.push(match[1]);
        }

        // Single quotes (avoid apostrophes; only accept if it looks like a full quoted segment)
        const single = /(^|\s)[‘']([^\n\r’']{1,400})[’'](\s|$)/g;
        for (const match of text.matchAll(single)) {
            results.push(match[2]);
        }

        return results.map((s) => normalise(s)).filter((s) => s);
    };

    const stripSurroundingQuotes = (value) => {
        let text = String(value ?? '').trim();
        if (!text) return '';

        const pairs = [
            ['"', '"'],
            ['“', '”'],
            ["'", "'"],
            ['‘', '’']
        ];

        for (const [start, end] of pairs) {
            if (text.startsWith(start) && text.endsWith(end) && text.length >= start.length + end.length + 1) {
                text = text.slice(start.length, text.length - end.length).trim();
                break;
            }
        }

        return text;
    };

    const markerParas = Array.from(root.querySelectorAll('p'));
    for (const p of markerParas) {
        try {
            if (!p || p.closest('pre, code')) {
                continue;
            }

            const markerText = normalise(p.textContent).toLowerCase();
            if (!markerPhrases.has(markerText)) {
                continue;
            }

            const list = p.nextElementSibling;
            if (!list || (list.tagName !== 'UL' && list.tagName !== 'OL')) {
                continue;
            }

            if (list.dataset && list.dataset.buttonified === '1') {
                continue;
            }

            const items = Array.from(list.querySelectorAll(':scope > li'));
            const options = [];
            for (const li of items) {
                const raw = normalise(li?.textContent);
                if (!raw) {
                    continue;
                }

                const quotedSegments = extractQuotedSegments(raw);
                if (quotedSegments.length > 0) {
                    for (const seg of quotedSegments) {
                        options.push(seg);
                    }
                    continue;
                }

                const stripped = stripSurroundingQuotes(raw);
                options.push(stripped ? stripped : raw);
            }

            if (options.length === 0) {
                continue;
            }
            try {
                list.dataset.buttonified = '1';
            } catch (_) {
                // Ignore.
            }

            while (list.firstChild) {
                list.removeChild(list.firstChild);
            }

            for (const optionText of options) {
                const li = document.createElement('li');

                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'chat-insert-prompt-button';
                btn.textContent = optionText;
                btn.title = 'Insert into chat prompt and send (Shift inserts without sending)';
                btn.setAttribute('aria-label', `Insert into chat prompt: ${optionText}`);
                btn.addEventListener('click', (e) => {
                    try {
                        e.preventDefault();
                        e.stopPropagation();
                    } catch (_) {
                        // Ignore.
                    }

                    insertTextIntoChatPrompt(optionText);

                    const shiftHeld = !!(e && e.shiftKey);
                    if (!shiftHeld) {
                        submitChatPromptImmediately();
                    }
                });

                li.appendChild(btn);
                list.appendChild(li);
            }
        } catch (_) {
            // Ignore detached nodes or DOM mutation races.
        }
    }
}

function convertQuotedInstructionBlockquotesToButtons(root) {
    if (!root || !root.querySelectorAll) return;

    const blocks = Array.from(root.querySelectorAll('blockquote'));
    for (const block of blocks) {
        try {
            // Match the exact pattern (as rendered HTML):
            // <blockquote><p><strong>"..."</strong></p></blockquote>
            // i.e., one direct <p>, containing only one direct <strong>, whose content is fully quoted.
            const elementChildren = Array.from(block.children ?? []);
            if (elementChildren.length !== 1) continue;
            const p = elementChildren[0];
            if (!p || p.tagName !== 'P') continue;

            // No non-whitespace text nodes at the blockquote level.
            const blockNodes = Array.from(block.childNodes ?? []);
            if (blockNodes.some((n) => n.nodeType === Node.TEXT_NODE && String(n.textContent ?? '').trim())) {
                continue;
            }

            const pElementChildren = Array.from(p.children ?? []);
            const strongCandidates = pElementChildren.filter((el) => el && el.tagName === 'STRONG');
            const normalise = (s) => String(s ?? '').trim().replace(/\s+/g, ' ');

            let instruction = null;

            if (strongCandidates.length === 1) {
                const strong = strongCandidates[0];

                // Allow harmless formatting elements that do not contribute text.
                if (pElementChildren.some((el) => el !== strong && el.tagName !== 'BR')) {
                    continue;
                }

                // No non-whitespace text nodes inside the paragraph.
                const pNodes = Array.from(p.childNodes ?? []);
                if (pNodes.some((n) => n.nodeType === Node.TEXT_NODE && String(n.textContent ?? '').trim())) {
                    continue;
                }

                instruction = extractBoldQuotedInstructionFromStrong(strong);
                if (!instruction) {
                    const candidate = normalise(strong.textContent);
                    if (isAllowedUnquotedBlockquoteInstruction(candidate)) {
                        instruction = candidate;
                    }
                }
                if (!instruction) continue;

                // Ensure the blockquote text is exactly the expected paragraph text (no extra content).
                if (normalise(block.textContent) !== normalise(p.textContent)) continue;
            } else {
                // Also support plain quoted text without bold:
                // <blockquote><p>“...”</p></blockquote>
                // Allow only <br> tags and no other nested markup.
                if (pElementChildren.some((el) => el && el.tagName !== 'BR')) {
                    continue;
                }

                const pNodes = Array.from(p.childNodes ?? []);
                if (pNodes.some((n) => n.nodeType === Node.ELEMENT_NODE && n.tagName && n.tagName !== 'BR')) {
                    continue;
                }

                instruction = extractQuotedInstruction(p.textContent);
                if (!instruction) continue;
                if (normalise(block.textContent) !== normalise(p.textContent)) continue;
            }

            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'chat-insert-prompt-button';
            btn.textContent = instruction;
            btn.title = 'Insert into chat prompt and send (Shift inserts without sending)';
            btn.setAttribute('aria-label', `Insert into chat prompt: ${instruction}`);
            btn.addEventListener('click', (e) => {
                try {
                    e.preventDefault();
                    e.stopPropagation();
                } catch (_) {
                    // Ignore.
                }
                insertTextIntoChatPrompt(instruction);

                const shiftHeld = !!(e && e.shiftKey);
                if (!shiftHeld) {
                    submitChatPromptImmediately();
                }
            });

            const wrapper = document.createElement('div');
            wrapper.className = 'chat-insert-prompt-wrapper';
            wrapper.appendChild(btn);
            block.replaceWith(wrapper);
        } catch (_) {
            // Ignore detached nodes or DOM mutation races.
        }
    }
}

function convertInlineQuotedStrongSegmentsToButtons(root) {
    if (!root || !root.querySelectorAll) return;

    const strongEls = Array.from(root.querySelectorAll('strong'));
    for (const strong of strongEls) {
        try {
            if (!strong || !strong.closest) continue;
            if (strong.closest('pre, code, a, button')) continue;
            if (strong.closest('blockquote')) continue;
            if (strong.closest('ul, ol')) continue;
            if (strong.closest('.chat-insert-prompt-wrapper')) continue;

            const instruction = extractBoldQuotedInstructionFromStrong(strong);
            if (!instruction) continue;
            if (!shouldButtonifyInlineQuotedInstruction(instruction)) continue;

            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'chat-insert-prompt-button';
            btn.textContent = instruction;
            btn.title = 'Insert into chat prompt and send (Shift inserts without sending)';
            btn.setAttribute('aria-label', `Insert into chat prompt: ${instruction}`);
            btn.addEventListener('click', (e) => {
                try {
                    e.preventDefault();
                    e.stopPropagation();
                } catch (_) {
                    // Ignore.
                }
                insertTextIntoChatPrompt(instruction);

                const shiftHeld = !!(e && e.shiftKey);
                if (!shiftHeld) {
                    submitChatPromptImmediately();
                }
            });

            const wrapper = document.createElement('span');
            wrapper.className = 'chat-insert-prompt-inline-wrapper';
            wrapper.appendChild(btn);
            strong.replaceWith(wrapper);
        } catch (_) {
            // Ignore detached nodes or DOM mutation races.
        }
    }
}

function convertQuotedInstructionListItemsToButtons(root) {
    if (!root || !root.querySelectorAll) return;

    const listItems = Array.from(root.querySelectorAll('li'));
    for (const li of listItems) {
        try {
            if (!li || !li.closest) continue;
            if (li.closest('pre, code, a, button')) continue;
            if (li.closest('.chat-insert-prompt-wrapper, .chat-insert-prompt-inline-wrapper')) continue;

            if (li.querySelector('.chat-insert-prompt-button')) {
                continue;
            }

            const elementChildren = Array.from(li.children ?? []);
            const strongCandidates = elementChildren.filter((el) => el && el.tagName === 'STRONG');
            if (strongCandidates.length !== 1) continue;
            const strong = strongCandidates[0];

            if (elementChildren.some((el) => el !== strong && el.tagName !== 'BR')) {
                continue;
            }

            const liNodes = Array.from(li.childNodes ?? []);
            if (liNodes.some((n) => n.nodeType === Node.TEXT_NODE && String(n.textContent ?? '').trim())) {
                continue;
            }

            const instruction = extractBoldQuotedInstructionFromStrong(strong);
            if (!instruction) continue;

            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'chat-insert-prompt-button';
            btn.textContent = instruction;
            btn.title = 'Insert into chat prompt and send (Shift inserts without sending)';
            btn.setAttribute('aria-label', `Insert into chat prompt: ${instruction}`);
            btn.addEventListener('click', (e) => {
                try {
                    e.preventDefault();
                    e.stopPropagation();
                } catch (_) {
                    // Ignore.
                }
                insertTextIntoChatPrompt(instruction);

                const shiftHeld = !!(e && e.shiftKey);
                if (!shiftHeld) {
                    submitChatPromptImmediately();
                }
            });

            const wrapper = document.createElement('span');
            wrapper.className = 'chat-insert-prompt-inline-wrapper';
            wrapper.appendChild(btn);
            strong.replaceWith(wrapper);
        } catch (_) {
            // Ignore detached nodes or DOM mutation races.
        }
    }
}

// Cache concept metadata used for cartouches in chat transcript.
// Map<fullId, { name: string, kind: string, source?: string, provisional?: boolean }>
const chatConceptMetaCache = new Map();
// Map<fullId, Promise<meta|null>> for in-flight lookups.
const chatConceptMetaPending = new Map();

const CHAT_CONCEPT_META_MAX_RETRIES = 3;
const chatConceptMetaRetryCounts = new Map();
const chatConceptMetaRetryTimers = new Map();

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
    convertQuotedInstructionBlockquotesToButtons(container);
    convertInlineQuotedStrongSegmentsToButtons(container);
    convertJustSayInstructionsToButtons(container);
    convertReplyOptionsListsToButtons(container);
    convertQuotedInstructionListItemsToButtons(container);
    try {
        container.dataset.renderMode = 'rendered';
    } catch (_) {
        // Ignore.
    }

    // Preserve clickable #V# tokens; cartouchify where allowed, otherwise linkify in-place.
    cartouchifyVontologyTokensInElement(container, { skipSelectors: ['pre', 'code', 'a'], allowStandaloneCodeTokens: true, allowStandaloneCodeBlockTokens: true });
    linkifyVontologyTokensInElement(container, { skipSelectors: ['a', '.vontology-cartouche', 'button'], plain: true });
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
        convertQuotedInstructionBlockquotesToButtons(messageTextEl);
        convertInlineQuotedStrongSegmentsToButtons(messageTextEl);
        convertJustSayInstructionsToButtons(messageTextEl);
        convertQuotedInstructionListItemsToButtons(messageTextEl);
        cartouchifyVontologyTokensInElement(messageTextEl, { skipSelectors: ['pre', 'code', 'a'], allowStandaloneCodeTokens: true, allowStandaloneCodeBlockTokens: true });
        linkifyVontologyTokensInElement(messageTextEl, { skipSelectors: ['a', '.vontology-cartouche', 'button'], plain: true });
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
        const nodeUrl = `/vontology/api/vontology/node_content?identifier=${encodeURIComponent(fullId)}&raw_only=1&soft=1`;
        const nodeRes = await fetch(nodeUrl, { cache: 'no-store' });
        if (nodeRes.ok) {
            const node = await nodeRes.json();
            const looksMissing =
                node &&
                node.concept_id == null &&
                node.raw_doc == null &&
                node.display_name == null &&
                node.kind == null;

            if (node && (node.not_found || node.error || looksMissing)) {
                return null;
            }
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
            return { name: String(name), kind: String(kind), source: 'node_content' };
        }

        // Fallback to search endpoint if node_content is unavailable.
        const url = `/vontology/api/vontology/search?q=${encodeURIComponent(fullId)}&limit=8`;
        const res = await fetch(url, { cache: 'no-store' });
        if (!res.ok) return null;

        const data = await res.json();
        const results = Array.isArray(data?.results) ? data.results : [];
        const match = results.find(r => r && r.id === fullId);
        if (!match || !match.id) return null;

        return {
            name: match.name || match.id,
            kind: match.kind || 'type',
            source: 'search',
            provisional: true
        };
    } catch (err) {
        console.debug('[chatTab] fetchConceptMetaForChat failed', err);
        return null;
    }
}

async function fetchConceptMetaForChatNodeOnly(fullId) {
    try {
        const nodeUrl = `/vontology/api/vontology/node_content?identifier=${encodeURIComponent(fullId)}&raw_only=1&soft=1`;
        const nodeRes = await fetch(nodeUrl, { cache: 'no-store' });
        if (!nodeRes.ok) {
            return null;
        }

        const node = await nodeRes.json();
        const looksMissing =
            node &&
            node.concept_id == null &&
            node.raw_doc == null &&
            node.display_name == null &&
            node.kind == null;

        if (node && (node.not_found || node.error || looksMissing)) {
            return null;
        }

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

        return { name: String(name), kind: String(kind), source: 'node_content' };
    } catch (err) {
        console.debug('[chatTab] fetchConceptMetaForChatNodeOnly failed', err);
        return null;
    }
}

function shouldRetryChatConceptMeta(fullId, meta) {
    if (!fullId) {
        return false;
    }

    if (!meta) {
        return true;
    }

    if (meta.provisional === true) {
        return true;
    }

    const name = typeof meta.name === 'string' ? meta.name : '';
    if (name === fullId) {
        return true;
    }

    return false;
}

function scheduleChatConceptMetaRetry(fullId) {
    if (!fullId) {
        return;
    }

    const retries = chatConceptMetaRetryCounts.get(fullId) || 0;
    if (retries >= CHAT_CONCEPT_META_MAX_RETRIES) {
        return;
    }

    if (chatConceptMetaRetryTimers.has(fullId)) {
        return;
    }

    const delayMs = retries === 0 ? 250 : retries === 1 ? 1000 : 2500;
    const timerId = setTimeout(() => {
        chatConceptMetaRetryTimers.delete(fullId);
        chatConceptMetaRetryCounts.set(fullId, retries + 1);

        const shouldTrackPending = !chatConceptMetaPending.has(fullId);
        const p = fetchConceptMetaForChatNodeOnly(fullId).then((meta) => {
            if (shouldTrackPending) {
                chatConceptMetaPending.delete(fullId);
            }

            if (meta) {
                chatConceptMetaCache.set(fullId, meta);
                const els = Array.from(document.querySelectorAll('.vontology-cartouche[data-full-concept-id]'));
                els
                    .filter(el => el.dataset.fullConceptId === fullId)
                    .forEach(el => updateCartoucheElement(el, meta));
            } else {
                scheduleChatConceptMetaRetry(fullId);
            }

            return meta;
        });

        if (shouldTrackPending) {
            chatConceptMetaPending.set(fullId, p);
        }
    }, delayMs);

    chatConceptMetaRetryTimers.set(fullId, timerId);
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

            if (shouldRetryChatConceptMeta(fullId, cached)) {
                scheduleChatConceptMetaRetry(fullId);
            }
            continue;
        }

        if (!chatConceptMetaPending.has(fullId)) {
            const p = fetchConceptMetaForChat(fullId).then((meta) => {
                chatConceptMetaPending.delete(fullId);

                // Avoid caching null or provisional results indefinitely; newly-created concepts can
                // race indexing, and we want a short retry window to auto-hydrate.
                if (meta) {
                    chatConceptMetaCache.set(fullId, meta);
                }

                if (shouldRetryChatConceptMeta(fullId, meta)) {
                    scheduleChatConceptMetaRetry(fullId);
                }
                return meta;
            });
            chatConceptMetaPending.set(fullId, p);
            scheduleChatConceptMetaRetry(fullId);
        }

        chatConceptMetaPending.get(fullId)
            .then((meta) => {
                cartouches
                    .filter(el => el.dataset.fullConceptId === fullId)
                    .forEach(el => updateCartoucheElement(el, meta));

                if (shouldRetryChatConceptMeta(fullId, meta)) {
                    scheduleChatConceptMetaRetry(fullId);
                }
            })
            .catch(() => {
                // Ignore lookup failures; leave placeholders.
                scheduleChatConceptMetaRetry(fullId);
            });
    }
}

// Export for testing.
export function __testOnly_resetChatConceptMetaCaches() {
    chatConceptMetaCache.clear();
    chatConceptMetaPending.clear();
    chatConceptMetaRetryCounts.clear();
    for (const timerId of chatConceptMetaRetryTimers.values()) {
        try {
            clearTimeout(timerId);
        } catch (_) {
            // Ignore.
        }
    }
    chatConceptMetaRetryTimers.clear();
}

// Export for testing.
export function __testOnly_hydrateChatConceptCartouches(root) {
    hydrateChatConceptCartouches(root);
}

// Export for testing.
export function __testOnly_convertQuotedInstructionBlockquotesToButtons(root) {
    convertQuotedInstructionBlockquotesToButtons(root);
}

// Export for testing.
export function __testOnly_convertReplyOptionsListsToButtons(root) {
    convertReplyOptionsListsToButtons(root);
}

// Export for testing.
export function __testOnly_convertInlineQuotedStrongSegmentsToButtons(root) {
    convertInlineQuotedStrongSegmentsToButtons(root);
}

// Export for testing.
export function __testOnly_convertQuotedInstructionListItemsToButtons(root) {
    convertQuotedInstructionListItemsToButtons(root);
}

function deriveLlmDebugWarnings(debugData) {
    const warnings = [];
    if (!debugData || typeof debugData !== 'object') {
        return warnings;
    }

    const responseText = (typeof debugData.response === 'string') ? debugData.response : '';
    const toolInvocations = Array.isArray(debugData.tool_invocations) ? debugData.tool_invocations : [];
    const toolStatsCount = (debugData.tool_stats && Number.isFinite(debugData.tool_stats.tool_count))
        ? Number(debugData.tool_stats.tool_count)
        : null;
    const executedToolCount = toolInvocations.length || (toolStatsCount ?? 0);

    // Surface tool invocation failures / parse errors so it is obvious when tool
    // use was intended but did not execute.
    for (const inv of toolInvocations) {
        if (!inv || typeof inv !== 'object') {
            continue;
        }

        const method = (typeof inv.method === 'string')
            ? inv.method
            : ((typeof inv.tool === 'string') ? inv.tool : 'unknown');

        const errorText = (typeof inv.error === 'string') ? inv.error.trim() : '';
        if (!errorText) {
            continue;
        }

        if (method === '__tool_call_parse_error__') {
            warnings.push(errorText);
        } else {
            warnings.push(`Tool ${method} failed: ${errorText}`);
        }
    }

    // Detect when we reached the max tool invocation cap but the response still
    // looks like a tool call (i.e., the LLM wanted more tools than we executed).
    const maxToolInvocations = (debugData.internal_mcp && debugData.internal_mcp.execution_caps && Number.isFinite(debugData.internal_mcp.execution_caps.max_tool_invocations))
        ? Number(debugData.internal_mcp.execution_caps.max_tool_invocations)
        : null;

    if (maxToolInvocations != null && maxToolInvocations > 0 && toolInvocations.length >= maxToolInvocations) {
        const trimmed = String(responseText || '').trim();
        const looksLikeToolCall = (trimmed.startsWith('{') || trimmed.startsWith('['))
            && trimmed.includes('"call_tool"')
            && trimmed.includes('"tool"');
        if (looksLikeToolCall) {
            warnings.push(
                `Reached max tool invocation limit (${maxToolInvocations}); additional tool calls were not executed.`
            );
        }
    }

    // Heuristic: detect when the assistant claims it performed N operations but we
    // only executed M tool calls. This often indicates tool truncation/early stop.
    // (e.g., “Done — 9 links” but tool_invocations contains 4 items).
    if (responseText) {
        const patterns = [
            /\b(?:done|complete|completed)\s*[—\-:]\s*(\d+)\b/gi,
            /\b(?:added|linked|created|removed|updated|executed)\s+(\d+)\b/gi,
            /\b(\d+)\s+(?:links?|relationships?|relations?|tool\s*calls?|tools?)\b/gi
        ];

        let claimed = null;
        for (const re of patterns) {
            let match;
            while ((match = re.exec(responseText)) !== null) {
                const n = Number(match[1]);
                if (Number.isFinite(n)) {
                    claimed = claimed == null ? n : Math.max(claimed, n);
                }
            }
        }

        if (claimed != null && claimed > 0 && executedToolCount >= 0 && claimed > executedToolCount) {
            warnings.push(
                `Response claims ${claimed} operations, but only ${executedToolCount} tool invocations were recorded.`
            );
        }
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

    // Presenter channel health (screen/spoken routes)
    const presenterChannels = normalisePresenterChannels(debugData.presenter_channels);
    if (presenterChannels) {
        const hasScreen = typeof presenterChannels.screen === 'string' && presenterChannels.screen.trim();
        const hasSpoken = typeof presenterChannels.spoken === 'string' && presenterChannels.spoken.trim();

        if (hasScreen && !hasSpoken) {
            warnings.push('Presenter output missing spoken channel; text-to-speech will fall back to screen text.');
        } else if (hasSpoken && !hasScreen) {
            warnings.push('Presenter output missing screen channel; display will fall back to spoken text.');
        } else if (!hasScreen && !hasSpoken) {
            warnings.push('Presenter output present but both screen and spoken channels are empty.');
        }
    }

    const spokenBackfillAttempted = !!debugData.spoken_backfill_second_pass_attempted;
    if (spokenBackfillAttempted) {
        const spokenStillMissing = !(presenterChannels?.spoken && presenterChannels.spoken.trim());
        if (spokenStillMissing) {
            const reason = (typeof debugData.spoken_backfill_second_pass_reason === 'string' && debugData.spoken_backfill_second_pass_reason.trim())
                ? debugData.spoken_backfill_second_pass_reason.trim()
                : 'unknown_reason';
            warnings.push(`Spoken backfill attempted but spoken channel is still missing (${reason}).`);
        }
    }

    const speechPlayback = debugData.speech_playback;
    if (speechPlayback && typeof speechPlayback === 'object' && speechPlayback.duration_suspect_too_long) {
        const actualMs = Number.isFinite(speechPlayback.actual_duration_ms)
            ? speechPlayback.actual_duration_ms
            : null;
        const expectedMs = Number.isFinite(speechPlayback.expected_duration_ms)
            ? speechPlayback.expected_duration_ms
            : null;
        const thresholdSec = Number.isFinite(speechPlayback.duration_threshold_sec)
            ? speechPlayback.duration_threshold_sec
            : null;

        const actualSec = actualMs !== null ? (actualMs / 1000) : null;
        const expectedSec = expectedMs !== null ? (expectedMs / 1000) : null;

        const details = [];
        if (actualSec !== null) {
            details.push(`actual ${actualSec.toFixed(1)}s`);
        } else if (expectedSec !== null) {
            details.push(`expected ${expectedSec.toFixed(1)}s`);
        }
        if (thresholdSec !== null) {
            details.push(`threshold ${thresholdSec}s`);
        }

        warnings.push(
            `Narration playback duration exceeded long-duration threshold${details.length ? ` (${details.join(', ')})` : ''}.`
        );
    }

    return Array.from(new Set(warnings));
}

// Export for testing.
export function __testOnly_deriveLlmDebugWarnings(debugData) {
    return deriveLlmDebugWarnings(debugData);
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

const CHAT_TTS_STORAGE_KEY = 'chatTtsEnabled';

const CHAT_TTS_VOICE_URI_STORAGE_KEY = 'chatTtsVoiceUri';
const CHAT_TTS_LANGUAGE_STORAGE_KEY = 'chatTtsLanguage';
const CHAT_TTS_RATE_STORAGE_KEY = 'chatTtsRate';
const CHAT_TTS_PITCH_STORAGE_KEY = 'chatTtsPitch';
const CHAT_TTS_VOLUME_STORAGE_KEY = 'chatTtsVolume';
const CHAT_TTS_LONG_DURATION_THRESHOLD_KEY = 'chatTtsLongDurationThresholdSec';
const DEFAULT_TTS_LONG_DURATION_THRESHOLD_SEC = 30;
const CHAT_TTS_MAX_SPEAKING_SECONDS_KEY = 'chatTtsMaxSpeakingSeconds';
const DEFAULT_TTS_MAX_SPEAKING_SECONDS = 40;

const CHAT_STT_LANGUAGE_STORAGE_KEY = 'chatSttLanguage';
const CHAT_STT_CONTINUOUS_STORAGE_KEY = 'chatSttContinuous';
const CHAT_STT_INTERIM_RESULTS_STORAGE_KEY = 'chatSttInterimResults';

let activeDictation = null;
let dictationState = null;

let activeTtsTurnId = null;
let activeTtsButton = null;
let activeTtsPlaybackState = null;
let activeTtsTimeoutId = null;

function safeLocalStorageGet(key) {
    try {
        if (typeof localStorage === 'undefined') {
            return null;
        }
        return localStorage.getItem(key);
    } catch (_) {
        return null;
    }
}

function safeLocalStorageSet(key, value) {
    try {
        if (typeof localStorage === 'undefined') {
            return;
        }
        localStorage.setItem(key, value);
    } catch (_) {
        // Ignore.
    }
}

function getPreferredChatLanguage() {
    const ctx = getUserContext();
    const lang = (ctx && ctx.language) ? String(ctx.language).trim() : '';
    return lang || 'en-NZ';
}

function clampNumber(value, minValue, maxValue, fallbackValue) {
    const num = Number(value);
    if (!Number.isFinite(num)) {
        return fallbackValue;
    }
    return Math.min(Math.max(num, minValue), maxValue);
}

function normaliseLanguageSetting(value) {
    const lang = String(value ?? '').trim();
    return lang;
}

function parseBoolSetting(value, fallbackValue) {
    if (value === null || value === undefined) {
        return fallbackValue;
    }
    return String(value) === 'true';
}

function getTtsLongDurationThresholdSec() {
    const raw = safeLocalStorageGet(CHAT_TTS_LONG_DURATION_THRESHOLD_KEY);
    const parsed = Number(raw);
    if (Number.isFinite(parsed) && parsed > 0) {
        return Math.min(Math.max(parsed, 5), 300);
    }

    // Default to the user-configured maximum speaking duration so UI settings are respected.
    // (The dedicated long-duration threshold key is not exposed in the settings UI.)
    const maxSpeakingSeconds = getTtsMaxSpeakingSeconds();
    if (Number.isFinite(maxSpeakingSeconds) && maxSpeakingSeconds > 0) {
        return Math.min(Math.max(maxSpeakingSeconds, 5), 600);
    }

    return DEFAULT_TTS_LONG_DURATION_THRESHOLD_SEC;
}

function getTtsMaxSpeakingSeconds() {
    const raw = safeLocalStorageGet(CHAT_TTS_MAX_SPEAKING_SECONDS_KEY);
    const parsed = Number(raw);
    if (Number.isFinite(parsed) && parsed > 0) {
        return Math.min(Math.max(parsed, 10), 600);
    }
    return DEFAULT_TTS_MAX_SPEAKING_SECONDS;
}

function clearActiveTtsTimeout() {
    if (!activeTtsTimeoutId) {
        return;
    }
    try { clearTimeout(activeTtsTimeoutId); } catch (_) { }
    activeTtsTimeoutId = null;
}

function estimateSpeechDurationMs(text, rate) {
    const cleaned = String(text ?? '').trim();
    const chars = cleaned.length;
    const words = cleaned ? cleaned.split(/\s+/).filter(Boolean).length : 0;
    if (!chars) {
        return { expectedMs: null, estimateMethod: null, words: 0, chars: 0 };
    }

    const safeRate = Number.isFinite(rate) && rate > 0 ? rate : 1;
    const baseWpm = 180;
    if (words > 0) {
        const wpm = baseWpm * safeRate;
        return {
            expectedMs: Math.round((words / wpm) * 60 * 1000),
            estimateMethod: 'words_per_minute',
            words,
            chars
        };
    }

    const charsPerSec = 12 * safeRate;
    return {
        expectedMs: Math.round((chars / charsPerSec) * 1000),
        estimateMethod: 'chars_per_sec',
        words,
        chars
    };
}

function resolveSpeechVoiceInfo(voiceUri) {
    const uri = String(voiceUri || '').trim();
    if (!uri) {
        return { voice_uri: null, voice_name: null };
    }

    const voices = getSpeechSynthesisVoices();
    const match = voices.find((voice) => voice && String(voice.voiceURI || '') === uri) || null;
    const voiceName = match && match.name ? String(match.name).trim() : null;

    return {
        voice_uri: uri,
        voice_name: voiceName || null
    };
}

function buildSpeechPlaybackTelemetry(state, stopReason) {
    if (!state || typeof state !== 'object') {
        return null;
    }

    const startedAtMs = Number.isFinite(state.startedAtMs) ? state.startedAtMs : null;
    const endedAtMs = Number.isFinite(state.endedAtMs) ? state.endedAtMs : null;
    const actualDurationMs = (startedAtMs !== null && endedAtMs !== null)
        ? Math.max(0, endedAtMs - startedAtMs)
        : null;

    const expectedMs = Number.isFinite(state.expectedDurationMs) ? state.expectedDurationMs : null;
    const thresholdSec = Number.isFinite(state.thresholdSec) ? state.thresholdSec : null;

    const actualSec = actualDurationMs !== null ? actualDurationMs / 1000 : null;
    const expectedSec = expectedMs !== null ? expectedMs / 1000 : null;

    const durationTooLong = (actualSec !== null && thresholdSec !== null)
        ? actualSec > thresholdSec
        : (expectedSec !== null && thresholdSec !== null ? expectedSec > thresholdSec : false);

    return {
        request_id: state.requestId || null,
        turn_id: state.turnId || null,
        started_at: state.startedAt || null,
        ended_at: state.endedAt || null,
        actual_duration_ms: actualDurationMs,
        expected_duration_ms: expectedMs,
        estimate_method: state.estimateMethod || null,
        duration_threshold_sec: thresholdSec,
        duration_suspect_too_long: durationTooLong,
        tts_source: state.ttsSource || null,
        tts_chars: Number.isFinite(state.ttsChars) ? state.ttsChars : null,
        tts_words: Number.isFinite(state.ttsWords) ? state.ttsWords : null,
        stop_reason: stopReason || null,
        tts_settings: {
            language: state.ttsLanguage || null,
            rate: state.ttsRate ?? null,
            pitch: state.ttsPitch ?? null,
            volume: state.ttsVolume ?? null,
            voice_uri: state.voiceUri || null,
            voice_name: state.voiceName || null
        }
    };
}

async function postSpeechPlaybackTelemetry(turnId, telemetry) {
    if (!telemetry || typeof telemetry !== 'object') {
        return;
    }

    const payload = {
        conversation_id: elements.conversationId || 'local',
        turn_id: turnId,
        request_id: telemetry.request_id || null,
        speech_playback: telemetry,
        context: getUserContext()
    };

    try {
        await fetch('/api/speech/telemetry', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
    } catch (err) {
        console.warn('[speech] Telemetry post failed:', err);
    }
}

function recordSpeechPlaybackTelemetry(turnId, telemetry) {
    if (!telemetry || typeof telemetry !== 'object') {
        return;
    }

    const debugData = turnId ? llmDebugData.get(turnId) : null;
    if (debugData && typeof debugData === 'object') {
        const updated = {
            ...debugData,
            speech_playback: telemetry
        };
        llmDebugData.set(turnId, updated);
    }

    if (telemetry.duration_suspect_too_long) {
        console.warn('[speech] Long narration duration detected:', telemetry);
    } else {
        console.info('[speech] Narration playback telemetry:', telemetry);
    }

    void postSpeechPlaybackTelemetry(turnId, telemetry);
}

function getChatSpeechSettings() {
    const preferredLanguage = getPreferredChatLanguage();

    const ttsVoiceUri = String(safeLocalStorageGet(CHAT_TTS_VOICE_URI_STORAGE_KEY) || '').trim();
    const ttsLanguageRaw = safeLocalStorageGet(CHAT_TTS_LANGUAGE_STORAGE_KEY);
    const ttsLanguage = normaliseLanguageSetting(ttsLanguageRaw) || preferredLanguage;

    const ttsRate = clampNumber(safeLocalStorageGet(CHAT_TTS_RATE_STORAGE_KEY), 0.5, 2, 1);
    const ttsPitch = clampNumber(safeLocalStorageGet(CHAT_TTS_PITCH_STORAGE_KEY), 0, 2, 1);
    const ttsVolume = clampNumber(safeLocalStorageGet(CHAT_TTS_VOLUME_STORAGE_KEY), 0, 1, 1);

    const sttLanguageRaw = safeLocalStorageGet(CHAT_STT_LANGUAGE_STORAGE_KEY);
    const sttLanguage = normaliseLanguageSetting(sttLanguageRaw) || preferredLanguage;

    const sttContinuous = parseBoolSetting(
        safeLocalStorageGet(CHAT_STT_CONTINUOUS_STORAGE_KEY),
        true
    );
    const sttInterimResults = parseBoolSetting(
        safeLocalStorageGet(CHAT_STT_INTERIM_RESULTS_STORAGE_KEY),
        true
    );

    return {
        tts: {
            voiceUri: ttsVoiceUri || null,
            language: ttsLanguage,
            rate: ttsRate,
            pitch: ttsPitch,
            volume: ttsVolume
        },
        stt: {
            language: sttLanguage,
            continuous: sttContinuous,
            interimResults: sttInterimResults
        }
    };
}

function formatVoiceOptionLabel(voice) {
    if (!voice) {
        return 'Unknown voice';
    }
    const name = String(voice.name || 'Unknown');
    const lang = String(voice.lang || '').trim();
    return lang ? `${name} (${lang})` : name;
}

function populateTtsVoiceSelect(selectEl, selectedVoiceUri) {
    if (!selectEl) {
        return;
    }

    const keepFirst = selectEl.querySelector('option[value=""]');
    selectEl.innerHTML = '';
    if (keepFirst) {
        selectEl.appendChild(keepFirst);
    } else {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = 'Default';
        selectEl.appendChild(opt);
    }

    const voices = getSpeechSynthesisVoices();
    const sorted = voices
        .slice()
        .filter((v) => v && v.voiceURI)
        .sort((a, b) => formatVoiceOptionLabel(a).localeCompare(formatVoiceOptionLabel(b)));

    for (const voice of sorted) {
        const opt = document.createElement('option');
        opt.value = String(voice.voiceURI);
        opt.textContent = formatVoiceOptionLabel(voice);
        selectEl.appendChild(opt);
    }

    if (selectedVoiceUri) {
        selectEl.value = String(selectedVoiceUri);
    }
}

function isChatTtsEnabled() {
    const toggle = document.getElementById('ttsToggle');
    if (toggle && typeof toggle.checked === 'boolean') {
        return !!toggle.checked;
    }
    return safeLocalStorageGet(CHAT_TTS_STORAGE_KEY) === 'true';
}

function clearActiveTtsUi() {
    if (activeTtsButton) {
        activeTtsButton.classList.remove('active');
        activeTtsButton.textContent = 'Speak';
        activeTtsButton.title = 'Speak this response aloud';
    }
    activeTtsTurnId = null;
    activeTtsButton = null;
    clearActiveTtsTimeout();
}

function toggleSpeakTurn(turnId, text, button) {
    if (!turnId || !button) {
        return;
    }

    if (!isTextToSpeechSupported()) {
        return;
    }

    const trimmed = String(text ?? '').trim();
    if (!trimmed) {
        return;
    }

    const isAlreadyActive = activeTtsTurnId === turnId;
    if (isAlreadyActive) {
        if (activeTtsPlaybackState && activeTtsPlaybackState.turnId === turnId) {
            activeTtsPlaybackState.stopRequested = true;
        }
        stopSpeaking();
        clearActiveTtsUi();
        return;
    }

    // Stop any previous speech and update the previous button state.
    stopSpeaking();
    clearActiveTtsUi();

    activeTtsTurnId = turnId;
    activeTtsButton = button;

    button.classList.add('active');
    button.textContent = 'Stop';
    button.title = 'Stop speaking';

    let utterance = null;
    try {
        const settings = getChatSpeechSettings();
        const debugData = llmDebugData.get(turnId);
        const estimate = estimateSpeechDurationMs(trimmed, settings.tts.rate);
        const voiceInfo = resolveSpeechVoiceInfo(settings.tts.voiceUri);
        const thresholdSec = getTtsLongDurationThresholdSec();
        const maxSpeakingSeconds = getTtsMaxSpeakingSeconds();
        const ttsSource = debugData?.speech_planning?.tts_source || null;

        activeTtsPlaybackState = {
            turnId,
            requestId: debugData?.request_id || null,
            expectedDurationMs: estimate.expectedMs,
            estimateMethod: estimate.estimateMethod,
            ttsChars: estimate.chars,
            ttsWords: estimate.words,
            thresholdSec,
            maxSpeakingSeconds,
            ttsSource,
            ttsLanguage: settings.tts.language,
            ttsRate: settings.tts.rate,
            ttsPitch: settings.tts.pitch,
            ttsVolume: settings.tts.volume,
            voiceUri: voiceInfo.voice_uri,
            voiceName: voiceInfo.voice_name,
            startedAtMs: null,
            endedAtMs: null,
            startedAt: null,
            endedAt: null,
            stopRequested: false
        };

        utterance = speakText(trimmed, {
            language: settings.tts.language,
            rate: settings.tts.rate,
            pitch: settings.tts.pitch,
            volume: settings.tts.volume,
            voiceUri: settings.tts.voiceUri
        });
    } catch (err) {
        console.warn('[chatTab] TTS failed:', err);
        clearActiveTtsUi();
        return;
    }

    const finish = (reason) => {
        if (activeTtsPlaybackState && activeTtsPlaybackState.turnId === turnId) {
            activeTtsPlaybackState.endedAtMs = Date.now();
            activeTtsPlaybackState.endedAt = new Date(activeTtsPlaybackState.endedAtMs).toISOString();
            const stopReason = activeTtsPlaybackState.stopRequested ? 'cancelled' : reason;
            const telemetry = buildSpeechPlaybackTelemetry(activeTtsPlaybackState, stopReason);
            recordSpeechPlaybackTelemetry(turnId, telemetry);
            activeTtsPlaybackState = null;
        }
        if (activeTtsTurnId === turnId) {
            clearActiveTtsUi();
        }
    };

    const maxSeconds = getTtsMaxSpeakingSeconds();
    if (Number.isFinite(maxSeconds) && maxSeconds >= 10) {
        clearActiveTtsTimeout();
        activeTtsTimeoutId = setTimeout(() => {
            if (activeTtsPlaybackState && activeTtsPlaybackState.turnId === turnId) {
                activeTtsPlaybackState.stopRequested = true;
            }
            stopSpeaking();
            finish('timeout');
        }, Math.round(maxSeconds * 1000));
    }

    try {
        utterance.onstart = () => {
            if (activeTtsPlaybackState && activeTtsPlaybackState.turnId === turnId) {
                activeTtsPlaybackState.startedAtMs = Date.now();
                activeTtsPlaybackState.startedAt = new Date(activeTtsPlaybackState.startedAtMs).toISOString();
            }
        };
        utterance.onend = () => finish('ended');
        utterance.onerror = () => finish('error');
    } catch (_) {
        // Ignore.
    }
}

function stopDictation() {
    if (!activeDictation) {
        return;
    }

    try {
        activeDictation.stop?.();
    } catch (_) {
        // Ignore.
    }
}

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

function setActiveChatSession(sessionId, sessionName) {
    activeChatSessionId = (typeof sessionId === 'string' && sessionId.trim())
        ? sessionId.trim()
        : null;
    activeChatSessionName = (typeof sessionName === 'string' && sessionName.trim())
        ? sessionName.trim()
        : null;
}

function getChatSessionTabsContainer() {
    return document.getElementById('chatSessionTabs');
}

function getChatSessionCountEl() {
    return document.getElementById('chatSessionCount');
}

function setChatSessionCount(count) {
    const el = getChatSessionCountEl();
    if (!el) {
        return;
    }

    if (Number.isFinite(count)) {
        const value = Math.max(0, Number(count));
        el.textContent = String(value);
        el.title = `Conversations (${value})`;
        el.setAttribute('aria-label', `Conversation count: ${value}`);
    } else {
        el.textContent = '—';
        el.title = 'Conversations';
        el.setAttribute('aria-label', 'Conversation count');
    }
}

function getChatSessionMetadataEl() {
    return document.getElementById('chatSessionMetadata');
}

function _normaliseChatSessionLinks(links) {
    const raw = (links && typeof links === 'object') ? links : {};
    const out = {};
    for (const { key } of CHAT_SESSION_LINK_KEYS) {
        const values = raw[key];
        const arr = Array.isArray(values) ? values : (typeof values === 'string' ? [values] : []);
        const cleaned = [];
        const seen = new Set();
        for (const item of arr) {
            if (typeof item !== 'string') continue;
            const trimmed = item.trim();
            if (!trimmed) continue;
            if (!trimmed.startsWith('#V#')) continue;
            if (seen.has(trimmed)) continue;
            seen.add(trimmed);
            cleaned.push(trimmed);
        }
        out[key] = cleaned;
    }
    return out;
}

function _normalisePotentialConceptId(text) {
    const raw = String(text ?? '').trim();
    if (!raw) return '';

    let id = raw;
    if (id.startsWith('#v#')) id = `#V#${id.slice(3)}`;
    if (!id.startsWith('#V#')) return '';

    // Strip common trailing punctuation.
    const trailingJunk = new Set(['.', ',', ':', ';', '!', '?', ')', ']', '}', '…']);
    while (id.length > 3 && trailingJunk.has(id[id.length - 1])) {
        id = id.slice(0, -1);
    }

    return id.trim();
}

function _deriveNameFromConceptId(conceptId) {
    const slug = String(conceptId || '').replace(/^#V#/, '');
    if (!slug) return '';
    return slug.replace(/_/g, ' ');
}

async function _getConceptMetaForChatSession(conceptId) {
    const id = String(conceptId || '').trim();
    if (!id || !id.startsWith('#V#')) return null;

    if (chatSessionConceptMetaCache.has(id)) {
        return chatSessionConceptMetaCache.get(id);
    }

    if (chatSessionConceptMetaInFlight.has(id)) {
        return chatSessionConceptMetaInFlight.get(id);
    }

    const request = (async () => {
        try {
            const url = `/vontology/api/vontology/node_content?identifier=${encodeURIComponent(id)}&raw_only=1`;
            const resp = await fetch(url, { method: 'GET', headers: { 'Accept': 'application/json' } });
            if (!resp.ok) {
                return null;
            }
            const json = await resp.json();
            const preferredLanguage = getPreferredLanguage();
            const bestName = selectBestNameForContext(json?.raw_doc?.names, preferredLanguage);
            const meta = {
                displayName: bestName || json?.display_name || null,
                kind: json?.kind || json?.computed_kind || null
            };
            chatSessionConceptMetaCache.set(id, meta);
            return meta;
        } catch (_) {
            return null;
        } finally {
            chatSessionConceptMetaInFlight.delete(id);
        }
    })();

    chatSessionConceptMetaInFlight.set(id, request);
    return request;
}

function _clearChatSessionMetadata() {
    const el = getChatSessionMetadataEl();
    if (!el) return;
    el.innerHTML = '';
    el.classList.add('is-hidden');
}

function _renderChatSessionMetadataMessage(message) {
    const el = getChatSessionMetadataEl();
    if (!el) return;
    el.classList.remove('is-hidden');
    el.innerHTML = '';

    const isCollapsed = _isChatSessionMetadataCollapsed();

    const header = document.createElement('div');
    header.className = 'chat-session-metadata-header';

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'chat-session-metadata-toggle';
    toggle.setAttribute('aria-expanded', String(!isCollapsed));
    toggle.title = isCollapsed ? 'Show conversation context' : 'Hide conversation context';

    const toggleText = document.createElement('span');
    toggleText.className = 'chat-session-metadata-toggle-text';
    toggleText.textContent = 'Conversation context';

    const toggleIcon = document.createElement('span');
    toggleIcon.className = 'chat-session-metadata-toggle-icon';
    toggleIcon.textContent = '▾';

    toggle.appendChild(toggleText);
    toggle.appendChild(toggleIcon);
    toggle.addEventListener('click', () => {
        _setChatSessionMetadataCollapsed(!isCollapsed);
        _renderChatSessionMetadataMessage(message);
    });

    header.appendChild(toggle);
    el.appendChild(header);

    const body = document.createElement('div');
    body.className = 'chat-session-metadata-body';
    if (isCollapsed) {
        body.classList.add('is-collapsed');
    }

    const row = document.createElement('div');
    row.className = 'chat-session-metadata-row';
    const label = document.createElement('div');
    label.className = 'chat-session-metadata-label';
    label.textContent = 'Metadata';
    const values = document.createElement('div');
    values.className = 'chat-session-metadata-values';

    const msg = document.createElement('span');
    msg.style.fontSize = '13px';
    msg.style.color = '#475569';
    msg.textContent = String(message || '').trim() || '—';
    values.appendChild(msg);
    row.appendChild(label);
    row.appendChild(values);
    body.appendChild(row);
    el.appendChild(body);
}

function _buildChatSessionMetadataSuggestions({ inputEl, suggestionsEl, onPick, includeIndividuals = true }) {
    const state = { items: [], activeIndex: -1, abortController: null };
    let pointerDown = false;

    suggestionsEl.addEventListener('pointerdown', () => {
        pointerDown = true;
    });
    suggestionsEl.addEventListener('pointerup', () => {
        setTimeout(() => {
            pointerDown = false;
        }, 0);
    });

    const clearSuggestions = () => {
        state.items = [];
        state.activeIndex = -1;
        suggestionsEl.classList.remove('open');
        suggestionsEl.innerHTML = '';
    };

    const setActive = (idx) => {
        state.activeIndex = idx;
        suggestionsEl.querySelectorAll('.chat-session-metadata-suggestion').forEach((el, i) => {
            if (i === idx) {
                el.classList.add('active');
            } else {
                el.classList.remove('active');
            }
        });
    };

    const render = (items) => {
        suggestionsEl.innerHTML = '';
        if (!items.length) {
            suggestionsEl.classList.remove('open');
            return;
        }

        items.forEach((it, idx) => {
            const row = document.createElement('div');
            row.className = 'chat-session-metadata-suggestion';
            row.setAttribute('role', 'option');

            const name = document.createElement('span');
            name.textContent = it.name || it.id;
            name.title = `${it.name || it.id} — ${it.id}`;

            const kind = document.createElement('span');
            kind.className = 'chat-session-metadata-suggestion-kind';
            kind.textContent = it.kind === 'predicate' ? 'Predicate' : (it.kind === 'individual' ? 'Individual' : 'Type');

            row.appendChild(name);
            row.appendChild(kind);

            row.addEventListener('mouseenter', () => setActive(idx));
            row.addEventListener('mouseleave', () => setActive(-1));
            row.addEventListener('click', () => {
                onPick(it);
                clearSuggestions();
            });

            suggestionsEl.appendChild(row);
        });

        suggestionsEl.classList.add('open');
    };

    const performSearch = async (query) => {
        const q = String(query || '').trim();
        if (!q) {
            clearSuggestions();
            return;
        }

        try {
            state.abortController?.abort?.();
        } catch (_) { /* ignore */ }
        const ac = new AbortController();
        state.abortController = ac;

        const url = `/vontology/api/vontology/search?q=${encodeURIComponent(q)}&limit=12&fallback_substring=true${includeIndividuals ? '&include_individuals=true' : ''}`;
        try {
            const resp = await fetch(url, { signal: ac.signal });
            if (!resp.ok) {
                clearSuggestions();
                return;
            }
            const data = await resp.json();
            const items = Array.isArray(data?.results) ? data.results : [];
            state.items = items;
            state.activeIndex = -1;
            render(items);
        } catch (err) {
            if (err?.name === 'AbortError') return;
            clearSuggestions();
        }
    };

    const debounce = (fn, wait) => {
        let t;
        return (...args) => {
            clearTimeout(t);
            t = setTimeout(() => fn(...args), wait);
        };
    };

    const debouncedSearch = debounce(() => performSearch(inputEl.value), 200);

    inputEl.addEventListener('input', () => debouncedSearch());
    inputEl.addEventListener('focus', () => {
        if (state.items.length) {
            suggestionsEl.classList.add('open');
        }
    });

    inputEl.addEventListener('blur', () => {
        // Allow click selection to run first.
        setTimeout(() => {
            if (!pointerDown) {
                clearSuggestions();
            }
        }, 0);
    });

    inputEl.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            clearSuggestions();
            inputEl.blur();
            return;
        }

        if (!state.items.length) {
            if (e.key === 'Enter') {
                const maybeId = _normalisePotentialConceptId(inputEl.value);
                if (maybeId) {
                    e.preventDefault();
                    onPick({ id: maybeId, name: null, kind: 'individual' });
                    inputEl.value = '';
                    clearSuggestions();
                }
            }
            return;
        }

        if (e.key === 'ArrowDown') {
            e.preventDefault();
            setActive(Math.min(state.activeIndex + 1, state.items.length - 1));
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setActive(Math.max(state.activeIndex - 1, 0));
        } else if (e.key === 'Enter') {
            e.preventDefault();
            const idx = (state.activeIndex >= 0) ? state.activeIndex : 0;
            const chosen = state.items[idx];
            if (chosen) {
                onPick(chosen);
                inputEl.value = '';
                clearSuggestions();
            }
        }
    });

    return { clearSuggestions };
}

function _escapeCssValue(value) {
    const raw = String(value ?? '');
    if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') {
        return CSS.escape(raw);
    }
    return raw.replace(/[^a-zA-Z0-9_-]/g, '_');
}

function _isChatSessionMetadataCollapsed() {
    if (typeof chatSessionMetadataCollapsed === 'boolean') {
        return chatSessionMetadataCollapsed;
    }
    try {
        chatSessionMetadataCollapsed = window.localStorage.getItem(LS_CHAT_SESSION_METADATA_COLLAPSED) === 'true';
    } catch (_) {
        chatSessionMetadataCollapsed = false;
    }
    return chatSessionMetadataCollapsed;
}

function _setChatSessionMetadataCollapsed(value) {
    chatSessionMetadataCollapsed = !!value;
    try {
        window.localStorage.setItem(LS_CHAT_SESSION_METADATA_COLLAPSED, chatSessionMetadataCollapsed ? 'true' : 'false');
    } catch (_) {
        // ignore
    }
}

function _renderChatSessionMetadataPanel({ sessionId, links, statusText, statusTone, disabled }) {
    const el = getChatSessionMetadataEl();
    if (!el) return;

    const sid = String(sessionId || '').trim();
    if (!sid) {
        _clearChatSessionMetadata();
        return;
    }

    el.classList.remove('is-hidden');
    el.innerHTML = '';

    const isCollapsed = _isChatSessionMetadataCollapsed();
    if (isCollapsed && chatSessionMetadataOpenKey) {
        chatSessionMetadataOpenKey = null;
    }

    const header = document.createElement('div');
    header.className = 'chat-session-metadata-header';

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'chat-session-metadata-toggle';
    toggle.setAttribute('aria-expanded', String(!isCollapsed));
    toggle.title = isCollapsed ? 'Show conversation context' : 'Hide conversation context';
    toggle.disabled = false;

    const toggleText = document.createElement('span');
    toggleText.className = 'chat-session-metadata-toggle-text';
    toggleText.textContent = 'Conversation context';

    const toggleIcon = document.createElement('span');
    toggleIcon.className = 'chat-session-metadata-toggle-icon';
    toggleIcon.textContent = '▾';

    toggle.appendChild(toggleText);
    toggle.appendChild(toggleIcon);
    toggle.addEventListener('click', () => {
        _setChatSessionMetadataCollapsed(!isCollapsed);
        _renderChatSessionMetadataPanel({
            sessionId: sid,
            links: activeChatSessionLinks?.links,
            statusText: activeChatSessionLinks?.statusText,
            statusTone: activeChatSessionLinks?.statusTone,
            disabled: activeChatSessionLinks?.disabled
        });
    });

    header.appendChild(toggle);
    el.appendChild(header);

    if (statusText) {
        const statusRow = document.createElement('div');
        statusRow.className = 'chat-session-metadata-row';
        const label = document.createElement('div');
        label.className = 'chat-session-metadata-label';
        label.textContent = 'Status';
        const values = document.createElement('div');
        values.className = 'chat-session-metadata-values';
        const status = document.createElement('span');
        status.style.fontSize = '12px';
        status.style.color = statusTone === 'error' ? '#b91c1c' : '#64748b';
        status.textContent = statusText;
        values.appendChild(status);
        statusRow.appendChild(label);
        statusRow.appendChild(values);
        el.appendChild(statusRow);
    }

    const body = document.createElement('div');
    body.className = 'chat-session-metadata-body';
    if (isCollapsed) {
        body.classList.add('is-collapsed');
    }

    const safeLinks = _normaliseChatSessionLinks(links);

    for (const group of CHAT_SESSION_LINK_KEYS) {
        const row = document.createElement('div');
        row.className = 'chat-session-metadata-row';

        const label = document.createElement(group.typeConceptId ? 'button' : 'div');
        label.className = group.typeConceptId
            ? 'chat-session-metadata-label chat-session-metadata-label-link'
            : 'chat-session-metadata-label';
        label.textContent = group.label;
        if (group.typeConceptId) {
            label.type = 'button';
            label.title = `Open ${group.label} type concept`;
            label.disabled = !!disabled;
            label.addEventListener('click', (e) => {
                e.preventDefault();
                document.dispatchEvent(new CustomEvent('von:selectConceptById', {
                    detail: { conceptId: group.typeConceptId, createConceptTab: true }
                }));
            });
        }

        const values = document.createElement('div');
        values.className = 'chat-session-metadata-values';

        const ids = Array.isArray(safeLinks[group.key]) ? safeLinks[group.key] : [];
        ids.forEach((conceptId) => {
            const chip = document.createElement('span');
            chip.className = 'chat-session-metadata-chip';

            const link = document.createElement('a');
            link.href = '#';
            const cached = chatSessionConceptMetaCache.get(conceptId);
            link.textContent = cached?.displayName || _deriveNameFromConceptId(conceptId) || conceptId;
            link.title = `${cached?.displayName || conceptId} — ${conceptId}`;
            link.addEventListener('click', (e) => {
                e.preventDefault();
                document.dispatchEvent(new CustomEvent('von:selectConceptById', {
                    detail: { conceptId, createConceptTab: true }
                }));
            });
            chip.appendChild(link);

            const removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.className = 'chat-session-metadata-chip-remove';
            removeBtn.textContent = '×';
            removeBtn.title = `Remove ${conceptId}`;
            removeBtn.disabled = !!disabled;
            removeBtn.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                removeChatSessionLink(group.key, conceptId);
            });
            chip.appendChild(removeBtn);

            values.appendChild(chip);

            if (!cached && !chatSessionConceptMetaInFlight.has(conceptId)) {
                void _getConceptMetaForChatSession(conceptId).then(() => {
                    if (activeChatSessionLinks?.sessionId === sid) {
                        _renderChatSessionMetadataPanel({
                            sessionId: sid,
                            links: activeChatSessionLinks?.links,
                            statusText: activeChatSessionLinks?.statusText,
                            statusTone: activeChatSessionLinks?.statusTone,
                            disabled: activeChatSessionLinks?.disabled
                        });
                    }
                });
            }
        });

        const actions = document.createElement('div');
        actions.className = 'chat-session-metadata-actions';

        const addBtn = document.createElement('button');
        addBtn.type = 'button';
        addBtn.className = 'chat-session-metadata-add';
        addBtn.textContent = '+';
        addBtn.title = `Add ${group.label}`;
        addBtn.disabled = !!disabled;

        const inputWrap = document.createElement('div');
        inputWrap.className = 'chat-session-metadata-inputwrap';
        inputWrap.dataset.linkKey = group.key;
        inputWrap.style.display = (chatSessionMetadataOpenKey === group.key) ? 'block' : 'none';

        const input = document.createElement('input');
        input.className = 'chat-session-metadata-input';
        input.type = 'text';
        input.placeholder = group.placeholder;
        input.disabled = !!disabled;

        const suggestions = document.createElement('div');
        suggestions.className = 'chat-session-metadata-suggestions';
        suggestions.setAttribute('role', 'listbox');

        inputWrap.appendChild(input);
        inputWrap.appendChild(suggestions);

        addBtn.addEventListener('click', () => {
            if (chatSessionMetadataOpenKey === group.key) {
                chatSessionMetadataOpenKey = null;
                _renderChatSessionMetadataPanel({
                    sessionId: sid,
                    links: activeChatSessionLinks?.links,
                    statusText: activeChatSessionLinks?.statusText,
                    statusTone: activeChatSessionLinks?.statusTone,
                    disabled: activeChatSessionLinks?.disabled
                });
                return;
            }
            chatSessionMetadataOpenKey = group.key;
            _renderChatSessionMetadataPanel({
                sessionId: sid,
                links: activeChatSessionLinks?.links,
                statusText: activeChatSessionLinks?.statusText,
                statusTone: activeChatSessionLinks?.statusTone,
                disabled: activeChatSessionLinks?.disabled
            });

            setTimeout(() => {
                const container = getChatSessionMetadataEl();
                if (!container) return;
                const escapedKey = _escapeCssValue(group.key);
                const target = container.querySelector(`.chat-session-metadata-inputwrap[data-link-key="${escapedKey}"] .chat-session-metadata-input`);
                try { target?.focus?.(); } catch (_) { /* ignore */ }
            }, 0);
        });

        actions.appendChild(addBtn);
        actions.appendChild(inputWrap);
        values.appendChild(actions);

        row.appendChild(label);
        row.appendChild(values);
        body.appendChild(row);

        if (chatSessionMetadataOpenKey === group.key && !disabled && !isCollapsed) {
            // Wire autocomplete + focus.
            _buildChatSessionMetadataSuggestions({
                inputEl: input,
                suggestionsEl: suggestions,
                includeIndividuals: true,
                onPick: (it) => {
                    const picked = _normalisePotentialConceptId(it?.id) || (typeof it?.id === 'string' ? it.id.trim() : '');
                    if (!picked) {
                        showToast('Select a valid concept.', 'error');
                        return;
                    }
                    addChatSessionLink(group.key, picked);
                }
            });
            setTimeout(() => {
                try { input.focus(); } catch (_) { /* ignore */ }
            }, 0);
        }
    }

    el.appendChild(body);
}

function _setActiveChatSessionLinksState(state) {
    activeChatSessionLinks = state;
    _renderChatSessionMetadataPanel({
        sessionId: state?.sessionId,
        links: state?.links,
        statusText: state?.statusText,
        statusTone: state?.statusTone,
        disabled: state?.disabled
    });
}

function _scheduleSaveChatSessionLinks(sessionId, links) {
    const sid = String(sessionId || '').trim();
    if (!sid) return;
    chatSessionLinksDirtySessionId = sid;
    chatSessionLinksDirtyPayload = links;

    if (chatSessionLinksSaveDebounceId) {
        clearTimeout(chatSessionLinksSaveDebounceId);
        chatSessionLinksSaveDebounceId = null;
    }

    chatSessionLinksSaveDebounceId = setTimeout(() => {
        chatSessionLinksSaveDebounceId = null;
        void saveChatSessionLinks(sid, chatSessionLinksDirtyPayload);
    }, 650);
}

async function _flushPendingChatSessionLinksSave(sessionId) {
    const sid = String(sessionId || '').trim();
    if (!sid) return;
    if (!chatSessionLinksDirtySessionId || chatSessionLinksDirtySessionId !== sid) return;
    if (chatSessionLinksSaveDebounceId) {
        clearTimeout(chatSessionLinksSaveDebounceId);
        chatSessionLinksSaveDebounceId = null;
    }
    await saveChatSessionLinks(sid, chatSessionLinksDirtyPayload);
}

function addChatSessionLink(key, conceptId) {
    const sid = activeChatSessionLinks?.sessionId;
    if (!sid || sid !== activeChatSessionId) return;
    const safeKey = String(key || '').trim();
    const id = _normalisePotentialConceptId(conceptId);
    if (!safeKey || !id) return;

    const current = _normaliseChatSessionLinks(activeChatSessionLinks?.links);
    const list = Array.isArray(current[safeKey]) ? current[safeKey].slice() : [];
    if (list.includes(id)) {
        showToast('Already added.', 'info');
        return;
    }
    list.push(id);
    current[safeKey] = list;

    chatSessionLinksCache.set(sid, current);
    _setActiveChatSessionLinksState({
        sessionId: sid,
        links: current,
        statusText: 'Saving…',
        statusTone: 'info',
        disabled: false
    });
    _scheduleSaveChatSessionLinks(sid, current);
}

function removeChatSessionLink(key, conceptId) {
    const sid = activeChatSessionLinks?.sessionId;
    if (!sid || sid !== activeChatSessionId) return;
    const safeKey = String(key || '').trim();
    const id = String(conceptId || '').trim();
    if (!safeKey || !id) return;

    const current = _normaliseChatSessionLinks(activeChatSessionLinks?.links);
    const list = Array.isArray(current[safeKey]) ? current[safeKey].slice() : [];
    const next = list.filter(v => v !== id);
    current[safeKey] = next;

    chatSessionLinksCache.set(sid, current);
    _setActiveChatSessionLinksState({
        sessionId: sid,
        links: current,
        statusText: 'Saving…',
        statusTone: 'info',
        disabled: false
    });
    _scheduleSaveChatSessionLinks(sid, current);
}

async function loadChatSessionLinks(sessionId, options = {}) {
    const sid = String(sessionId || '').trim();
    if (!sid) {
        _clearChatSessionMetadata();
        return;
    }

    const force = options.force === true;
    const cached = chatSessionLinksCache.get(sid);
    if (!force && cached) {
        _setActiveChatSessionLinksState({ sessionId: sid, links: cached, statusText: '', statusTone: 'info', disabled: false });
        return;
    }

    try {
        chatSessionLinksLoadInFlight?.abortController?.abort?.();
    } catch (_) { /* ignore */ }
    const abortController = new AbortController();
    chatSessionLinksLoadInFlight = { sessionId: sid, abortController };

    _setActiveChatSessionLinksState({ sessionId: sid, links: cached || {}, statusText: 'Loading…', statusTone: 'info', disabled: true });

    try {
        const resp = await fetch(`/von/api/session/chat_session_links?session_id=${encodeURIComponent(sid)}`, {
            method: 'GET',
            headers: { 'Accept': 'application/json' },
            cache: 'no-store',
            signal: abortController.signal
        });
        const data = await resp.json().catch(() => ({}));

        if (abortController.signal.aborted) return;
        if (activeChatSessionId !== sid) return;

        if (resp.status === 401 || data?.error === 'Not authenticated') {
            chatSessionMetadataOpenKey = null;
            _renderChatSessionMetadataMessage('Log in to view/edit conversation metadata.');
            return;
        }

        if (!resp.ok) {
            _setActiveChatSessionLinksState({ sessionId: sid, links: cached || {}, statusText: 'Unable to load metadata.', statusTone: 'error', disabled: false });
            return;
        }

        const links = _normaliseChatSessionLinks(data?.session_links);
        chatSessionLinksCache.set(sid, links);
        _setActiveChatSessionLinksState({ sessionId: sid, links, statusText: '', statusTone: 'info', disabled: false });
    } catch (err) {
        if (err?.name === 'AbortError') return;
        _setActiveChatSessionLinksState({ sessionId: sid, links: cached || {}, statusText: 'Unable to load metadata.', statusTone: 'error', disabled: false });
    }
}

async function saveChatSessionLinks(sessionId, links) {
    const sid = String(sessionId || '').trim();
    if (!sid) return;
    const payload = _normaliseChatSessionLinks(links);

    // Avoid parallel saves; last write wins.
    try {
        chatSessionLinksSaveInFlight?.abortController?.abort?.();
    } catch (_) { /* ignore */ }
    const abortController = new AbortController();
    chatSessionLinksSaveInFlight = { sessionId: sid, abortController };

    try {
        const resp = await fetch('/von/api/session/chat_session_links', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: JSON.stringify({ session_id: sid, session_links: payload }),
            signal: abortController.signal
        });
        const data = await resp.json().catch(() => ({}));
        if (abortController.signal.aborted) return;
        if (activeChatSessionId !== sid) return;

        if (resp.status === 401 || data?.error === 'Not authenticated') {
            _renderChatSessionMetadataMessage('Log in to view/edit conversation metadata.');
            return;
        }

        if (!resp.ok) {
            _setActiveChatSessionLinksState({ sessionId: sid, links: payload, statusText: 'Save failed.', statusTone: 'error', disabled: false });
            return;
        }

        const saved = _normaliseChatSessionLinks(data?.session_links || payload);
        chatSessionLinksCache.set(sid, saved);
        chatSessionLinksDirtySessionId = null;
        chatSessionLinksDirtyPayload = null;
        _setActiveChatSessionLinksState({ sessionId: sid, links: saved, statusText: 'Saved', statusTone: 'info', disabled: false });
        setTimeout(() => {
            if (activeChatSessionLinks?.sessionId === sid && activeChatSessionLinks?.statusText === 'Saved') {
                _setActiveChatSessionLinksState({ sessionId: sid, links: saved, statusText: '', statusTone: 'info', disabled: false });
            }
        }, 1200);
    } catch (err) {
        if (err?.name === 'AbortError') return;
        _setActiveChatSessionLinksState({ sessionId: sid, links: payload, statusText: 'Save failed.', statusTone: 'error', disabled: false });
    }
}

function getChatTabButton() {
    return document.querySelector('.tab-button[data-tab="chatTab"]');
}

function getChatSessionTabById(sessionId) {
    const container = getChatSessionTabsContainer();
    const sid = String(sessionId || '').trim();
    if (!container || !sid) {
        return null;
    }
    const escaped = (typeof CSS !== 'undefined' && typeof CSS.escape === 'function')
        ? CSS.escape(sid)
        : sid.replace(/"/g, '\\"');
    return container.querySelector(`.chat-session-tab[data-session-id="${escaped}"]`);
}

function setChatSessionTabLoading(sessionId, isLoading) {
    const sid = String(sessionId || '').trim();
    const container = getChatSessionTabsContainer();
    if (!container) {
        if (!isLoading && loadingChatSessionId === sid) {
            loadingChatSessionId = null;
        } else if (isLoading && sid) {
            loadingChatSessionId = sid;
        }
        return;
    }

    if (loadingChatSessionId && loadingChatSessionId !== sid) {
        const previousTab = getChatSessionTabById(loadingChatSessionId);
        if (previousTab) {
            previousTab.classList.remove('is-loading');
        }
    }

    if (!isLoading) {
        if (loadingChatSessionId === sid) {
            const targetTab = getChatSessionTabById(sid);
            if (targetTab) {
                targetTab.classList.remove('is-loading');
            }
            loadingChatSessionId = null;
        }
        return;
    }

    if (sid) {
        loadingChatSessionId = sid;
        const targetTab = getChatSessionTabById(sid);
        if (targetTab) {
            targetTab.classList.add('is-loading');
        }
    }
}

function getShortSessionId(sessionId) {
    const sid = String(sessionId || '').trim();
    if (!sid) {
        return 'session';
    }
    return sid.length > 10 ? `${sid.slice(0, 8)}.` : sid;
}

function getSessionDisplayName(session) {
    const rawName = (typeof session?.session_name === 'string' && session.session_name.trim())
        ? session.session_name.trim()
        : '';
    if (rawName) {
        return rawName;
    }
    return getShortSessionId(session?.session_id);
}

function formatCompletedLabel(isoString) {
    if (!isoString) {
        return 'completed';
    }
    const parsed = new Date(isoString);
    if (Number.isNaN(parsed.getTime())) {
        return 'completed';
    }
    const formatted = parsed.toLocaleString('en-NZ', {
        year: 'numeric',
        month: 'short',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit'
    });
    return `completed ${formatted}`;
}

function formatSessionTimestamp(isoString) {
    if (!isoString) {
        return '';
    }
    const parsed = new Date(isoString);
    if (Number.isNaN(parsed.getTime())) {
        return '';
    }

    const now = new Date();
    const isToday = now.getFullYear() === parsed.getFullYear()
        && now.getMonth() === parsed.getMonth()
        && now.getDate() === parsed.getDate();

    if (isToday) {
        return parsed.toLocaleTimeString('en-NZ', {
            hour: '2-digit',
            minute: '2-digit'
        });
    }

    return parsed.toLocaleDateString('en-NZ', {
        day: '2-digit',
        month: 'short'
    });
}

function scheduleChatSessionTabsRefresh(force = false) {
    if (force) {
        void refreshChatSessionTabs();
        return;
    }

    const now = Date.now();
    const elapsed = now - lastSessionTabsRefreshMs;
    if (elapsed >= SESSION_TABS_REFRESH_COOLDOWN_MS) {
        void refreshChatSessionTabs();
        return;
    }

    if (pendingSessionTabsRefresh) {
        return;
    }

    pendingSessionTabsRefresh = setTimeout(() => {
        pendingSessionTabsRefresh = null;
        void refreshChatSessionTabs();
    }, Math.max(500, SESSION_TABS_REFRESH_COOLDOWN_MS - elapsed));
}

function _chatTabsNowPerfMs() {
    try {
        if (typeof performance !== 'undefined' && typeof performance.now === 'function') {
            return performance.now();
        }
    } catch (_) {
        // ignore
    }
    return Date.now();
}

function _getChatTabsLoadStats() {
    try {
        const raw = localStorage.getItem(LS_CHAT_TABS_LOAD_STATS);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== 'object') return null;
        const emaMs = Number(parsed.ema_ms);
        const samples = Number(parsed.samples);
        const lastMs = Number(parsed.last_ms);
        return {
            ema_ms: Number.isFinite(emaMs) ? emaMs : null,
            samples: Number.isFinite(samples) ? samples : 0,
            last_ms: Number.isFinite(lastMs) ? lastMs : null,
            last_at: typeof parsed.last_at === 'string' ? parsed.last_at : null
        };
    } catch (_) {
        return null;
    }
}

function _storeChatTabsLoadStats(durationMs) {
    const ms = Number(durationMs);
    if (!Number.isFinite(ms) || ms <= 0) return;

    const existing = _getChatTabsLoadStats() || { ema_ms: null, samples: 0, last_ms: null, last_at: null };
    const prev = Number(existing.ema_ms);
    const hasPrev = Number.isFinite(prev) && prev > 0;
    const alpha = 0.30;
    const ema = hasPrev ? (prev * (1 - alpha) + ms * alpha) : ms;
    const samples = (Number.isFinite(existing.samples) ? existing.samples : 0) + 1;

    try {
        localStorage.setItem(LS_CHAT_TABS_LOAD_STATS, JSON.stringify({
            ema_ms: Math.round(ema),
            samples,
            last_ms: Math.round(ms),
            last_at: new Date().toISOString()
        }));
    } catch (_) {
        // ignore localStorage failures
    }
}

function _stopChatTabsLoadingTicker() {
    if (chatTabsLoadingTickerId) {
        clearInterval(chatTabsLoadingTickerId);
        chatTabsLoadingTickerId = null;
    }
    chatTabsLoadingTickerLastRenderedSec = -1;
}

function _startChatTabsLoadingTicker() {
    _stopChatTabsLoadingTicker();

    chatTabsLoadingTickerId = setInterval(() => {
        try {
            const container = getChatSessionTabsContainer();
            if (!container || container.hidden) {
                _stopChatTabsLoadingTicker();
                return;
            }

            const placeholder = container.querySelector('.chat-session-tabs-placeholder.is-loading');
            if (!placeholder) {
                _stopChatTabsLoadingTicker();
                return;
            }

            const telemetryEl = placeholder.querySelector('.chat-session-tabs-placeholder-telemetry');
            if (!telemetryEl) {
                return;
            }

            if (!Number.isFinite(chatTabsFirstLoadStartPerfMs)) {
                telemetryEl.textContent = '';
                return;
            }

            const elapsedMs = Math.max(0, _chatTabsNowPerfMs() - chatTabsFirstLoadStartPerfMs);
            const elapsedSec = Math.floor(elapsedMs / 1000);
            if (elapsedSec === chatTabsLoadingTickerLastRenderedSec) {
                return;
            }
            chatTabsLoadingTickerLastRenderedSec = elapsedSec;

            const stats = _getChatTabsLoadStats();
            const expectedMs = Number(stats?.ema_ms);
            const hasExpected = Number.isFinite(expectedMs) && expectedMs > 250;
            const etaSec = hasExpected ? Math.max(0, Math.round((expectedMs - elapsedMs) / 1000)) : null;

            const parts = [];
            parts.push(`elapsed ${elapsedSec}s`);
            if (etaSec !== null) {
                parts.push(`ETA ~${etaSec}s`);
            }

            telemetryEl.textContent = parts.length ? `• ${parts.join(' • ')}` : '';
        } catch (_) {
            // Best-effort ticker.
        }
    }, 500);
}

function openSettingsToLogin() {
    try {
        const settingsTabButton = document.querySelector('.tab-button[data-tab="settingsTab"]');
        if (settingsTabButton) {
            settingsTabButton.click();
        }

        const sendFocusMessage = (attempt = 0) => {
            const frame = document.getElementById('settingsFrame');
            const targetWindow = frame?.contentWindow;
            if (targetWindow) {
                try {
                    targetWindow.postMessage({ type: 'von:focus-current-user-settings' }, window.location.origin);
                } catch (_) {
                    // Best-effort
                }

                if (attempt < 8) {
                    setTimeout(() => sendFocusMessage(attempt + 1), 250);
                }
                return;
            }

            if (attempt < 8) {
                setTimeout(() => sendFocusMessage(attempt + 1), 250);
            }
        };

        sendFocusMessage(0);
    } catch (err) {
        console.warn('[chatTab] Failed to open Settings for login:', err);
        try { showToast('Unable to open Settings for login.', true); } catch (_) { }
    }
}

function renderChatSessionTabsPlaceholder(mode = 'loading') {
    const container = getChatSessionTabsContainer();
    if (!container) {
        return;
    }

    container.hidden = false;
    container.innerHTML = '';

    setChatSessionCount(null);

    const fragment = document.createDocumentFragment();

    const allowNewChat = mode !== 'unauthenticated';
    if (allowNewChat) {
        const newTab = document.createElement('button');
        newTab.type = 'button';
        newTab.className = 'chat-session-tab chat-session-tab-new';
        newTab.title = 'New chat';
        newTab.setAttribute('aria-label', 'New chat');
        newTab.textContent = '+';
        newTab.addEventListener('click', () => {
            void promptAndCreateChatSession();
        });
        fragment.appendChild(newTab);
    }

    const placeholder = document.createElement('div');
    placeholder.className = 'chat-session-tabs-placeholder';

    const primary = document.createElement('span');
    primary.className = 'chat-session-tabs-placeholder-primary';

    const telemetry = document.createElement('span');
    telemetry.className = 'chat-session-tabs-placeholder-telemetry';

    if (mode === 'unauthenticated') {
        primary.textContent = 'Log in to load your saved conversations.';
    } else if (mode === 'error') {
        primary.textContent = 'Unable to load conversations right now (will retry).';
    } else if (mode === 'empty') {
        primary.textContent = 'No saved conversations yet.';
    } else {
        primary.textContent = 'Loading conversations…';
        placeholder.classList.add('is-loading');
    }

    placeholder.appendChild(primary);
    placeholder.appendChild(telemetry);

    fragment.appendChild(placeholder);

    if (mode === 'unauthenticated') {
        const loginTab = document.createElement('button');
        loginTab.type = 'button';
        loginTab.className = 'chat-session-tab';
        loginTab.title = 'Open Settings to log in';
        loginTab.setAttribute('aria-label', 'Open Settings to log in');
        loginTab.textContent = 'Log in';
        loginTab.addEventListener('click', () => {
            openSettingsToLogin();
        });
        fragment.appendChild(loginTab);
    }

    if (mode === 'error') {
        const retryTab = document.createElement('button');
        retryTab.type = 'button';
        retryTab.className = 'chat-session-tab';
        retryTab.title = 'Retry loading conversations';
        retryTab.setAttribute('aria-label', 'Retry loading conversations');
        retryTab.textContent = 'Retry';
        retryTab.addEventListener('click', () => {
            scheduleChatSessionTabsRefresh(true);
        });
        fragment.appendChild(retryTab);
    }

    container.appendChild(fragment);
}

async function refreshChatSessionTabs() {
    const container = getChatSessionTabsContainer();
    if (!container) {
        return;
    }

    const hasCachedTabs = Array.isArray(sessionTabsCache) && sessionTabsCache.length > 0;
    const containerLooksEmpty = container.hidden || !container.firstElementChild;

    // If we're about to load sessions and there's nothing visible yet, keep a placeholder
    // in the tabs bar so the controls don't disappear.
    if (!hasCachedTabs && containerLooksEmpty) {
        if (!Number.isFinite(chatTabsFirstLoadStartPerfMs)) {
            chatTabsFirstLoadStartPerfMs = _chatTabsNowPerfMs();
            chatTabsFirstLoadStartEpochMs = Date.now();
        }
        renderChatSessionTabsPlaceholder('loading');
        _startChatTabsLoadingTicker();
    }

    // Avoid UI flicker: if we have a last-known-good session list, keep it visible
    // while refresh is in flight (and especially if the container was previously hidden).
    if (container.hidden && hasCachedTabs) {
        renderChatSessionTabs(sessionTabsCache, activeChatSessionId);
    }

    if (pendingSessionTabsRefresh) {
        clearTimeout(pendingSessionTabsRefresh);
        pendingSessionTabsRefresh = null;
    }

    lastSessionTabsRefreshMs = Date.now();

    try {
        const response = await fetch('/von/history/sessions?limit=50&summary=light', { cache: 'no-store' });
        const data = await response.json();

        if (data?.authenticated === false) {
            _stopChatTabsLoadingTicker();
            renderChatSessionTabsPlaceholder('unauthenticated');
            lastRenderedSessionCount = 0;
            sessionTabsCache = [];
            chatSessionMetadataOpenKey = null;
            _clearChatSessionMetadata();
            return;
        }

        if (!response.ok) {
            console.warn('[chatTab] Failed to refresh chat sessions (keeping existing tabs)', {
                status: response.status,
                data
            });
            if (hasCachedTabs) {
                container.hidden = false;
            } else {
                _stopChatTabsLoadingTicker();
                renderChatSessionTabsPlaceholder('error');
                setTimeout(() => scheduleChatSessionTabsRefresh(true), 2000);
            }
            return;
        }

        const sessions = Array.isArray(data?.sessions) ? data.sessions : [];

        // If the server temporarily reports no sessions (e.g. after creating a new chat
        // while history is still updating), keep the existing UI rather than hiding it.
        if (sessions.length === 0) {
            if (hasCachedTabs) {
                console.warn('[chatTab] Session list empty during refresh; keeping existing tabs');
                setTimeout(() => scheduleChatSessionTabsRefresh(true), 2000);
                return;
            }

            // Keep a placeholder visible; this is common right after reload if history
            // is still warming up.
            if (!Number.isFinite(chatTabsFirstLoadStartPerfMs)) {
                chatTabsFirstLoadStartPerfMs = _chatTabsNowPerfMs();
                chatTabsFirstLoadStartEpochMs = Date.now();
            }
            renderChatSessionTabsPlaceholder('loading');
            _startChatTabsLoadingTicker();
            setTimeout(() => scheduleChatSessionTabsRefresh(true), 2000);
            return;
        }

        if (Number.isFinite(chatTabsFirstLoadStartPerfMs)) {
            const durationMs = Math.max(0, _chatTabsNowPerfMs() - chatTabsFirstLoadStartPerfMs);
            _storeChatTabsLoadStats(durationMs);
            const stats = _getChatTabsLoadStats();
            console.info('[chatTab] Chat sessions loaded', {
                duration_ms: Math.round(durationMs),
                ema_ms: stats?.ema_ms ?? null,
                samples: stats?.samples ?? 0,
                start_epoch_ms: chatTabsFirstLoadStartEpochMs
            });
            chatTabsFirstLoadStartPerfMs = null;
            chatTabsFirstLoadStartEpochMs = null;
            _stopChatTabsLoadingTicker();
        }

        sessionTabsCache = sessions;
        const activeSessionId = (typeof data?.active_session_id === 'string' && data.active_session_id.trim())
            ? data.active_session_id.trim()
            : null;

        if (activeSessionId) {
            const activeSession = sessions.find(
                s => (typeof s?.session_id === 'string') && s.session_id === activeSessionId
            );
            setActiveChatSession(activeSessionId, activeSession?.session_name);
        }

        renderChatSessionTabs(sessions, activeChatSessionId || activeSessionId);

        const sidForMeta = activeChatSessionId || activeSessionId;
        if (sidForMeta) {
            void loadChatSessionLinks(sidForMeta, { force: false });
        }
    } catch (err) {
        console.error('Failed to load chat sessions:', err);

        // If the refresh failed but we have cached tabs, ensure they remain visible.
        if (Array.isArray(sessionTabsCache) && sessionTabsCache.length > 0) {
            try {
                renderChatSessionTabs(sessionTabsCache, activeChatSessionId);
            } catch (renderErr) {
                console.error('Failed to re-render cached chat tabs:', renderErr);
            }
        } else {
            _stopChatTabsLoadingTicker();
            renderChatSessionTabsPlaceholder('error');
        }
    }
}

function renderChatSessionTabs(sessions, activeSessionId) {
    _stopChatTabsLoadingTicker();
    const container = getChatSessionTabsContainer();
    if (!container) {
        return;
    }

    if (!Array.isArray(sessions) || sessions.length === 0) {
        renderChatSessionTabsPlaceholder('empty');
        lastRenderedSessionCount = 0;
        setChatSessionCount(0);
        sessionTabsCache = [];
        chatSessionMetadataOpenKey = null;
        _clearChatSessionMetadata();
        return;
    }

    container.hidden = false;
    container.innerHTML = '';
    lastRenderedSessionCount = sessions.length;
    setChatSessionCount(sessions.length);

    const fragment = document.createDocumentFragment();
    const hasMultiple = sessions.length > 1;

    if (hasMultiple) {
        const newTab = document.createElement('button');
        newTab.type = 'button';
        newTab.className = 'chat-session-tab chat-session-tab-new';
        newTab.title = 'New chat';
        newTab.setAttribute('aria-label', 'New chat');
        newTab.textContent = '+';
        newTab.addEventListener('click', () => {
            void promptAndCreateChatSession();
        });
        fragment.appendChild(newTab);
    }

    sessions.forEach((session) => {
        const sid = (typeof session?.session_id === 'string') ? session.session_id.trim() : '';
        if (!sid) {
            return;
        }

        const displayName = getSessionDisplayName(session);
        const timestampSource = (typeof session?.last_message_at === 'string' && session.last_message_at.trim())
            ? session.last_message_at.trim()
            : (typeof session?.created_at === 'string' && session.created_at.trim())
                ? session.created_at.trim()
                : '';
        const timestampLabel = formatSessionTimestamp(timestampSource) || '-';
        const tab = document.createElement('button');
        tab.type = 'button';
        tab.className = 'chat-session-tab';
        tab.setAttribute('role', 'tab');
        tab.setAttribute('aria-selected', sid === activeSessionId ? 'true' : 'false');
        tab.dataset.sessionId = sid;
        if (typeof session?.session_name === 'string') {
            tab.dataset.sessionName = session.session_name;
        }

        if (sid === activeSessionId) {
            tab.classList.add('is-active');
        }

        if (sid === loadingChatSessionId) {
            tab.classList.add('is-loading');
        }

        if (session?.is_completed === true) {
            tab.classList.add('is-completed');
            const completedLabel = formatCompletedLabel(session?.completed_at);
            tab.title = `${displayName} • ${timestampLabel} (${completedLabel})`;
        } else {
            tab.classList.add('is-open');
            tab.title = `${displayName} • ${timestampLabel}`;
        }

        const header = document.createElement('span');
        header.className = 'chat-session-tab-header';

        const label = document.createElement('span');
        label.className = 'chat-session-tab-label';
        label.textContent = displayName;
        header.appendChild(label);

        const messageCount = Number.isFinite(session?.message_count) ? Number(session.message_count) : null;
        const turnCount = messageCount === null ? null : Math.max(0, Math.ceil(messageCount / 2));
        if (turnCount !== null && turnCount > 0) {
            const suffix = turnCount === 1 ? 'turn' : 'turns';
            tab.title = `${tab.title} • ${turnCount} ${suffix}`;

            const count = document.createElement('span');
            count.className = 'chat-session-tab-count';
            count.textContent = String(turnCount);
            header.appendChild(count);
        }

        const meta = document.createElement('span');
        meta.className = 'chat-session-tab-meta';
        meta.textContent = timestampLabel;

        tab.appendChild(header);
        tab.appendChild(meta);
        tab.addEventListener('click', () => {
            if (sid === activeChatSessionId) {
                return;
            }
            void switchToChatSession(sid);
        });
        tab.addEventListener('dblclick', () => {
            void promptRenameChatSession(sid, session?.session_name || displayName);
        });
        tab.addEventListener('contextmenu', (event) => {
            event.preventDefault();
            openChatSessionMenu(event.clientX, event.clientY, [
                {
                    label: 'Rename',
                    onClick: () => {
                        void promptRenameChatSession(sid, session?.session_name || displayName);
                    }
                }
            ]);
        });

        fragment.appendChild(tab);
    });

    container.appendChild(fragment);
}

function shouldShowChatTabMenu() {
    const container = getChatSessionTabsContainer();
    if (!container || container.hidden) {
        return false;
    }
    return lastRenderedSessionCount <= 1;
}

function ensureChatSessionMenu() {
    if (chatSessionMenuEl) {
        return chatSessionMenuEl;
    }

    const menu = document.createElement('div');
    menu.className = 'chat-session-menu';
    menu.setAttribute('role', 'menu');
    menu.setAttribute('aria-hidden', 'true');
    document.body.appendChild(menu);

    document.addEventListener('click', (event) => {
        if (menu.classList.contains('open') && !menu.contains(event.target)) {
            closeChatSessionMenu();
        }
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            closeChatSessionMenu();
        }
    });

    chatSessionMenuEl = menu;
    return menu;
}

function populateChatSessionMenu(items) {
    const menu = ensureChatSessionMenu();
    menu.innerHTML = '';
    items.forEach((item) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = item.label;
        button.setAttribute('role', 'menuitem');
        button.addEventListener('click', () => {
            closeChatSessionMenu();
            item.onClick();
        });
        menu.appendChild(button);
    });
    return menu;
}

function openChatSessionMenu(x, y, items) {
    if (!Array.isArray(items) || items.length === 0) {
        return;
    }

    const menu = populateChatSessionMenu(items);
    const padding = 8;
    menu.classList.add('open');
    menu.setAttribute('aria-hidden', 'false');
    const maxX = window.innerWidth - menu.offsetWidth - padding;
    const maxY = window.innerHeight - menu.offsetHeight - padding;
    const left = Math.max(padding, Math.min(x, maxX));
    const top = Math.max(padding, Math.min(y, maxY));
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
}

function closeChatSessionMenu() {
    if (!chatSessionMenuEl) {
        return;
    }
    chatSessionMenuEl.classList.remove('open');
    chatSessionMenuEl.setAttribute('aria-hidden', 'true');
}

function setupChatTabContextMenu() {
    const chatTabButton = getChatTabButton();
    if (!chatTabButton) {
        return;
    }
    chatTabButton.addEventListener('contextmenu', (event) => {
        if (!shouldShowChatTabMenu()) {
            return;
        }
        event.preventDefault();
        openChatSessionMenu(event.clientX, event.clientY, [
            {
                label: 'New chat',
                onClick: () => {
                    void promptAndCreateChatSession();
                }
            }
        ]);
    });
}

async function promptAndCreateChatSession() {
    const proposed = window.prompt('Name this chat (optional)', '');
    if (proposed === null) {
        return;
    }
    try {
        await createChatSession(proposed);
        scheduleChatSessionTabsRefresh(true);
    } catch (err) {
        const msg = err?.message ? String(err.message) : 'Unable to create chat.';
        alert(msg);
    }
}

async function promptRenameChatSession(sessionId, currentName) {
    const proposed = window.prompt('Rename chat', currentName || '');
    if (proposed === null) {
        return;
    }
    const trimmed = String(proposed || '').trim();
    if (!trimmed) {
        alert('Conversation name is required.');
        return;
    }
    try {
        await renameChatSession(sessionId, trimmed);
        scheduleChatSessionTabsRefresh(true);
    } catch (err) {
        const msg = err?.message ? String(err.message) : 'Unable to rename chat.';
        alert(msg);
    }
}

async function createChatSession(sessionName) {
    abortActiveChatRequest();

    const payload = {};
    if (typeof sessionName === 'string' && sessionName.trim()) {
        payload.session_name = sessionName.trim();
    }

    const response = await fetch('/von/api/session/create_chat_session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });
    const data = await response.json();

    if (!response.ok) {
        const msg = data?.error ? String(data.error) : 'Unable to create chat session.';
        throw new Error(msg);
    }

    setActiveChatSession(data?.session_id, data?.session_name);

    const nowIso = new Date().toISOString();
    const effectiveSessionId = (typeof data?.session_id === 'string' && data.session_id.trim())
        ? data.session_id.trim()
        : activeChatSessionId;
    const effectiveName = (typeof data?.session_name === 'string' && data.session_name.trim())
        ? data.session_name.trim()
        : (typeof sessionName === 'string' && sessionName.trim())
            ? sessionName.trim()
            : '';

    if (effectiveSessionId) {
        const newSession = {
            session_id: effectiveSessionId,
            session_name: effectiveName || null,
            message_count: 0,
            last_message_at: nowIso,
            created_at: nowIso,
            is_completed: false,
            completed_at: null
        };
        sessionTabsCache = [
            newSession,
            ...sessionTabsCache.filter(s => String(s?.session_id || '') !== effectiveSessionId)
        ];
        renderChatSessionTabs(sessionTabsCache, effectiveSessionId);
    }

    // Metadata editor: start with a fresh load for the new session.
    chatSessionMetadataOpenKey = null;
    if (effectiveSessionId) {
        void loadChatSessionLinks(effectiveSessionId, { force: true });
    }

    const history = Array.isArray(data?.history) ? data.history : [];
    const scrollableField = document.getElementById('scrollableField');
    if (scrollableField) {
        historySegmentsShown = history.length ? 1 : 0;
        totalHistorySegments = history.length ? 1 : 0;
        updateHistoryBanner();
        rehydrateHistory(scrollableField, history, {
            scrollToBottom: true,
            preserveScroll: false,
            showResetNotice: false,
            forceScrollToBottom: true
        });
    }

    const promptInput = document.getElementById('promptInput');
    if (promptInput) {
        promptInput.value = '';
        promptInput.focus();
    }

    document.dispatchEvent(new CustomEvent('von:contextReset', {
        detail: { trigger: 'chat_new_session', session_id: effectiveSessionId, session_name: effectiveName || null }
    }));

    scheduleChatSessionTabsRefresh(true);
    return data;
}

async function renameChatSession(sessionId, sessionName) {
    const sid = (typeof sessionId === 'string') ? sessionId.trim() : '';
    if (!sid) {
        throw new Error('session_id required');
    }

    const name = (typeof sessionName === 'string') ? sessionName.trim() : '';
    if (!name) {
        throw new Error('session_name required');
    }

    const response = await fetch('/von/api/session/rename_chat_session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sid, session_name: name })
    });
    const data = await response.json();

    if (!response.ok) {
        const msg = data?.error ? String(data.error) : 'Unable to rename chat session.';
        throw new Error(msg);
    }

    if (sid === activeChatSessionId) {
        setActiveChatSession(sid, data?.session_name);
    }

    if (sid) {
        const updatedName = (typeof data?.session_name === 'string' && data.session_name.trim())
            ? data.session_name.trim()
            : name;
        sessionTabsCache = sessionTabsCache.map((session) => {
            if (String(session?.session_id || '') !== sid) {
                return session;
            }
            return {
                ...session,
                session_name: updatedName
            };
        });
        renderChatSessionTabs(sessionTabsCache, activeChatSessionId || sid);
    }

    scheduleChatSessionTabsRefresh(true);
    return data;
}

async function switchToChatSession(sessionId) {
    const sid = String(sessionId || '').trim();
    if (!sid) {
        return { ok: false, error: 'session_id required' };
    }

    const previousSessionId = activeChatSessionId;
    const previousSessionName = activeChatSessionName;

    // Best-effort: flush any pending metadata save before switching away.
    if (previousSessionId) {
        void _flushPendingChatSessionLinksSave(previousSessionId);
    }
    const cachedSession = sessionTabsCache.find(
        session => String(session?.session_id || '') === sid
    );
    const targetName = cachedSession?.session_name || null;
    const shouldReuseCachedHistory = canReuseSessionHistory(sid);
    const switchStart = performance.now();
    console.log('[chatTab] switchToChatSession start', {
        from_session_id: previousSessionId || null,
        from_session_name: previousSessionName || null,
        to_session_id: sid,
        to_session_name: targetName,
        cache_reuse: shouldReuseCachedHistory
    });

    setActiveChatSession(sid, cachedSession?.session_name);
    if (sessionTabsCache.length > 0) {
        renderChatSessionTabs(sessionTabsCache, sid);
    }
    setChatSessionTabLoading(sid, true);

    // Render metadata panel immediately (cached if available).
    chatSessionMetadataOpenKey = null;
    void loadChatSessionLinks(sid, { force: false });

    abortActiveHistoryRequest();
    abortActiveChatRequest();

    const scrollableField = document.getElementById('scrollableField');
    if (!scrollableField) {
        setChatSessionTabLoading(sid, false);
        return { ok: false, error: 'Conversation panel unavailable.' };
    }

    scrollableField.innerHTML = '<div class="chat-session-loading">Switching chat…</div>';
    historySegmentsShown = 0;
    totalHistorySegments = 0;
    updateHistoryBanner();

    try {
        const setSessionStart = performance.now();
        const response = await fetch('/von/api/session/set_chat_session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sid, include_history: false })
        });
        const data = await response.json();
        const setSessionMs = Math.round(performance.now() - setSessionStart);
        console.log('[chatTab] switchToChatSession set_chat_session', {
            ok: response.ok,
            status: response.status,
            duration_ms: setSessionMs,
            session_id: data?.session_id || sid
        });

        if (!response.ok) {
            const msg = data?.error ? String(data.error) : 'Unable to switch session.';
            setActiveChatSession(previousSessionId, previousSessionName);
            if (sessionTabsCache.length > 0) {
                renderChatSessionTabs(sessionTabsCache, previousSessionId || sid);
            }

            chatSessionMetadataOpenKey = null;
            if (previousSessionId) {
                void loadChatSessionLinks(previousSessionId, { force: false });
            } else {
                _clearChatSessionMetadata();
            }

            setChatSessionTabLoading(sid, false);
            scrollableField.innerHTML = `<div class="chat-session-loading">${escapeHtml(msg)}</div>`;
            void loadChatHistory({
                segments: 1,
                scrollToBottom: true,
                showResetNotice: false,
                forceScrollToBottom: true
            });
            return { ok: false, error: msg };
        }

        setActiveChatSession(data?.session_id, data?.session_name);

        // Refresh metadata (server-side session may have changed).
        const effectiveMetaSessionId = (typeof data?.session_id === 'string' && data.session_id.trim())
            ? data.session_id.trim()
            : sid;
        chatSessionMetadataOpenKey = null;
        void loadChatSessionLinks(effectiveMetaSessionId, { force: true });

        if (shouldReuseCachedHistory) {
            const cached = getSessionHistoryCache(sid);
            const reused = rehydrateFromCache(scrollableField, cached);
            console.log('[chatTab] switchToChatSession cache reuse', {
                loaded: reused,
                cached_messages: cached?.history?.length ?? 0,
                session_id: sid
            });
            if (!reused) {
                scrollableField.innerHTML = '';
            }
            setChatSessionTabLoading(sid, false);
        } else {
            const recentStart = performance.now();
            const loaded = await loadRecentChatPair({
                scrollToBottom: true,
                preserveScroll: false,
                showResetNotice: false,
                forceScrollToBottom: true
            });
            const recentMs = Math.round(performance.now() - recentStart);
            console.log('[chatTab] switchToChatSession recent pair', {
                loaded,
                duration_ms: recentMs,
                session_id: sid
            });
            if (!loaded) {
                scrollableField.innerHTML = '';
            }

            setTimeout(() => {
                if (activeChatSessionId !== sid) {
                    setChatSessionTabLoading(sid, false);
                    return;
                }
                console.log('[chatTab] switchToChatSession backfill start', { session_id: sid });
                const backfillPromise = loadChatHistory({
                    segments: 1,
                    scrollToBottom: false,
                    preserveScroll: true,
                    showResetNotice: false
                });
                backfillPromise.finally(() => {
                    if (activeChatSessionId === sid) {
                        setChatSessionTabLoading(sid, false);
                    }
                });
            }, 250);
        }

        document.dispatchEvent(new CustomEvent('von:contextReset', {
            detail: { trigger: 'history_session_switch', session_id: sid, session_name: data?.session_name || targetName }
        }));

        const promptInput = document.getElementById('promptInput');
        if (promptInput) {
            promptInput.focus();
        }

        scheduleChatSessionTabsRefresh(true);
        console.log('[chatTab] switchToChatSession done', {
            session_id: sid,
            duration_ms: Math.round(performance.now() - switchStart)
        });
        return { ok: true, data };
    } catch (err) {
        console.error('Error switching chat session:', err);
        setActiveChatSession(previousSessionId, previousSessionName);
        if (sessionTabsCache.length > 0) {
            renderChatSessionTabs(sessionTabsCache, previousSessionId || sid);
        }

        chatSessionMetadataOpenKey = null;
        if (previousSessionId) {
            void loadChatSessionLinks(previousSessionId, { force: false });
        } else {
            _clearChatSessionMetadata();
        }

        setChatSessionTabLoading(sid, false);
        scrollableField.innerHTML = '<div class="chat-session-loading">Unable to switch session.</div>';
        void loadChatHistory({
            segments: 1,
            scrollToBottom: true,
            showResetNotice: false,
            forceScrollToBottom: true
        });
        console.log('[chatTab] switchToChatSession failed', {
            session_id: sid,
            duration_ms: Math.round(performance.now() - switchStart)
        });
        return { ok: false, error: 'Unable to switch session.' };
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
            const sessionCount = (typeof data.session_count === 'number') ? data.session_count : null;
            const authenticated = data.authenticated !== undefined ? data.authenticated : true;
            const historyLengthElement = document.getElementById('chat-history-length');
            if (historyLengthElement) {
                if (!authenticated) {
                    historyLengthElement.textContent = 'History: unauthenticated';
                } else {
                    const contextCount = transcriptTurns.length;
                    const conversationsText = (sessionCount === null) ? '- conversations' : `${sessionCount} conversations`;
                    historyLengthElement.textContent = `History: ${conversationsText} | this ${contextCount}`;
                    historyLengthElement.title = `Conversation history: ${sessionCount ?? '—'} conversations • total ${historyLength} messages • this session ${contextCount} messages`;
                }

                // Wire a lightweight history popup (scrollable list of sessions).
                if (!historyLengthElement._wired) {
                    historyLengthElement._wired = true;
                    historyLengthElement.style.cursor = 'pointer';
                    historyLengthElement.addEventListener('click', async () => {
                        const modal = document.getElementById('historyStatusModal');
                        const body = document.getElementById('historyStatusBody');
                        const closeBtn = document.getElementById('historyStatusClose');
                        const titleEl = document.getElementById('historyStatusTitle');
                        if (!modal || !body) {
                            return;
                        }

                        try {
                            modal.classList.add('open');
                            modal.setAttribute('aria-hidden', 'false');
                            body.innerHTML = '<p>Loading…</p>';

                            if (titleEl) {
                                titleEl.textContent = 'Conversation history';
                            }

                            if (closeBtn && !closeBtn._wired) {
                                closeBtn._wired = true;
                                closeBtn.addEventListener('click', () => {
                                    modal.classList.remove('open');
                                    modal.setAttribute('aria-hidden', 'true');
                                });
                            }

                            // Fetch a fresh summary for the header.
                            let totalMessages = null;
                            let conversations = null;
                            try {
                                const lenRes = await fetch('/von/history/length', { cache: 'no-store' });
                                if (lenRes.ok) {
                                    const lenJs = await lenRes.json();
                                    totalMessages = (typeof lenJs?.history_length === 'number') ? lenJs.history_length : null;
                                    conversations = (typeof lenJs?.session_count === 'number') ? lenJs.session_count : null;
                                }
                            } catch (_) { /* ignore */ }

                            const res = await fetch('/von/history/sessions?limit=50', { cache: 'no-store' });
                            const js = await res.json();
                            if (!res.ok || js?.authenticated === false) {
                                body.innerHTML = '<p>History unavailable (not logged in).</p>';
                                return;
                            }

                            const sessions = Array.isArray(js?.sessions) ? js.sessions : [];
                            const activeSessionId = (typeof js?.active_session_id === 'string')
                                ? js.active_session_id
                                : null;
                            if (activeSessionId) {
                                const activeSession = sessions.find(
                                    s => (typeof s?.session_id === 'string') && s.session_id === activeSessionId
                                );
                                setActiveChatSession(activeSessionId, activeSession?.session_name);
                            }
                            if (sessions.length === 0) {
                                body.innerHTML = '<p>No saved sessions.</p>';
                                return;
                            }

                            const currentCount = transcriptTurns.length;
                            if (titleEl) {
                                const conversationsText2 = (chats === null) ? '— conversations' : `${chats} conversations`;
                                const totalText2 = (totalMessages === null) ? '— total' : `${totalMessages} total`;
                                titleEl.textContent = `Conversation history — ${conversationsText2} • ${totalText2} • this ${currentCount}`;
                            }

                            const rows = sessions.map((s) => {
                                const sidRaw = s?.session_id ? String(s.session_id) : '(unknown session)';
                                const sidShort = (sidRaw.length > 10) ? `${sidRaw.slice(0, 8)}.` : sidRaw;
                                const count = (typeof s?.message_count === 'number') ? s.message_count : 0;

                                const lastAt = (typeof s?.last_message_at === 'string') ? s.last_message_at : null;
                                const lastAtShort = lastAt ? lastAt.replace('T', ' ').replace('Z', '') : '-';
                                const preview = (typeof s?.preview === 'string' && s.preview.trim()) ? s.preview.trim() : '-';

                                const nameRaw = (typeof s?.session_name === 'string' && s.session_name.trim())
                                    ? s.session_name.trim()
                                    : null;
                                const displayName = nameRaw || sidShort;
                                const nameTitle = nameRaw || sidRaw;

                                const ns = (typeof s?.namespace === 'string' && s.namespace.trim()) ? s.namespace.trim() : null;
                                const nsShort = ns ? (ns.length > 48 ? `${ns.slice(0, 46)}.` : ns) : null;

                                const sessionAttr = escapeHtml(sidRaw);
                                const nameAttr = nameRaw ? escapeHtml(nameRaw) : '';
                                const isActive = activeSessionId && sidRaw === activeSessionId;
                                const isCompleted = s?.is_completed === true;
                                const rowClass = `history-session-row${isActive ? ' is-active' : ''}${isCompleted ? ' is-completed' : ''}`;
                                const idSnippet = nameRaw ? `  id ${escapeHtml(sidShort)}` : '';
                                const completionSnippet = isCompleted
                                    ? `  ${escapeHtml(formatCompletedLabel(s?.completed_at))}`
                                    : '';

                                return [
                                    `<li class="${rowClass}" role="button" tabindex="0" data-session-id="${sessionAttr}" data-session-name="${nameAttr}">`,
                                    '<div class="history-session-content">',
                                    `<strong class="history-session-id" title="${escapeHtml(nameTitle)}">${escapeHtml(displayName)}<\/strong>`,
                                    `<span class="history-session-meta">${count} msgs  last ${escapeHtml(lastAtShort)}${completionSnippet}${idSnippet}${nsShort ? `  ns ${escapeHtml(nsShort)}` : ''}<\/span>`,
                                    `<span class="history-session-preview" title="${escapeHtml(preview)}">${escapeHtml(preview)}<\/span>`,
                                    '<\/div>',
                                    '<\/li>'
                                ].join('');
                            });

                            const summaryLine = `Showing ${sessions.length} most recent sessions (sorted by last message time)`;

                            body.innerHTML = [
                                `<p class="history-session-summary">${escapeHtml(summaryLine)}<\/p>`,
                                '<div class="history-session-scroll">',
                                '<ul class="history-session-list">',
                                ...rows,
                                '<\/ul>',
                                '<\/div>'
                            ].join('');

                            const switchToSession = async (sessionId) => {
                                const sid = String(sessionId || '').trim();
                                if (!sid) {
                                    return;
                                }

                                body.innerHTML = '<p>Switching session.</p>';
                                const result = await switchToChatSession(sid);
                                if (!result.ok) {
                                    body.innerHTML = `<p>${escapeHtml(result.error || 'Unable to switch session.')}</p>`;
                                    return;
                                }

                                modal.classList.remove('open');
                                modal.setAttribute('aria-hidden', 'true');
                            };

                            const list = body.querySelector('.history-session-list');
                            if (list && !list._wiredSessionSwitch) {
                                list._wiredSessionSwitch = true;

                                list.addEventListener('click', (e) => {
                                    const row = e.target?.closest ? e.target.closest('.history-session-row') : null;
                                    const sid = row?.dataset?.sessionId;
                                    if (sid) {
                                        void switchToSession(sid);
                                    }
                                });

                                list.addEventListener('keydown', (e) => {
                                    if (e.key !== 'Enter' && e.key !== ' ') {
                                        return;
                                    }
                                    const row = e.target?.closest ? e.target.closest('.history-session-row') : null;
                                    const sid = row?.dataset?.sessionId;
                                    if (sid) {
                                        e.preventDefault();
                                        void switchToSession(sid);
                                    }
                                });
                            }

                        } catch (err) {
                            console.error('Error loading history sessions:', err);
                            body.innerHTML = '<p>Unable to load history sessions.</p>';
                        }
                    });
                }
            }

            scheduleChatSessionTabsRefresh();
        } else {
            console.error('Failed to load Conversation history length:', data.error);
        }
    } catch (error) {
        console.error('Error loading Conversation history length:', error);
    }
}

async function loadChatHistory(options = {}) {
    const {
        segments,
        scrollToBottom = true,
        preserveScroll = false,
        showResetNotice = false,
        forceScrollToBottom = false,
        segmentSizeOverride = null,
        tailLimit = null,
        filterRecentPair = false
    } = options;

    const scrollableField = document.getElementById('scrollableField');
    if (!scrollableField) {
        console.error('scrollableField not found');
        return false;
    }

    const requestedSegments = Number.isInteger(segments) && segments > 0 ? segments : historySegmentsShown;
    const segmentCount = Math.max(requestedSegments || 1, 1);

    const requestedSessionId = activeChatSessionId;
    abortActiveHistoryRequest();
    const requestId = ++historyRequestCounter;
    const abortController = new AbortController();
    activeHistoryRequest = {
        id: requestId,
        sessionId: requestedSessionId,
        abortController,
        aborted: false
    };

    // Get user context to ensure we can load history even if session is new
    const userContext = getUserContext();
    const params = new URLSearchParams({ segments: segmentCount.toString() });
    if (userContext && userContext.user_id) {
        params.append('user_id', userContext.user_id);
    }
    if (requestedSessionId) {
        params.append('session_id', requestedSessionId);
    }
    const effectiveSegmentSize = Number.isInteger(segmentSizeOverride) && segmentSizeOverride > 0
        ? segmentSizeOverride
        : HISTORY_SEGMENT_SIZE;
    if (effectiveSegmentSize > 0) {
        params.append('segment_size', effectiveSegmentSize.toString());
    }
    if (Number.isInteger(tailLimit) && tailLimit > 0) {
        params.append('tail_limit', tailLimit.toString());
    }
    params.append('include_debug', '0');
    try {
        console.log('[chatTab] loadChatHistory request', {
            session_id: requestedSessionId || null,
            segments: segmentCount,
            segment_size: effectiveSegmentSize,
            tail_limit: Number.isInteger(tailLimit) ? tailLimit : null,
            filter_recent_pair: !!filterRecentPair
        });
        const response = await fetch(`/von/history?${params.toString()}`, {
            signal: abortController.signal
        });
        const data = await response.json();

        if (!activeHistoryRequest || activeHistoryRequest.id !== requestId) {
            return false;
        }
        if (requestedSessionId && activeChatSessionId && requestedSessionId !== activeChatSessionId) {
            return false;
        }

        console.log(`[chatTab] loadChatHistory response: ok=${response.ok}, segments=${data.segments_returned}, total=${data.total_segments}, history_len=${data.history ? data.history.length : 'undefined'}`);

        if (response.ok && data.history && Array.isArray(data.history)) {
            const historyMessages = filterRecentPair
                ? selectRecentChatPair(data.history)
                : data.history;
            const hasMoreHistory = data?.has_more_history === true;
            const segmentsReturned = Math.max(data.segments_returned || segmentCount, 0);
            let totalSegments = Math.max(data.total_segments || segmentsReturned, segmentsReturned);
            if (hasMoreHistory && totalSegments <= segmentsReturned) {
                totalSegments = segmentsReturned + 1;
            }
            historySegmentsShown = segmentsReturned;
            totalHistorySegments = totalSegments;

            rehydrateHistory(scrollableField, historyMessages, {
                scrollToBottom,
                preserveScroll,
                showResetNotice,
                forceScrollToBottom
            });

            updateHistoryBanner();
            const sessionMeta = sessionTabsCache.find(
                session => String(session?.session_id || '') === String(requestedSessionId || '')
            );
            updateSessionHistoryCache(requestedSessionId, historyMessages, {
                message_count: sessionMeta?.message_count ?? null,
                last_message_at: sessionMeta?.last_message_at ?? null,
                segments: historySegmentsShown,
                total_segments: totalHistorySegments
            });
            console.log(`Loaded ${data.history.length} historical messages across ${historySegmentsShown} segment(s)`);
            if (activeHistoryRequest && activeHistoryRequest.id === requestId) {
                activeHistoryRequest = null;
            }
            return true;
        }

        const hasMoreHistory = data?.has_more_history === true;
        const segmentsReturned = Math.max(data?.segments_returned || 0, 0);
        let totalSegments = Math.max(data?.total_segments || segmentsReturned, segmentsReturned);
        if (hasMoreHistory && totalSegments <= segmentsReturned) {
            totalSegments = segmentsReturned + 1;
        }
        historySegmentsShown = segmentsReturned;
        totalHistorySegments = totalSegments;
        updateHistoryBanner();
        console.log('No Conversation history to load or empty history');
        if (activeHistoryRequest && activeHistoryRequest.id === requestId) {
            activeHistoryRequest = null;
        }
        return false;
    } catch (error) {
        if (error?.name === 'AbortError') {
            return false;
        }
        console.error('Error loading Conversation history:', error);
        updateHistoryBanner();
        if (activeHistoryRequest && activeHistoryRequest.id === requestId) {
            activeHistoryRequest = null;
        }
        return false;
    }
}

function forceScrollToBottomWithRetries(scrollableField, options = {}) {
    const { attempts = 6 } = options;
    const maxAttempts = Number.isInteger(attempts) && attempts > 0 ? attempts : 1;

    const requestFrame = (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function')
        ? window.requestAnimationFrame.bind(window)
        : (cb) => setTimeout(cb, 0);

    let remaining = maxAttempts;
    const tick = () => {
        if (!scrollableField || remaining <= 0) {
            return;
        }

        scrollableField.scrollTop = scrollableField.scrollHeight;
        remaining -= 1;
        requestFrame(tick);
    };

    // A few frames is usually enough to catch delayed markdown/layout changes.
    requestFrame(tick);
}

async function loadRecentChatPair(options = {}) {
    return loadChatHistory({
        ...options,
        segments: 1,
        segmentSizeOverride: HISTORY_TAIL_SEGMENT_SIZE,
        tailLimit: HISTORY_TAIL_SEGMENT_SIZE,
        filterRecentPair: true
    });
}

function selectRecentChatPair(historyMessages) {
    if (!Array.isArray(historyMessages) || historyMessages.length === 0) {
        return [];
    }

    const eligible = historyMessages.filter(
        (msg) => msg && (msg.role === 'user' || msg.role === 'assistant')
    );
    if (eligible.length === 0) {
        return [];
    }

    let lastAssistantIndex = -1;
    for (let i = eligible.length - 1; i >= 0; i -= 1) {
        if (eligible[i].role === 'assistant') {
            lastAssistantIndex = i;
            break;
        }
    }

    if (lastAssistantIndex === -1) {
        return eligible.slice(-1);
    }

    let lastUserIndex = -1;
    for (let i = lastAssistantIndex - 1; i >= 0; i -= 1) {
        if (eligible[i].role === 'user') {
            lastUserIndex = i;
            break;
        }
    }

    if (lastUserIndex === -1) {
        return eligible.slice(lastAssistantIndex);
    }

    return eligible.slice(lastUserIndex);
}

function rehydrateHistory(scrollableField, historyMessages, options = {}) {
    const {
        scrollToBottom = true,
        preserveScroll = false,
        showResetNotice = false,
        forceScrollToBottom = false
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
            const hasDebugData = msg.role === 'assistant'
                && (!!msg.llm_debug_data || !!msg.history_location);
            if (msg.role === 'assistant') {
                const merged = {
                    ...(msg.llm_debug_data && typeof msg.llm_debug_data === 'object' ? msg.llm_debug_data : {}),
                    history_location: msg.history_location || null
                };
                llmDebugData.set(turnId, merged);
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

            if (forceScrollToBottom) {
                forceScrollToBottomWithRetries(scrollableField);
            }
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
    const dictateButton = document.getElementById('dictateButton');
    const ttsToggle = document.getElementById('ttsToggle');
    const exportConversationJsonBtn = document.getElementById('exportConversationJsonBtn');
    const exportConversationMarkdownBtn = document.getElementById('exportConversationMarkdownBtn');
    const uploadFileButton = document.getElementById('uploadFileButton');
    const uploadFileInput = document.getElementById('uploadFileInput');
    const uploadFileStatus = document.getElementById('uploadFileStatus');
    const chatTab = document.getElementById('chatTab');

    if (!sendButton || !resetButton || !promptInput) {
        console.error("Chat tab elements not found");
        return;
    }

    if (!isAnnotationEnabled() && annotationToggle) {
        const label = annotationToggle.closest('.annotation-toggle');
        if (label) {
            label.remove();
        } else {
            annotationToggle.remove();
        }
    }

    uploadUiState.button = uploadFileButton || null;
    uploadUiState.statusEl = uploadFileStatus || null;

    if (uploadFileButton && uploadFileInput) {
        uploadFileButton.addEventListener('click', () => {
            try {
                uploadFileInput.click();
            } catch (_) {
                // Ignore.
            }
        });

        uploadFileInput.addEventListener('change', async () => {
            const files = uploadFileInput.files;
            // Clear the input so selecting the same file again triggers change.
            uploadFileInput.value = '';
            await uploadFilesToVon(files);
        });
    }

    if (chatTab && scrollableField) {
        // Prevent the browser navigating away when dropping files.
        const preventIfFiles = (event) => {
            if (!isFileDragEvent(event)) return;
            event.preventDefault();
            event.stopPropagation();
        };

        // Global guards
        document.addEventListener('dragover', preventIfFiles);
        document.addEventListener('drop', preventIfFiles);

        // Local UI + drop handling
        const applyDragOverState = () => {
            scrollableField.classList.add('drag-over');
            if (promptInput) {
                promptInput.classList.add('drag-over');
            }
        };

        const clearDragOverState = () => {
            scrollableField.classList.remove('drag-over');
            if (promptInput) {
                promptInput.classList.remove('drag-over');
            }
        };

        chatTab.addEventListener('dragover', (event) => {
            if (!isFileDragEvent(event)) return;
            preventIfFiles(event);
            applyDragOverState();
        });

        chatTab.addEventListener('dragleave', (event) => {
            if (!isFileDragEvent(event)) return;
            clearDragOverState();
        });

        chatTab.addEventListener('drop', async (event) => {
            if (!isFileDragEvent(event)) return;
            preventIfFiles(event);
            clearDragOverState();

            const dt = event.dataTransfer;
            const files = dt ? dt.files : null;
            await uploadFilesToVon(files);
        });

        if (promptInput) {
            promptInput.addEventListener('dragover', (event) => {
                if (!isFileDragEvent(event)) return;
                preventIfFiles(event);
                applyDragOverState();
            });

            promptInput.addEventListener('dragleave', (event) => {
                if (!isFileDragEvent(event)) return;
                clearDragOverState();
            });

            promptInput.addEventListener('drop', async (event) => {
                if (!isFileDragEvent(event)) return;
                preventIfFiles(event);
                clearDragOverState();

                const dt = event.dataTransfer;
                const files = dt ? dt.files : null;
                await uploadFilesToVon(files);
            });
        }
    }

    // Initialize LLM debug popup handlers
    initializeLlmDebugPopup();
    initializeHistoryControls();
    updateHistoryBanner();
    setupChatTabContextMenu();

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

    // Load TTS toggle state from localStorage (default: false)
    if (ttsToggle) {
        const savedTts = safeLocalStorageGet(CHAT_TTS_STORAGE_KEY);
        ttsToggle.checked = savedTts === 'true';

        if (!isTextToSpeechSupported()) {
            ttsToggle.disabled = true;
            ttsToggle.checked = false;
            ttsToggle.title = 'Text-to-speech is not supported in this browser.';
        }

        ttsToggle.addEventListener('change', (e) => {
            const enabled = !!e.target.checked;
            safeLocalStorageSet(CHAT_TTS_STORAGE_KEY, enabled ? 'true' : 'false');

            // If disabled while speaking, stop immediately.
            if (!enabled) {
                stopSpeaking();
                clearActiveTtsUi();
            }
        });
    }

    // Dictation (STT): optional browser capability.
    if (dictateButton) {
        if (!isSpeechRecognitionSupported()) {
            dictateButton.disabled = true;
            dictateButton.title = 'Dictation is not supported in this browser.';
        } else {
            dictateButton.addEventListener('click', () => {
                if (activeDictation) {
                    stopDictation();
                    return;
                }

                if (!promptInput) {
                    return;
                }

                const baseText = String(promptInput.value || '');
                dictationState = {
                    baseText,
                    finalText: '',
                    interimText: ''
                };

                dictateButton.textContent = 'Stop dictation';
                dictateButton.classList.add('active-dictation');

                try {
                    const settings = getChatSpeechSettings();
                    activeDictation = startSpeechRecognition({
                        language: settings.stt.language,
                        continuous: settings.stt.continuous,
                        interimResults: settings.stt.interimResults,
                        onResult: ({ finalText, interimText }) => {
                            if (!dictationState) {
                                return;
                            }

                            if (finalText) {
                                dictationState.finalText = [dictationState.finalText, finalText]
                                    .map(t => String(t || '').trim())
                                    .filter(Boolean)
                                    .join(' ');
                            }

                            dictationState.interimText = String(interimText || '').trim();

                            const baseText = String(dictationState.baseText || '');
                            const dictatedText = [dictationState.finalText, dictationState.interimText]
                                .map(t => String(t || '').trim())
                                .filter(Boolean)
                                // Normalise spaces/tabs, but preserve newlines.
                                .join(' ')
                                .replace(/[ \t]+/g, ' ');

                            if (!dictatedText) {
                                promptInput.value = baseText;
                            } else {
                                const needsSpacer = baseText.length > 0 && !/[ \t\n]$/.test(baseText);
                                promptInput.value = `${baseText}${needsSpacer ? ' ' : ''}${dictatedText}`;
                            }
                            promptInput.dispatchEvent(new Event('input', { bubbles: true }));
                        },
                        onError: (event) => {
                            console.warn('[chatTab] Dictation error:', event);
                        },
                        onEnd: () => {
                            activeDictation = null;
                            dictationState = null;
                            dictateButton.textContent = 'Dictate';
                            dictateButton.classList.remove('active-dictation');
                        }
                    });
                } catch (err) {
                    console.warn('[chatTab] Unable to start dictation:', err);
                    activeDictation = null;
                    dictationState = null;
                    dictateButton.textContent = 'Dictate';
                    dictateButton.classList.remove('active-dictation');
                }
            });
        }
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

    void loadRecentChatPair({
        scrollToBottom: true,
        preserveScroll: false,
        showResetNotice: false,
        forceScrollToBottom: true
    }).then(() => loadChatHistory({
        segments: 1,
        scrollToBottom: false,
        preserveScroll: true,
        showResetNotice: false
    })).catch(() => { });
    void refreshChatSessionTabs();
    void refreshToolUseDuringThinkingSetting();
    console.log("Chat tab initialized successfully");
}

function setThinkingState(isThinking) {
    const loadingIndicator = document.getElementById('loadingIndicator');
    const loadingDetail = document.getElementById('loadingIndicatorDetail');
    const abortButton = document.getElementById('abortButton');
    const sendButton = document.getElementById('sendButton');

    if (loadingIndicator) {
        loadingIndicator.style.display = isThinking ? 'inline-flex' : 'none';
        loadingIndicator.setAttribute('aria-hidden', isThinking ? 'false' : 'true');
        if (isThinking) {
            setLoadingIndicatorText(DEFAULT_THINKING_TEXT);
            setLoadingIndicatorTooltip('');
        } else {
            setLoadingIndicatorText(DEFAULT_THINKING_TEXT);
            setLoadingIndicatorTooltip('');
        }
    }

    if (loadingDetail) {
        loadingDetail.style.display = isThinking ? 'block' : 'none';
        if (!isThinking) {
            setLoadingIndicatorDetailText('');
        }
    }

    if (sendButton) {
        sendButton.disabled = !!isThinking;
    }

    if (abortButton) {
        abortButton.style.display = isThinking ? 'inline-flex' : 'none';
        abortButton.setAttribute('aria-hidden', isThinking ? 'false' : 'true');
    }
}

function restorePromptEditingState(request) {
    const promptInput = document.getElementById('promptInput');
    if (!promptInput || !request) {
        return;
    }

    const currentValue = String(promptInput.value ?? '').trim();
    if (!currentValue) {
        promptInput.value = request.promptRaw || '';
        promptInput.dispatchEvent(new Event('input', { bubbles: true }));
    }

    try {
        if (!currentValue) {
            const valueLength = promptInput.value.length;
            const start = Number.isInteger(request.selectionStart) ? request.selectionStart : valueLength;
            const end = Number.isInteger(request.selectionEnd) ? request.selectionEnd : start;
            promptInput.setSelectionRange(Math.min(start, valueLength), Math.min(end, valueLength));
        }
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

    stopToolUseProgressPolling(request);
    stopThinkingTooltipTicker(request);

    try {
        request.abortController?.abort();
    } catch (_) {
        // Ignore abort errors.
    }

    setThinkingState(false);
    restorePromptEditingState(request);
}

function abortActiveHistoryRequest() {
    if (!activeHistoryRequest) {
        return;
    }

    const request = activeHistoryRequest;
    activeHistoryRequest = null;
    request.aborted = true;

    try {
        request.abortController?.abort();
    } catch (_) {
        // Ignore abort errors.
    }
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

    // Refresh setting in the background; default is enabled.
    void refreshToolUseDuringThinkingSetting();

    const clientRequestId = createClientRequestId();

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
            aborted: false,
            clientRequestId,
            thinkingStartedAtMs: Date.now(),
            toolUseProgressHistory: []
        };
        activeChatRequest = request;

        setLoadingIndicatorTooltip(formatToolUseHistoryTooltip(request));

        startThinkingTooltipTicker(request);

        // Start polling immediately while the request is in flight.
        startToolUseProgressPolling(request);

        // Get user context from localStorage to send to backend
        const userContext = getUserContext();
        // Presenter-mode controls whether the backend produces two-channel output
        // (screen + spoken). This should be enabled regardless of whether auto-TTS
        // is enabled, so clicking Speak later never needs to read raw markdown.
        const presenterMode = true;

        const response = await fetch('/von/generate', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            signal: request.abortController.signal,
            body: JSON.stringify({
                prompt: promptText,
                client_request_id: request.clientRequestId,
                user_id: userContext.user_id,
                org_id: userContext.org_id,
                language: userContext.language,
                gmail_profile: userContext.gmail_profile,
                presenter_mode: presenterMode
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
                const presenterChannelsRaw = data.presenter_channels || data.response_channels || data?.metadata?.presenter_channels;
                const responseChannels = normalisePresenterChannels(presenterChannelsRaw);
                const screenText = responseChannels?.screen ? responseChannels.screen : String(data.response ?? '');
                const spokenText = responseChannels?.spoken ? responseChannels.spoken : null;
                const enriched = enrichDebugDataWithSpeechPlanning(data.llm_debug, {
                    presenterChannels: responseChannels,
                    screenText,
                    spokenText
                });
                llmDebugData.set(assistantTurnId, enriched);
                console.log('[chatTab] Stored LLM debug data for turn:', assistantTurnId);
            }

            const fastpathMeta = data.fastpath || (data.llm_debug && data.llm_debug.fastpath) || null;

            const presenterChannelsRaw = data.presenter_channels || data.response_channels || data?.metadata?.presenter_channels;
            const responseChannels = normalisePresenterChannels(presenterChannelsRaw);
            const screenText = responseChannels?.screen ? responseChannels.screen : String(data.response ?? '');
            const spokenText = responseChannels?.spoken ? responseChannels.spoken : null;

            const toolProgressEnabled = data?.llm_debug?.internal_mcp?.tool_use_progress?.enabled;
            if (toolProgressEnabled === false) {
                stopToolUseProgressPolling(request);
                setLoadingIndicatorText(DEFAULT_THINKING_TEXT);
            }

            // Append assistant message with turnId and llm_debug flag
            appendMessage('Von', screenText, assistantTurnId, !!data.llm_debug, false, null, fastpathMeta, spokenText);
            // Annotate assistant turn and render suggestions when returned - only if toggle is enabled
            const annotationToggle = document.getElementById('annotationToggle');
            if (annotationToggle && annotationToggle.checked) {
                try {
                    annotateTurn({
                        conversation_id: elements.conversationId || 'local',
                        turn_id: assistantTurnId,
                        speaker: 'assistant',
                        text: screenText
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
            stopToolUseProgressPolling(request);
            stopThinkingTooltipTicker(request);
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
                scrollToBottom: true,
                showResetNotice: true,
                forceScrollToBottom: true
            });

            if (!loaded) {
                const scrollableField = document.getElementById('scrollableField');
                appendResetNotice(scrollableField);
            }

            transcriptTurns.length = 0;
            llmDebugData.clear();
            updateHistoryLength();
            scheduleChatSessionTabsRefresh(true);

            // Trigger immediate health poll to update RAG cartouche with new session context
            // Dispatch custom event that main.js health polling can listen for
            document.dispatchEvent(new CustomEvent('von:contextReset', {
                detail: { trigger: 'chat_reset', session_id: activeChatSessionId || null, session_name: activeChatSessionName || null }
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

function appendMessage(sender, message, turnId, hasLlmDebug = false, isHistory = false, timestampStr = null, fastpathMeta = null, ttsText = null) {
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
            // IMPORTANT: in a flex row, children default to min-width:auto, which can
            // force horizontal overflow and clip the header control buttons when the
            // left header text is long. min-width:0 allows proper wrapping/shrinking.
            messageContent.style.cssText = 'flex: 1; min-width: 0; line-height: 1.5;';

            const rawText = String(message ?? '');

            const screenTextForTurn = rawText;
            let spokenTextForTurn = (typeof ttsText === 'string' && ttsText.trim()) ? ttsText : null;
            if (!spokenTextForTurn && turnId) {
                const debugDataForTurn = llmDebugData.get(turnId);
                const channels = normalisePresenterChannels(debugDataForTurn?.presenter_channels);
                if (channels?.spoken && channels.spoken.trim()) {
                    spokenTextForTurn = channels.spoken;
                }
            }

            async function ensureHistorySpokenTalkTrack() {
                if (!isHistory || !turnId) {
                    return null;
                }

                const recentFailure = historySpokenBackfillFailures.get(turnId);
                if (recentFailure && (Date.now() - recentFailure.at) < HISTORY_SPOKEN_BACKFILL_FAILURE_COOLDOWN_MS) {
                    return null;
                }

                const debugDataForTurn = llmDebugData.get(turnId);
                const historyLocation = debugDataForTurn?.history_location;
                if (!historyLocation || typeof historyLocation !== 'object') {
                    return null;
                }

                try {
                    const resp = await fetch('/von/history/backfill_spoken', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ history_location: historyLocation })
                    });

                    const data = await resp.json().catch(() => ({}));
                    if (!resp.ok) {
                        const errMsg = (data && typeof data.error === 'string' && data.error.trim())
                            ? data.error.trim()
                            : `HTTP ${resp.status}`;
                        historySpokenBackfillFailures.set(turnId, { at: Date.now(), error: errMsg, shownAt: null });
                        console.warn('[chatTab] backfill_spoken failed:', data);
                        return null;
                    }

                    const channels = normalisePresenterChannels(data?.presenter_channels);
                    if (!channels?.spoken || !channels.spoken.trim()) {
                        return null;
                    }

                    // Cache the generated channels in-memory so subsequent clicks work.
                    const existing = llmDebugData.get(turnId);
                    llmDebugData.set(turnId, {
                        ...(existing && typeof existing === 'object' ? existing : {}),
                        presenter_channels: channels
                    });

                    // Clear any previous failure cool-down.
                    historySpokenBackfillFailures.delete(turnId);

                    return channels.spoken;
                } catch (err) {
                    console.warn('[chatTab] backfill_spoken request failed:', err);
                    const errMsg = err && typeof err.message === 'string' && err.message.trim()
                        ? err.message.trim()
                        : 'Request failed';
                    historySpokenBackfillFailures.set(turnId, { at: Date.now(), error: errMsg, shownAt: null });
                    return null;
                }
            }

            const messageHeader = document.createElement('div');
            messageHeader.style.cssText = 'font-weight: bold; color: #007bff; margin-bottom: 5px; font-size: 0.9em; display: flex; flex-wrap: wrap; align-items: center; gap: 8px;';

            const headerText = document.createElement('span');
            headerText.textContent = `Von • ${displayTimestamp}${historySuffix}`;
            headerText.style.minWidth = '0';
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

            const debugData = turnId ? llmDebugData.get(turnId) : null;
            const hasHistoryLocation = !!debugData?.history_location;
            const shouldShowDebug = !!turnId && (hasLlmDebug || hasHistoryLocation);

            // Add LLM debug button if debug data is available or can be loaded on demand.
            if (shouldShowDebug) {
                const hasPayload = hasLlmDebugPayload(debugData);

                // Compact model badge (visible at-a-glance)
                if (hasPayload && debugData && debugData.model) {
                    const modelBadge = document.createElement('span');
                    modelBadge.className = 'chat-llm-model-badge';
                    modelBadge.textContent = String(debugData.model);
                    modelBadge.title = 'LLM model used for this turn';
                    messageHeader.appendChild(modelBadge);
                }

                const llmDebugButton = document.createElement('button');
                llmDebugButton.className = 'btn-mini llm-debug-button';
                llmDebugButton.textContent = 'LLM ℹ';
                llmDebugButton.title = hasPayload
                    ? 'Show LLM interaction details'
                    : 'Load LLM interaction details';
                llmDebugButton.dataset.turnId = turnId;
                llmDebugButton.addEventListener('click', () => {
                    void showLlmDebugPopup(turnId, { button: llmDebugButton });
                });
                messageHeader.appendChild(llmDebugButton);

                messageHeader.appendChild(copyMarkdownButton);
                copyButtonAppended = true;

                if (hasPayload) {
                    const warnings = deriveLlmDebugWarnings(debugData);
                    const warningIndicator = createChatDebugWarningIndicator(warnings);
                    if (warningIndicator) {
                        messageHeader.appendChild(warningIndicator);
                    }
                }
            }
            if (!copyButtonAppended) {
                messageHeader.appendChild(copyMarkdownButton);
            }

            const rightControls = document.createElement('span');
            rightControls.className = 'chat-message-controls';
            rightControls.style.cssText = 'margin-left: auto; display: inline-flex; align-items: center; gap: 6px; flex: 0 0 auto;';

            const showTtsNotice = (text, options = {}) => {
                const msg = String(text ?? '').trim();
                if (!msg) {
                    return;
                }

                const existing = rightControls.querySelector('.chat-tts-notice');
                if (existing) {
                    try { existing.remove(); } catch (_) { /* ignore */ }
                }

                const notice = document.createElement('span');
                notice.className = 'chat-tts-notice';
                notice.textContent = msg;
                notice.title = msg;
                notice.setAttribute('role', 'status');
                notice.setAttribute('aria-live', 'polite');
                notice.style.cssText = [
                    'display: inline-flex',
                    'align-items: center',
                    'max-width: 320px',
                    'padding: 1px 6px',
                    'border-radius: 10px',
                    'font-size: 0.78em',
                    'line-height: 1.2',
                    'background: #fff3cd',
                    'border: 1px solid #ffeeba',
                    'color: #856404',
                    'white-space: nowrap',
                    'overflow: hidden',
                    'text-overflow: ellipsis'
                ].join(';');

                rightControls.insertBefore(notice, rightControls.firstChild);

                const durationMs = Number.isFinite(options.durationMs) ? options.durationMs : 4500;
                if (durationMs > 0) {
                    setTimeout(() => {
                        try { notice.remove(); } catch (_) { /* ignore */ }
                    }, durationMs);
                }
            };

            // Render-mode badge: shows whether this message is in Rendered/Text mode.
            const renderModeBadge = document.createElement('span');
            renderModeBadge.className = 'chat-render-mode-badge';
            rightControls.appendChild(renderModeBadge);

            // TTS controls (optional).
            const ttsSupported = isTextToSpeechSupported();
            const speakButton = document.createElement('button');
            speakButton.className = 'btn-mini chat-tts-button';
            speakButton.type = 'button';
            speakButton.textContent = 'Speak';
            speakButton.title = 'Speak the talk track aloud (Shift+click to speak the on-screen text)';
            if (!ttsSupported) {
                speakButton.disabled = true;
                speakButton.title = 'Text-to-speech is not supported in this browser.';
            }
            speakButton.addEventListener('click', async (e) => {
                e.preventDefault();
                e.stopPropagation();

                const wantsScreen = !!(e && e.shiftKey);
                const getScreenTextForSpeech = () => {
                    const displayed = (messageText && (messageText.innerText || messageText.textContent))
                        ? (messageText.innerText || messageText.textContent)
                        : screenTextForTurn;
                    return stripMarkdownForSpeech(displayed);
                };

                let desiredText = wantsScreen ? getScreenTextForSpeech() : (spokenTextForTurn || '');
                if (!wantsScreen && !String(desiredText ?? '').trim() && isHistory) {
                    // For legacy history turns, try to backfill a talk track on-demand.
                    const originalLabel = speakButton.textContent;
                    const originalTitle = speakButton.title;
                    const originalDisabled = speakButton.disabled;
                    try {
                        speakButton.disabled = true;
                        speakButton.textContent = 'Generating…';
                        speakButton.title = 'Generating talk track…';

                        const backfilled = await ensureHistorySpokenTalkTrack();
                        if (typeof backfilled === 'string' && backfilled.trim()) {
                            spokenTextForTurn = backfilled;
                            desiredText = backfilled;
                        }
                    } finally {
                        speakButton.disabled = originalDisabled;
                        speakButton.textContent = originalLabel;
                        speakButton.title = originalTitle;
                    }
                }

                // If no talk track is available, derive a plain narration from the screen text.
                // Never speak raw markdown.
                if (!String(desiredText ?? '').trim() && !wantsScreen) {
                    const failure = turnId ? historySpokenBackfillFailures.get(turnId) : null;
                    const reason = failure?.error ? ` (talk track unavailable: ${failure.error})` : '';
                    desiredText = deriveNarrationFromScreenText(screenTextForTurn);
                    speakButton.title = `Speaking a derived narration${reason}. Shift+click speaks the on-screen text.`;

                    if (failure && typeof failure === 'object') {
                        const shouldShow = !failure.shownAt || (typeof failure.at === 'number' && failure.shownAt < failure.at);
                        if (shouldShow) {
                            const errShort = typeof failure.error === 'string' ? failure.error.trim() : '';
                            const msg = errShort
                                ? `Talk track unavailable — speaking on-screen text (${errShort})`
                                : 'Talk track unavailable — speaking on-screen text';
                            showTtsNotice(msg);
                            historySpokenBackfillFailures.set(turnId, { ...failure, shownAt: Date.now() });
                        }
                    }
                }

                if (!String(desiredText ?? '').trim()) {
                    speakButton.title = 'Nothing to speak.';
                    return;
                }

                toggleSpeakTurn(turnId, desiredText, speakButton);
            });
            rightControls.appendChild(speakButton);

            const messageText = document.createElement('div');
            messageText.style.cssText = 'color: #333; white-space: pre-wrap; text-align: left; font-weight: 400; overflow-wrap: anywhere; word-break: break-word;';
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

                // Auto-speak new assistant responses when enabled.
                if (!isHistory && turnId && ttsSupported && isChatTtsEnabled()) {
                    // Auto-speak uses the talk track only (never the screen channel).
                    if (spokenTextForTurn && spokenTextForTurn.trim()) {
                        toggleSpeakTurn(turnId, spokenTextForTurn, speakButton);
                    }
                }
            } catch (e) {
                console.error('[chatTab] Failed to render Von message:', e);
                messageText.textContent = String(message);
            }

            messageHeader.appendChild(rightControls);

            messageContent.appendChild(messageHeader);
            messageContent.appendChild(messageText);
            try {
                const debugData = turnId ? llmDebugData.get(turnId) : null;
                appendQuickReplyButtons(messageContent, debugData?.buttonify?.options);
            } catch (_) {
                // Ignore quick-reply rendering failures.
            }
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
                llmDebugButton.textContent = 'LLM ℹ';
                llmDebugButton.title = 'Show what was sent to LLM before error';
                llmDebugButton.dataset.turnId = turnId;
                llmDebugButton.addEventListener('click', () => {
                    void showLlmDebugPopup(turnId, { button: llmDebugButton });
                });
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

function buildLlmDebugMetadata(debugData) {
    const metadata = {
        model: debugData?.model || 'Unknown',
        message_count: debugData?.messages?.length || 0
    };

    if (debugData?.presenter_channels || debugData?.speech_planning) {
        metadata.speech_planning = debugData.speech_planning || null;
        metadata.presenter_channels = debugData.presenter_channels || null;
    }
    if (debugData?.speech_playback) {
        metadata.speech_playback = debugData.speech_playback;
    }

    // LLM interaction telemetry (JVNAUTOSCI-877)
    const llmInteraction = (debugData && typeof debugData === 'object') ? debugData.llm_interaction : null;
    if (llmInteraction && typeof llmInteraction === 'object') {
        const calls = Array.isArray(llmInteraction.calls) ? llmInteraction.calls : [];

        const callTypeCounts = {};
        const callModels = new Set();
        for (const call of calls) {
            if (!call || typeof call !== 'object') {
                continue;
            }
            const t = typeof call.type === 'string' ? call.type : 'unknown';
            callTypeCounts[t] = (callTypeCounts[t] || 0) + 1;

            if (typeof call.model === 'string' && call.model.trim().length > 0) {
                callModels.add(call.model.trim());
            }
        }

        metadata.llm_interaction = {
            requested_model: llmInteraction.requested_model ?? null,
            orchestrator_used: llmInteraction.orchestrator_used ?? null,
            duration_ms: llmInteraction.duration_ms ?? null,
            server_elapsed_ms: llmInteraction.server_elapsed_ms ?? null,
            usage: llmInteraction.usage ?? null,
            call_count: calls.length,
            call_type_counts: callTypeCounts,
            call_models: Array.from(callModels)
        };
    }

    // Add context statistics if available
    if (debugData?.context_stats) {
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
    if (debugData?.tool_stats) {
        metadata.mcp_tools_used = {
            tool_count: debugData.tool_stats.tool_count,
            total_chars: debugData.tool_stats.total_chars,
            truncated_count: debugData.tool_stats.truncated_count
        };
    }

    // Surface internal MCP execution caps + usage in the visible debug metadata.
    const internalMcp = (debugData && typeof debugData === 'object') ? debugData.internal_mcp : null;
    if (internalMcp && typeof internalMcp === 'object') {
        const caps = internalMcp.execution_caps;
        const progress = internalMcp.tool_use_progress;
        const toolInvocationsCount = Array.isArray(debugData.tool_invocations) ? debugData.tool_invocations.length : 0;

        const maxToolInvocations = (caps && Number.isFinite(caps.max_tool_invocations)) ? caps.max_tool_invocations : null;
        const toolBatchCap = (caps && Number.isFinite(caps.tool_batch_cap)) ? caps.tool_batch_cap : null;

        const usageAgainstCaps = {
            tool_invocations_done: toolInvocationsCount,
            tool_invocations_cap: maxToolInvocations,
            tool_invocations_remaining: (typeof maxToolInvocations === 'number') ? Math.max(0, maxToolInvocations - toolInvocationsCount) : null,
            tool_invocations_exceeded: (typeof maxToolInvocations === 'number') ? toolInvocationsCount > maxToolInvocations : null,
            tool_batch_cap: toolBatchCap,
            estimated_batches: (typeof toolBatchCap === 'number' && toolBatchCap > 0) ? Math.ceil(toolInvocationsCount / toolBatchCap) : null,
            estimated_last_batch_size: (typeof toolBatchCap === 'number' && toolBatchCap > 0)
                ? (toolInvocationsCount === 0 ? 0 : (toolInvocationsCount % toolBatchCap || toolBatchCap))
                : null
        };

        if (caps || progress) {
            metadata.internal_mcp = {
                execution_caps: caps || null,
                usage_against_caps: usageAgainstCaps,
                tool_use_progress: progress || null
            };
        }
    }

    if (debugData?.error !== undefined) {
        metadata.error = debugData.error;
    }

    return metadata;
}

export function __testOnly_buildLlmDebugMetadata(debugData) {
    return buildLlmDebugMetadata(debugData);
}

function hasLlmDebugPayload(debugData) {
    if (!debugData || typeof debugData !== 'object') {
        return false;
    }
    return Boolean(
        debugData.model
        || debugData.response
        || debugData.error !== undefined
        || (Array.isArray(debugData.messages) && debugData.messages.length > 0)
        || (Array.isArray(debugData.tool_invocations) && debugData.tool_invocations.length > 0)
        || (Array.isArray(debugData.aux_llm_calls) && debugData.aux_llm_calls.length > 0)
        || debugData.llm_interaction
        || debugData.context_stats
    );
}

async function loadLlmDebugDataForTurn(turnId, options = {}) {
    const existing = llmDebugData.get(turnId);
    if (hasLlmDebugPayload(existing)) {
        return existing;
    }

    const historyLocation = existing?.history_location;
    if (!historyLocation || !historyLocation.session_id || historyLocation.history_index === undefined || historyLocation.history_index === null) {
        return null;
    }

    if (llmDebugFetchInFlight.has(turnId)) {
        return llmDebugFetchInFlight.get(turnId);
    }

    const button = options.button;
    const originalText = button?.textContent;
    const originalTitle = button?.title;
    if (button) {
        button.disabled = true;
        button.classList.add('loading');
        button.textContent = 'LLM ...';
        button.title = 'Loading LLM interaction details';
    }

    const fetchPromise = (async () => {
        try {
            const params = new URLSearchParams({
                session_id: historyLocation.session_id,
                history_index: String(historyLocation.history_index)
            });
            const response = await fetch(`/von/history/debug?${params.toString()}`, { cache: 'no-store' });
            let data = null;
            try {
                data = await response.json();
            } catch (_) {
                data = null;
            }
            if (!response.ok) {
                if (response.status === 404 && data?.error === 'debug_not_available') {
                    showToast('No LLM debug data stored for this turn.');
                }
                return null;
            }
            if (data?.success === false && data?.error === 'debug_not_available') {
                showToast('No LLM debug data stored for this turn.');
                return null;
            }
            if (!data || typeof data.llm_debug_data !== 'object') {
                return null;
            }
            const merged = {
                ...data.llm_debug_data,
                history_location: historyLocation
            };
            llmDebugData.set(turnId, merged);
            return merged;
        } catch (err) {
            console.warn('[chatTab] Failed to load LLM debug data:', err);
            return null;
        } finally {
            if (button) {
                button.disabled = false;
                button.classList.remove('loading');
                button.textContent = originalText || 'LLM ℹ';
                if (originalTitle) {
                    button.title = originalTitle;
                } else {
                    button.title = 'Load LLM interaction details';
                }
            }
        }
    })();

    llmDebugFetchInFlight.set(turnId, fetchPromise);
    fetchPromise.finally(() => {
        llmDebugFetchInFlight.delete(turnId);
    });
    return fetchPromise;
}

// Show LLM debug popup for a specific turn
async function showLlmDebugPopup(turnId, options = {}) {
    let debugDataRaw = llmDebugData.get(turnId);
    if (!hasLlmDebugPayload(debugDataRaw)) {
        debugDataRaw = await loadLlmDebugDataForTurn(turnId, options);
    }
    const debugData = enrichDebugDataWithSpeechPlanning(debugDataRaw, { turnId });
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
    const metadata = buildLlmDebugMetadata(debugData);
    const hasError = debugData.error !== undefined;

    let workflowExecutionTrace = null;
    let workflowStages = [];
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

        workflowStages = debugData.aux_llm_calls.filter((entry) => {
            if (!entry || typeof entry !== 'object') {
                return false;
            }
            return entry.type === 'workflow_stage';
        });
    }

    // Build metadata HTML display
    let metadataHtml = '';
    const promptGroups = extractPromptConceptGroups(debugData);
    metadataHtml += buildPromptConceptLinksHtml(promptGroups);
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

    if (workflowStages.length > 0) {
        const stageSummary = workflowStages.map((entry) => ({
            stage: entry.stage ?? null,
            source: entry.source ?? null,
            reason: entry.reason ?? null,
            format: entry.format ?? null
        }));

        metadataHtml += '<div class="llm-debug-metadata-section">';
        metadataHtml += '<strong>Workflow stages</strong>';
        metadataHtml += '<pre>';
        metadataHtml += escapeHtml(JSON.stringify(stageSummary, null, 2));
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

    if (!metaDiv.dataset.promptLinkBound) {
        metaDiv.addEventListener('click', (event) => {
            const target = event.target;
            if (!target || !(target instanceof HTMLElement)) {
                return;
            }
            const button = target.closest('.llm-debug-prompt-link');
            if (!button) {
                return;
            }
            event.preventDefault();
            const conceptId = button.dataset.conceptId;
            if (!conceptId) {
                return;
            }
            document.dispatchEvent(new CustomEvent('von:selectConceptById', {
                detail: {
                    conceptId,
                    createConceptTab: true,
                    modifierKeys: {
                        shiftKey: event.shiftKey
                    }
                }
            }));
        });
        metaDiv.dataset.promptLinkBound = 'true';
    }

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
            format_version: '1.1'
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
        const enrichedDebugData = enrichDebugDataWithSpeechPlanning(debugData, { turnId });
        conversationData.turns.push({
            turn_id: turnId,
            timestamp: new Date((debugData && debugData.timestamp) || Date.now()).toISOString(),
            debug_data: enrichedDebugData
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
// Export for testing
export const __test_only__rehydrateHistory = rehydrateHistory;
export const __test_only__renderChatSessionMetadataPanel = _renderChatSessionMetadataPanel;

// Export for testing.
export function __testOnly_resetChatTtsState() {
    try {
        stopSpeaking();
    } catch (_) {
        // Ignore.
    }
    try {
        clearActiveTtsUi();
    } catch (_) {
        // Ignore.
    }
    try {
        activeTtsPlaybackState = null;
    } catch (_) {
        // Ignore.
    }
    try {
        historySpokenBackfillFailures.clear();
    } catch (_) {
        // Ignore.
    }
}
export { formatChatTimestamp, showLlmDebugPopup, updateHistoryLength };




