// Prompt cartouche overlay
//
// Implements an in-place “rich textarea” overlay for Vontology concept IDs.
// When a concept is inserted from autocomplete, we insert a non-trigger form
// (#V\u200B#...) into the textarea so autocomplete does not re-trigger.
// The overlay renders those tokens as cartouches and supports removal.

import { getPreferredLanguage, selectBestNameForContext } from '../utils/nameSelection.js';
import { createVontologyCartouche } from '../utils/textDecorator.js';

const ZWSP = '\u200B';

// Use the same search implementation as the global search UI to avoid drift.
const SEARCH_API = '/vontology/api/vontology/search';

// Match #V\u200B#<id> and #v\u200B#<id>
// Mirrors the allowed id character set used elsewhere.
// NOTE: Parentheses NOT allowed in concept IDs (they use underscores)
const NON_TRIGGER_TOKEN_RE = /#([Vv])\u200B#([A-Za-z0-9_\./:–\-]+?)(?=[\s"'`]|$)/g;

// For normalising before send.
const NON_TRIGGER_PREFIX_RE = /#([Vv])\u200B#/g;

// Cache concept metadata used for prompt cartouches.
// Map<fullId, { name: string, kind: string } | null>
const promptConceptMetaCache = new Map();
// Map<fullId, Promise<meta|null>>
const promptConceptMetaPending = new Map();

function computeEffectiveOverlayScrollTop(textarea, overlayInner) {
    if (!textarea || !overlayInner) {
        return 0;
    }

    const scrollTop = Math.max(0, Number(textarea.scrollTop) || 0);
    const clientHeight = Math.max(0, Number(textarea.clientHeight) || 0);
    const textareaScrollHeight = Math.max(0, Number(textarea.scrollHeight) || 0);
    const overlayScrollHeight = Math.max(0, Number(overlayInner.scrollHeight) || 0);

    const textareaMaxScroll = Math.max(0, textareaScrollHeight - clientHeight);
    const overlayMaxScroll = Math.max(0, overlayScrollHeight - clientHeight);

    // If the overlay isn't taller than the textarea scroll range, use the native scrollTop.
    if (overlayMaxScroll <= textareaMaxScroll + 0.5) {
        return scrollTop;
    }

    // If the textarea can't scroll at all, we have no input range to map.
    if (textareaMaxScroll <= 0.5) {
        return 0;
    }

    const ratio = Math.max(0, Math.min(1, scrollTop / textareaMaxScroll));
    return ratio * overlayMaxScroll;
}

