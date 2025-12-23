// Prompt cartouche overlay
//
// Implements an in-place “rich textarea” overlay for Vontology concept IDs.
// When a concept is inserted from autocomplete, we insert a non-trigger form
// (#V\u200B#...) into the textarea so autocomplete does not re-trigger.
// The overlay renders those tokens as cartouches and supports removal.

const ZWSP = '\u200B';

const SEARCH_API = '/von/api/search';

// Match #V\u200B#<id> and #v\u200B#<id>
// Mirrors the allowed id character set used elsewhere.
const NON_TRIGGER_TOKEN_RE = /#([Vv])\u200B#([A-Za-z0-9_\(\)\./:–\-]+?)(?=[\s"'`]|$)/g;

// For normalising before send.
const NON_TRIGGER_PREFIX_RE = /#([Vv])\u200B#/g;

// Cache concept metadata used for prompt cartouches.
// Map<fullId, { name: string, kind: string } | null>
const promptConceptMetaCache = new Map();
// Map<fullId, Promise<meta|null>>
const promptConceptMetaPending = new Map();

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
            return { wrapper: parent, overlay, inner };
        }
    }

    const wrapper = document.createElement('div');
    wrapper.className = 'prompt-input-wrapper';

    const overlay = document.createElement('div');
    overlay.className = 'prompt-input-overlay';

    const inner = document.createElement('div');
    inner.className = 'prompt-input-overlay-inner';
    overlay.appendChild(inner);

    // Wrap the textarea in-place.
    const insertBeforeTarget = textarea;
    insertBeforeTarget.parentNode.insertBefore(wrapper, insertBeforeTarget);
    wrapper.appendChild(textarea);
    wrapper.appendChild(overlay);

    return { wrapper, overlay, inner };
}

function syncOverlayStyles(textarea, overlay, inner) {
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
        // Do not copy its colour, otherwise the overlay becomes transparent too.
        // Prefer caretColour if set; otherwise fall back to CSS.
        const caretColour = cs.caretColor;
        if (caretColour && caretColour !== 'auto') {
            inner.style.color = caretColour;
        } else {
            inner.style.removeProperty('color');
        }
        inner.style.textAlign = cs.textAlign;
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
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'vontology-cartouche prompt-vontology-cartouche';
    btn.dataset.fullConceptId = fullId;
    btn.dataset.start = String(start);
    btn.dataset.end = String(end);
    btn.title = 'Concept reference';
    btn.setAttribute('aria-label', `Concept reference ${fullId}`);

    const name = document.createElement('span');
    name.className = 'vontology-cartouche-name';
    name.textContent = '…';

    const id = document.createElement('span');
    id.className = 'vontology-cartouche-id';
    id.textContent = fullId;

    const kind = document.createElement('span');
    kind.className = 'vontology-cartouche-kind type';
    kind.textContent = '…';

    const remove = document.createElement('span');
    remove.className = 'prompt-vontology-cartouche-remove';
    remove.textContent = '×';
    remove.setAttribute('role', 'button');
    remove.setAttribute('aria-label', `Remove concept ${fullId}`);
    remove.tabIndex = -1;

    btn.appendChild(name);
    btn.appendChild(id);
    btn.appendChild(kind);
    btn.appendChild(remove);

    return btn;
}

function renderOverlay(textarea, inner) {
    const value = String(textarea?.value ?? '');
    clearChildren(inner);

    if (!value) {
        // Let the native placeholder do its job.
        return;
    }

    const tokens = extractNonTriggerTokensWithPositions(value);
    if (tokens.length === 0) {
        inner.appendChild(document.createTextNode(value));
        return;
    }

    let lastIndex = 0;
    for (const token of tokens) {
        if (token.start > lastIndex) {
            inner.appendChild(document.createTextNode(value.slice(lastIndex, token.start)));
        }
        const fullId = `#V#${token.conceptId}`;
        inner.appendChild(createPromptCartouche(fullId, token.start, token.end));
        lastIndex = token.end;
    }
    if (lastIndex < value.length) {
        inner.appendChild(document.createTextNode(value.slice(lastIndex)));
    }
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
                const name =
                    node?.display_name ||
                    node?.name ||
                    node?.node?.display_name ||
                    node?.node?.name ||
                    fullId;
                const kind = node?.kind || node?.node?.kind || 'type';
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

function hydratePromptCartouches(root) {
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
        });
    }
}

export function initializePromptCartoucheOverlay(textarea) {
    const parts = ensureOverlay(textarea);
    if (!parts) {
        return;
    }

    parts.wrapper.dataset.initialised = '1';
    syncOverlayStyles(textarea, parts.overlay, parts.inner);

    // Keep overlay updated.
    const rerender = () => {
        renderOverlay(textarea, parts.inner);
        hydratePromptCartouches(parts.inner);
    };

    textarea.addEventListener('input', rerender);
    textarea.addEventListener('scroll', () => {
        try {
            parts.inner.style.transform = `translateY(-${textarea.scrollTop}px)`;
        } catch (_) {
            // Ignore.
        }
    });
    window.addEventListener('resize', () => syncOverlayStyles(textarea, parts.overlay, parts.inner));

    // If the caret lands inside a cartouche token, treat backspace/delete as removing the whole token.
    textarea.addEventListener('keydown', (event) => {
        const key = event.key;
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
        }
    });

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
    });

    // Initial render.
    rerender();
}
