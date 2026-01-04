// Chat Tab Module
import { annotateTurn, getUserContext } from './apiService.js';
import { initializeConceptAutocomplete } from './components/conceptAutocomplete.js';
import { initializePromptCartoucheOverlay, normaliseVontologyIdsForBackend } from './components/promptCartoucheOverlay.js';
import { elements, renderSpanSuggestions } from './domUtils.js';
import { detectMarkdown, renderMarkdownViaServer } from './markdownUtils.js';
import {
    getSpeechSynthesisVoices,
    isSpeechRecognitionSupported,
    isTextToSpeechSupported,
    speakText,
    startSpeechRecognition,
    stopSpeaking
} from './speech.js';
import { selectBestNameForContext } from './utils/nameSelection.js';
import { cartouchifyElementText, cartouchifyVontologyTokensInElement } from './utils/textDecorator.js';

// Store LLM debug data for each turn
const llmDebugData = new Map();
// Track conversation turns for Markdown export and state resets
const transcriptTurns = [];
let historySegmentsShown = 1;
let totalHistorySegments = 1;

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
    convertReplyOptionsListsToButtons(container);
    try {
        container.dataset.renderMode = 'rendered';
    } catch (_) {
        // Ignore.
    }

    // Preserve clickable #V# tokens, but never inside code blocks.
    cartouchifyVontologyTokensInElement(container, { skipSelectors: ['pre', 'code', 'a'], allowStandaloneCodeTokens: true, allowStandaloneCodeBlockTokens: true });
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
        cartouchifyVontologyTokensInElement(messageTextEl, { skipSelectors: ['pre', 'code', 'a'], allowStandaloneCodeTokens: true, allowStandaloneCodeBlockTokens: true });
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

        if (chatConceptMetaPending.has(fullId)) {
            scheduleChatConceptMetaRetry(fullId);
            return;
        }

        const p = fetchConceptMetaForChatNodeOnly(fullId).then((meta) => {
            chatConceptMetaPending.delete(fullId);

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

        chatConceptMetaPending.set(fullId, p);
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

const CHAT_STT_LANGUAGE_STORAGE_KEY = 'chatSttLanguage';
const CHAT_STT_CONTINUOUS_STORAGE_KEY = 'chatSttContinuous';
const CHAT_STT_INTERIM_RESULTS_STORAGE_KEY = 'chatSttInterimResults';

let activeDictation = null;
let dictationState = null;

let activeTtsTurnId = null;
let activeTtsButton = null;

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

    const finish = () => {
        if (activeTtsTurnId === turnId) {
            clearActiveTtsUi();
        }
    };

    try {
        utterance.onend = finish;
        utterance.onerror = finish;
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
                    const chatsText = (sessionCount === null) ? '— chats' : `${sessionCount} chats`;
                    historyLengthElement.textContent = `History: ${chatsText} | this ${contextCount}`;
                    historyLengthElement.title = `Chat history: ${sessionCount ?? '—'} chats • total ${historyLength} messages • this session ${contextCount} messages`;
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
                                titleEl.textContent = 'Chat history';
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
                            let chats = null;
                            try {
                                const lenRes = await fetch('/von/history/length', { cache: 'no-store' });
                                if (lenRes.ok) {
                                    const lenJs = await lenRes.json();
                                    totalMessages = (typeof lenJs?.history_length === 'number') ? lenJs.history_length : null;
                                    chats = (typeof lenJs?.session_count === 'number') ? lenJs.session_count : null;
                                }
                            } catch (_) { /* ignore */ }

                            const res = await fetch('/von/history/sessions?limit=50', { cache: 'no-store' });
                            const js = await res.json();
                            if (!res.ok || js?.authenticated === false) {
                                body.innerHTML = '<p>History unavailable (not logged in).</p>';
                                return;
                            }

                            const sessions = Array.isArray(js?.sessions) ? js.sessions : [];
                            if (sessions.length === 0) {
                                body.innerHTML = '<p>No saved sessions.</p>';
                                return;
                            }

                            const currentCount = transcriptTurns.length;
                            if (titleEl) {
                                const chatsText2 = (chats === null) ? '— chats' : `${chats} chats`;
                                const totalText2 = (totalMessages === null) ? '— total' : `${totalMessages} total`;
                                titleEl.textContent = `Chat history — ${chatsText2} • ${totalText2} • this ${currentCount}`;
                            }

                            const rows = sessions.map((s) => {
                                const sidRaw = s?.session_id ? String(s.session_id) : '(unknown session)';
                                const sidShort = (sidRaw.length > 10) ? `${sidRaw.slice(0, 8)}…` : sidRaw;
                                const count = (typeof s?.message_count === 'number') ? s.message_count : 0;

                                const lastAt = (typeof s?.last_message_at === 'string') ? s.last_message_at : null;
                                const lastAtShort = lastAt ? lastAt.replace('T', ' ').replace('Z', '') : '—';
                                const preview = (typeof s?.preview === 'string' && s.preview.trim()) ? s.preview.trim() : '—';

                                const ns = (typeof s?.namespace === 'string' && s.namespace.trim()) ? s.namespace.trim() : null;
                                const nsShort = ns ? (ns.length > 48 ? `${ns.slice(0, 46)}…` : ns) : null;

                                const sessionAttr = escapeHtml(sidRaw);

                                return [
                                    `<li class="history-session-row" role="button" tabindex="0" data-session-id="${sessionAttr}">`,
                                    '<div class="history-session-content">',
                                    `<strong class="history-session-id" title="${escapeHtml(sidRaw)}">${escapeHtml(sidShort)}</strong>`,
                                    `<span class="history-session-meta">${count} msgs • last ${escapeHtml(lastAtShort)}${nsShort ? ` • ns ${escapeHtml(nsShort)}` : ''}</span>`,
                                    `<span class="history-session-preview" title="${escapeHtml(preview)}">${escapeHtml(preview)}</span>`,
                                    '</div>',
                                    '</li>'
                                ].join('');
                            });

                            const summaryLine = `Showing ${sessions.length} most recent sessions (sorted by last message time)`;

                            body.innerHTML = [
                                `<p class="history-session-summary">${escapeHtml(summaryLine)}</p>`,
                                '<div class="history-session-scroll">',
                                '<ul class="history-session-list">',
                                ...rows,
                                '</ul>',
                                '</div>'
                            ].join('');

                            const switchToSession = async (sessionId) => {
                                const sid = String(sessionId || '').trim();
                                if (!sid) {
                                    return;
                                }

                                abortActiveChatRequest();

                                const scrollableField2 = document.getElementById('scrollableField');
                                if (!scrollableField2) {
                                    return;
                                }

                                try {
                                    body.innerHTML = '<p>Switching session…</p>';

                                    const setRes = await fetch('/von/api/session/set_chat_session', {
                                        method: 'POST',
                                        headers: { 'Content-Type': 'application/json' },
                                        body: JSON.stringify({ session_id: sid })
                                    });
                                    const setJs = await setRes.json();
                                    if (!setRes.ok) {
                                        const msg = setJs?.error ? String(setJs.error) : 'Unable to switch session.';
                                        body.innerHTML = `<p>${escapeHtml(msg)}</p>`;
                                        return;
                                    }

                                    const history = Array.isArray(setJs?.history) ? setJs.history : [];

                                    historySegmentsShown = 1;
                                    totalHistorySegments = 1;
                                    updateHistoryBanner();

                                    rehydrateHistory(scrollableField2, history, {
                                        scrollToBottom: true,
                                        preserveScroll: false,
                                        showResetNotice: false,
                                        forceScrollToBottom: true
                                    });

                                    modal.classList.remove('open');
                                    modal.setAttribute('aria-hidden', 'true');

                                    // Trigger immediate health poll to update RAG status with new session context.
                                    document.dispatchEvent(new CustomEvent('von:contextReset', {
                                        detail: { trigger: 'history_session_switch' }
                                    }));

                                    const promptInput = document.getElementById('promptInput');
                                    if (promptInput) {
                                        promptInput.focus();
                                    }
                                } catch (err) {
                                    console.error('Error switching chat session:', err);
                                    body.innerHTML = '<p>Unable to switch session.</p>';
                                }
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
        showResetNotice = false,
        forceScrollToBottom = false
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
                showResetNotice,
                forceScrollToBottom
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
            const hasDebugData = msg.role === 'assistant' && !!msg.llm_debug_data;
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
    const debugDataRaw = llmDebugData.get(turnId);
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
    const hasError = debugData.error !== undefined;

    // Build metadata object (not HTML) so it's included in JSON structure
    const metadata = {
        model: debugData.model || 'Unknown',
        message_count: debugData.messages?.length || 0
    };

    if (debugData.presenter_channels || debugData.speech_planning) {
        metadata.speech_planning = debugData.speech_planning || null;
        metadata.presenter_channels = debugData.presenter_channels || null;
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
        historySpokenBackfillFailures.clear();
    } catch (_) {
        // Ignore.
    }
}
export { formatChatTimestamp, showLlmDebugPopup, updateHistoryLength };