export function makeNonTriggerVontologyId(fullId) {
    const raw = String(fullId ?? '');
    return raw.replace(/^#([Vv])#/, (_, v) => `#${v}${ZWSP}#`);
}

export function normaliseVontologyIdsForBackend(text) {
    const input = String(text ?? '');
    // Normalise both #V\u200B# and #v\u200B# to canonical #V#.
    return input.replace(NON_TRIGGER_PREFIX_RE, '#V#');
}

function ensureOverlay(textarea) {
    if (!textarea || !textarea.parentNode) {
        return null;
    }

    const parent = textarea.parentElement;
    if (parent && parent.classList.contains('prompt-input-wrapper')) {
        const overlay = parent.querySelector('.prompt-input-overlay');
        const inner = parent.querySelector('.prompt-input-overlay-inner');
        if (overlay && inner) {
            let content = inner.querySelector('.prompt-input-overlay-content');
            if (!content) {
                content = document.createElement('div');
                content.className = 'prompt-input-overlay-content';
                inner.appendChild(content);
            }

            let mirror = overlay.querySelector('.prompt-input-overlay-mirror');
            if (!mirror) {
                mirror = document.createElement('div');
                mirror.className = 'prompt-input-overlay-mirror';
                overlay.appendChild(mirror);
            }
            let mirrorContent = mirror.querySelector('.prompt-input-overlay-mirror-content');
            if (!mirrorContent) {
                mirrorContent = document.createElement('div');
                mirrorContent.className = 'prompt-input-overlay-mirror-content';
                mirror.appendChild(mirrorContent);
            }

            let caret = overlay.querySelector('.prompt-input-overlay-caret');
            if (!caret) {
                caret = document.createElement('div');
                caret.className = 'prompt-input-overlay-caret';
                caret.setAttribute('aria-hidden', 'true');
                overlay.appendChild(caret);
            }

            return { wrapper: parent, overlay, inner, content, mirror, mirrorContent, caret };
        }
    }

    const wrapper = document.createElement('div');
    wrapper.className = 'prompt-input-wrapper';

    const overlay = document.createElement('div');
    overlay.className = 'prompt-input-overlay';

    const inner = document.createElement('div');
    inner.className = 'prompt-input-overlay-inner';

    const content = document.createElement('div');
    content.className = 'prompt-input-overlay-content';
    inner.appendChild(content);

    overlay.appendChild(inner);

    const mirror = document.createElement('div');
    mirror.className = 'prompt-input-overlay-mirror';
    const mirrorContent = document.createElement('div');
    mirrorContent.className = 'prompt-input-overlay-mirror-content';
    mirror.appendChild(mirrorContent);
    overlay.appendChild(mirror);

    const caret = document.createElement('div');
    caret.className = 'prompt-input-overlay-caret';
    caret.setAttribute('aria-hidden', 'true');
    overlay.appendChild(caret);

    // Wrap the textarea in-place.
    const insertBeforeTarget = textarea;
    insertBeforeTarget.parentNode.insertBefore(wrapper, insertBeforeTarget);
    wrapper.appendChild(textarea);
    wrapper.appendChild(overlay);

    return { wrapper, overlay, inner, content, mirror, mirrorContent, caret };
}

function syncOverlayStyles(textarea, overlay, inner, mirror) {
    try {
        const cs = getComputedStyle(textarea);
        overlay.style.borderRadius = cs.borderRadius;
        inner.style.padding = cs.padding;
        inner.style.fontFamily = cs.fontFamily;
        inner.style.fontSize = cs.fontSize;
        inner.style.fontWeight = cs.fontWeight;
        inner.style.lineHeight = cs.lineHeight;
        inner.style.letterSpacing = cs.letterSpacing;
        // The textarea text is intentionally made transparent (we draw text in the overlay).
        // Always inherit the overlay colour rather than the textarea caret colour (which may be
        // transparent when using an overlay-rendered caret).
        inner.style.removeProperty('color');
        inner.style.textAlign = cs.textAlign;

        if (mirror) {
            mirror.style.borderRadius = cs.borderRadius;
            mirror.style.padding = cs.padding;
            mirror.style.fontFamily = cs.fontFamily;
            mirror.style.fontSize = cs.fontSize;
            mirror.style.fontWeight = cs.fontWeight;
            mirror.style.lineHeight = cs.lineHeight;
            mirror.style.letterSpacing = cs.letterSpacing;
            mirror.style.textAlign = cs.textAlign;
        }
    } catch (_) {
        // Best-effort only.
    }
}

function clearChildren(node) {
    while (node && node.firstChild) {
        node.removeChild(node.firstChild);
    }
}

function extractNonTriggerTokensWithPositions(text) {
    const input = String(text ?? '');
    const matches = [];
    let match;
    NON_TRIGGER_TOKEN_RE.lastIndex = 0;
    while ((match = NON_TRIGGER_TOKEN_RE.exec(input)) !== null) {
        matches.push({
            start: match.index,
            end: NON_TRIGGER_TOKEN_RE.lastIndex,
            prefixChar: match[1],
            conceptId: match[2],
            rawText: match[0]
        });
    }
    return matches;
}

function formatKindLabel(kind) {
    const k = String(kind ?? '').toLowerCase();
    if (k === 'predicate') return 'Predicate';
    if (k === 'individual') return 'Individual';
    return 'Type';
}

function normaliseKindClass(kind) {
    const k = String(kind ?? '').toLowerCase();
    if (k === 'predicate' || k === 'individual' || k === 'type') {
        return k;
    }
    return 'type';
}

function createPromptCartouche(fullId, start, end) {
    const btn = createVontologyCartouche(fullId);
    btn.classList.add('prompt-vontology-cartouche');
    btn.dataset.start = String(start);
    btn.dataset.end = String(end);

    const remove = document.createElement('span');
    remove.className = 'prompt-vontology-cartouche-remove';
    remove.textContent = '×';
    remove.setAttribute('role', 'button');
    remove.setAttribute('aria-label', `Remove concept ${fullId}`);
    remove.tabIndex = -1;
    btn.appendChild(remove);

    return btn;
}

function renderOverlay(textarea, content) {
    const value = String(textarea?.value ?? '');
    clearChildren(content);

    if (!value) {
        // Let the native placeholder do its job.
        return;
    }

    const tokens = extractNonTriggerTokensWithPositions(value);
    if (tokens.length === 0) {
        content.appendChild(document.createTextNode(value));
        return;
    }

    let lastIndex = 0;
    for (const token of tokens) {
        if (token.start > lastIndex) {
            content.appendChild(document.createTextNode(value.slice(lastIndex, token.start)));
        }
        const fullId = `#V#${token.conceptId}`;
        content.appendChild(createPromptCartouche(fullId, token.start, token.end));
        lastIndex = token.end;
    }
    if (lastIndex < value.length) {
        content.appendChild(document.createTextNode(value.slice(lastIndex)));
    }
}

function normaliseCaretPosition(text, pos) {
    const input = String(text ?? '');
    const caretPos = Math.max(0, Math.min(Number(pos) || 0, input.length));
    const token = findTokenAtPosition(input, caretPos);
    if (!token) {
        return caretPos;
    }
    // If the caret lands inside a token, align it to the nearest edge.
    const distToStart = Math.abs(caretPos - token.start);
    const distToEnd = Math.abs(caretPos - token.end);
    return distToEnd <= distToStart ? token.end : token.start;
}

function ensureCaretNotInsideToken(textarea) {
    if (!textarea) {
        return false;
    }
    const selStart = typeof textarea.selectionStart === 'number' ? textarea.selectionStart : null;
    const selEnd = typeof textarea.selectionEnd === 'number' ? textarea.selectionEnd : null;
    if (selStart == null || selEnd == null || selStart !== selEnd) {
        return false;
    }

    const value = String(textarea.value ?? '');
    const normalised = normaliseCaretPosition(value, selStart);
    if (normalised === selStart) {
        return false;
    }
    try {
        textarea.setSelectionRange(normalised, normalised);
        return true;
    } catch (_) {
        return false;
    }
}

function renderMirrorWithCaretMarker(textarea, mirrorContent, caretPos) {
    const value = String(textarea?.value ?? '');
    const caret = normaliseCaretPosition(value, caretPos);
    clearChildren(mirrorContent);

    const marker = document.createElement('span');
    marker.className = 'prompt-input-caret-marker';
    marker.textContent = ZWSP;

    const tokens = extractNonTriggerTokensWithPositions(value);
    if (tokens.length === 0) {
        const before = value.slice(0, caret);
        const after = value.slice(caret);
        if (before) mirrorContent.appendChild(document.createTextNode(before));
        mirrorContent.appendChild(marker);
        if (after) mirrorContent.appendChild(document.createTextNode(after));
        return marker;
    }

    let lastIndex = 0;
    let markerInserted = false;

    const maybeInsertMarker = (absoluteIndex) => {
        if (markerInserted) return;
        if (caret === absoluteIndex) {
            mirrorContent.appendChild(marker);
            markerInserted = true;
        }
    };

    maybeInsertMarker(0);
    for (const token of tokens) {
        if (token.start > lastIndex) {
            const segment = value.slice(lastIndex, token.start);
            const segStart = lastIndex;
            const segEnd = token.start;
            const caretInSeg = caret > segStart && caret < segEnd;
            if (caretInSeg) {
                const before = value.slice(segStart, caret);
                const after = value.slice(caret, segEnd);
                if (before) mirrorContent.appendChild(document.createTextNode(before));
                mirrorContent.appendChild(marker);
                markerInserted = true;
                if (after) mirrorContent.appendChild(document.createTextNode(after));
            } else {
                maybeInsertMarker(segStart);
                mirrorContent.appendChild(document.createTextNode(segment));
                maybeInsertMarker(segEnd);
            }
        } else {
            maybeInsertMarker(token.start);
        }

        if (!markerInserted) {
            // If caret is within a token, normaliseCaretPosition() moves it to an edge.
            maybeInsertMarker(token.start);
        }

        const fullId = `#V#${token.conceptId}`;
        const cartouche = createPromptCartouche(fullId, token.start, token.end);
        const meta = promptConceptMetaCache.get(fullId);
        if (meta) {
            updatePromptCartouche(cartouche, meta);
        }
        mirrorContent.appendChild(cartouche);
        maybeInsertMarker(token.end);
        lastIndex = token.end;
    }

    if (lastIndex < value.length) {
        const segStart = lastIndex;
        const segEnd = value.length;
        const caretInSeg = caret > segStart && caret < segEnd;
        if (caretInSeg) {
            const before = value.slice(segStart, caret);
            const after = value.slice(caret);
            if (before) mirrorContent.appendChild(document.createTextNode(before));
            mirrorContent.appendChild(marker);
            markerInserted = true;
            if (after) mirrorContent.appendChild(document.createTextNode(after));
        } else {
            maybeInsertMarker(segStart);
            mirrorContent.appendChild(document.createTextNode(value.slice(lastIndex)));
            maybeInsertMarker(segEnd);
        }
    }

    if (!markerInserted) {
        mirrorContent.appendChild(marker);
    }

    return marker;
}

function removeTokenRangeFromTextarea(textarea, start, end) {
    if (!textarea) {
        return;
    }
    const value = String(textarea.value ?? '');
    let removeStart = Math.max(0, Math.min(start, value.length));
    let removeEnd = Math.max(0, Math.min(end, value.length));
    if (removeEnd < removeStart) {
        [removeStart, removeEnd] = [removeEnd, removeStart];
    }

    // Trim a single adjacent space for nicer ergonomics.
    if (value[removeEnd] === ' ') {
        removeEnd += 1;
    } else if (removeStart > 0 && value[removeStart - 1] === ' ') {
        removeStart -= 1;
    }

    textarea.value = value.slice(0, removeStart) + value.slice(removeEnd);
    try {
        textarea.setSelectionRange(removeStart, removeStart);
    } catch (_) {
        // Best-effort.
    }
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    try {
        textarea.focus();
    } catch (_) {
        // Ignore.
    }
}

function findTokenAtPosition(text, pos) {
    const tokens = extractNonTriggerTokensWithPositions(text);
    for (const token of tokens) {
        if (pos >= token.start && pos <= token.end) {
            return token;
        }
    }
    return null;
}

async function fetchConceptMeta(fullId) {
    if (promptConceptMetaCache.has(fullId)) {
        return promptConceptMetaCache.get(fullId);
    }
    if (promptConceptMetaPending.has(fullId)) {
        return promptConceptMetaPending.get(fullId);
    }

    const promise = (async () => {
        try {
            // Prefer exact node lookup for accuracy.
            const nodeUrl = `/vontology/api/vontology/node_content?identifier=${encodeURIComponent(fullId)}`;
            const nodeResp = await fetch(nodeUrl, { cache: 'no-store' });
            if (nodeResp.ok) {
                const node = await nodeResp.json();
                const preferredLanguage = getPreferredLanguage();
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
                const computedKind = node?.computed_kind || node?.node?.computed_kind || null;
                let kind = node?.kind || node?.node?.kind || computedKind || 'type';
                if (computedKind && (kind === 'type' || !kind)) {
                    kind = computedKind;
                }
                const meta = { name: String(name), kind: String(kind) };
                promptConceptMetaCache.set(fullId, meta);
                return meta;
            }

            // Fallback to search endpoint if node_content is unavailable.
            const resp = await fetch(`${SEARCH_API}?q=${encodeURIComponent(fullId)}&limit=8`);
            if (!resp.ok) {
                promptConceptMetaCache.set(fullId, null);
                return null;
            }
            const data = await resp.json();
            const results = Array.isArray(data?.results) ? data.results : [];
            const match = results.find(r => String(r?.id) === fullId);
            if (!match) {
                promptConceptMetaCache.set(fullId, null);
                return null;
            }
            const meta = { name: String(match.name ?? ''), kind: String(match.kind ?? '') };
            promptConceptMetaCache.set(fullId, meta);
            return meta;
        } catch (_) {
            promptConceptMetaCache.set(fullId, null);
            return null;
        } finally {
            promptConceptMetaPending.delete(fullId);
        }
    })();

    promptConceptMetaPending.set(fullId, promise);
    return promise;
}

function updatePromptCartouche(cartoucheEl, meta) {
    if (!cartoucheEl || !meta) {
        return;
    }
    const nameEl = cartoucheEl.querySelector('.vontology-cartouche-name');
    if (nameEl && meta.name) {
        nameEl.textContent = meta.name;
    }
    const kindEl = cartoucheEl.querySelector('.vontology-cartouche-kind');
    if (kindEl) {
        const kindClass = normaliseKindClass(meta.kind);
        kindEl.className = `vontology-cartouche-kind ${kindClass}`;
        kindEl.textContent = meta.kind ? formatKindLabel(meta.kind) : 'Type';
        // Also apply kind class to the button itself for consistent styling
        cartoucheEl.className = cartoucheEl.className.replace(/\b(type|individual|predicate)\b/g, '');
        if (kindClass && kindClass !== 'type') {
            cartoucheEl.classList.add(kindClass);
        }
    }
}

function hydratePromptCartouches(root, onUpdated) {
    if (!root) {
        return;
    }
    const elements = Array.from(root.querySelectorAll('button.prompt-vontology-cartouche[data-full-concept-id]'));
    const unique = new Set(elements.map(el => el.dataset.fullConceptId).filter(Boolean));
    for (const fullId of unique) {
        fetchConceptMeta(fullId).then(meta => {
            if (!meta) return;
            for (const el of elements) {
                if (el.dataset.fullConceptId === fullId) {
                    updatePromptCartouche(el, meta);
                }
            }

            if (typeof onUpdated === 'function') {
                try {
                    onUpdated();
                } catch (_) {
                    // Ignore.
                }
            }
        });
    }
}

export function initializePromptCartoucheOverlay(textarea) {
    const parts = ensureOverlay(textarea);
    if (!parts) {
        return;
    }

    parts.wrapper.dataset.initialised = '1';
    parts.wrapper.classList.add('has-overlay-caret');
    syncOverlayStyles(textarea, parts.overlay, parts.inner, parts.mirror);

    let scheduledId = null;
    let scheduledVia = null;

    let scrollSyncTimer = null;

    const schedule = (fn) => {
        if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
            scheduledVia = 'raf';
            scheduledId = window.requestAnimationFrame(fn);
        } else {
            scheduledVia = 'timeout';
            scheduledId = window.setTimeout(fn, 0);
        }
    };

    const cancelSchedule = () => {
        if (scheduledId == null) {
            return;
        }
        if (scheduledVia === 'raf' && typeof window.cancelAnimationFrame === 'function') {
            window.cancelAnimationFrame(scheduledId);
        } else {
            window.clearTimeout(scheduledId);
        }
        scheduledId = null;
        scheduledVia = null;
    };

    const syncScrollTransforms = () => {
        const effectiveScrollTop = computeEffectiveOverlayScrollTop(textarea, parts.inner);
        try {
            parts.inner.style.transform = `translateY(-${effectiveScrollTop}px)`;
        } catch (_) {
            // Ignore.
        }
    };

    const scheduleScrollSync = () => {
        if (scrollSyncTimer != null) {
            return;
        }

        // Use setTimeout so Jest fake timers can deterministically flush it.
        scrollSyncTimer = window.setTimeout(() => {
            scrollSyncTimer = null;
            syncScrollTransforms();
        }, 0);
    };

    const updateOverlayCaret = () => {
        cancelSchedule();

        if (!parts.caret || !parts.mirrorContent) {
            return;
        }

        const hasFocus = typeof document !== 'undefined' && document.activeElement === textarea;
        const selStart = typeof textarea.selectionStart === 'number' ? textarea.selectionStart : null;
        const selEnd = typeof textarea.selectionEnd === 'number' ? textarea.selectionEnd : null;

        if (!hasFocus || selStart == null || selEnd == null || selStart !== selEnd) {
            parts.caret.style.display = 'none';
            return;
        }

        // Cartouche hydration can change wrapping/height without changing textarea.scrollTop.
        // Keep the overlay scroll translation in sync before measuring caret position.
        syncScrollTransforms();

        // If the real textarea caret ended up inside a token (common after a click, because the
        // hidden token text length differs from the rendered cartouche width), snap it out before
        // measuring and before the next keystroke mutates the token.
        if (ensureCaretNotInsideToken(textarea)) {
            // Selection changed; rerun measurement on the next frame.
            scheduleCaretUpdate();
            return;
        }

        parts.caret.style.display = '';

        // Mirror the scroll translation so measurements match visible overlay.
        try {
            const effectiveScrollTop = computeEffectiveOverlayScrollTop(textarea, parts.inner);
            parts.mirror.style.transform = `translateY(-${effectiveScrollTop}px)`;
        } catch (_) {
            // Ignore.
        }

        const marker = renderMirrorWithCaretMarker(textarea, parts.mirrorContent, selStart);
        try {
            const overlayRect = parts.overlay.getBoundingClientRect();
            const markerRect = marker.getBoundingClientRect();
            const left = Math.max(0, markerRect.left - overlayRect.left);
            const top = Math.max(0, markerRect.top - overlayRect.top);
            const height = Math.max(12, markerRect.height || 0);
            parts.caret.style.left = `${left}px`;
            parts.caret.style.top = `${top}px`;
            parts.caret.style.height = `${height}px`;
        } catch (_) {
            // Best-effort.
        }
    };

    const scheduleCaretUpdate = () => {
        cancelSchedule();
        schedule(updateOverlayCaret);
    };

    // Keep overlay updated.
    const rerender = () => {
        renderOverlay(textarea, parts.content);
        hydratePromptCartouches(parts.content, () => {
            scheduleScrollSync();
            scheduleCaretUpdate();
        });
        scheduleScrollSync();
        scheduleCaretUpdate();
    };

    textarea.addEventListener('input', rerender);
    textarea.addEventListener('click', scheduleCaretUpdate);
    textarea.addEventListener('keyup', scheduleCaretUpdate);
    textarea.addEventListener('mouseup', scheduleCaretUpdate);
    textarea.addEventListener('select', scheduleCaretUpdate);
    textarea.addEventListener('focus', scheduleCaretUpdate);
    textarea.addEventListener('blur', scheduleCaretUpdate);
    textarea.addEventListener('scroll', () => {
        syncScrollTransforms();
        scheduleCaretUpdate();
    });
    window.addEventListener('resize', () => {
        syncOverlayStyles(textarea, parts.overlay, parts.inner, parts.mirror);
        scheduleScrollSync();
        scheduleCaretUpdate();
    });

    // If the caret lands inside a cartouche token, treat backspace/delete as removing the whole token.
    textarea.addEventListener('keydown', (event) => {
        const key = event.key;

        // Prevent token mutation for normal typing as well.
        // If the caret is inside a token, snap it to the nearest edge before the character inserts.
        if (key && key.length === 1) {
            ensureCaretNotInsideToken(textarea);
            return;
        }

        if (key !== 'Backspace' && key !== 'Delete') {
            return;
        }
        if (typeof textarea.selectionStart !== 'number' || typeof textarea.selectionEnd !== 'number') {
            return;
        }
        if (textarea.selectionStart !== textarea.selectionEnd) {
            return;
        }
        const pos = textarea.selectionStart;
        const token = findTokenAtPosition(textarea.value, pos);
        if (!token) {
            return;
        }
        event.preventDefault();
        removeTokenRangeFromTextarea(textarea, token.start, token.end);
    });

    // Handle remove click and keyboard removal on the cartouche itself.
    parts.overlay.addEventListener('click', (event) => {
        const target = event.target;
        if (!(target instanceof HTMLElement)) {
            return;
        }
        const removeEl = target.closest('.prompt-vontology-cartouche-remove');
        if (removeEl) {
            event.preventDefault();
            event.stopPropagation();
            const btn = removeEl.closest('button.prompt-vontology-cartouche');
            if (!btn) return;
            const start = Number(btn.dataset.start);
            const end = Number(btn.dataset.end);
            if (!Number.isFinite(start) || !Number.isFinite(end)) return;
            removeTokenRangeFromTextarea(textarea, start, end);
            scheduleCaretUpdate();
        }
    }, true);

    parts.overlay.addEventListener('keydown', (event) => {
        const key = event.key;
        if (key !== 'Backspace' && key !== 'Delete') {
            return;
        }
        const target = event.target;
        if (!(target instanceof HTMLElement)) {
            return;
        }
        const btn = target.closest('button.prompt-vontology-cartouche');
        if (!btn) {
            return;
        }
        const start = Number(btn.dataset.start);
        const end = Number(btn.dataset.end);
        if (!Number.isFinite(start) || !Number.isFinite(end)) return;
        event.preventDefault();
        removeTokenRangeFromTextarea(textarea, start, end);
        scheduleCaretUpdate();
    });

    // Initial render.
    rerender();
}
