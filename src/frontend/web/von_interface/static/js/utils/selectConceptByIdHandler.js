import { getPreferredLanguage, selectBestNameForContext, selectShortestNameForContext } from './nameSelection.js';
import { applyCartoucheAppearance, getCartoucheAppearanceSettings } from './textDecorator.js';
import { showToast } from './toast.js';

export function normaliseVontologyId(value) {
    const raw = (value || '').toString().trim();
    if (!raw) return '';

    // Normalise prefix.
    let id = raw.startsWith('#V#') ? raw : `#V#${raw}`;

    // Heuristic: strip common sentence-ending punctuation accidentally attached to concept IDs.
    // This fixes cases like "#V#llm_workflow." or "#V#ai_agent_workflow:".
    // We only strip from the end; periods inside IDs (e.g. initials) are preserved.
    const trailingJunk = new Set(['.', ',', ':', ';', '!', '?', ')', ']', '}', '…']);
    while (id.length > 3 && trailingJunk.has(id[id.length - 1])) {
        id = id.slice(0, -1);
    }

    // Canonicalise slug: lowercased, and replace maximal spans of non-alphanumeric
    // characters with a single underscore.
    // This prevents hyphen/underscore variants resolving as distinct concepts.
    const slug = id.slice(3).toLowerCase();
    const canonicalSlug = slug
        .replace(/[^a-z0-9]+/g, '_')
        .replace(/^_+|_+$/g, '');
    if (!canonicalSlug) return '';
    id = `#V#${canonicalSlug}`;

    return id;
}

function deriveNameFromConceptId(conceptId) {
    return (conceptId || '').toString().replace(/^#V#/, '');
}

function normaliseKind(kind) {
    const k = (kind || '').toString().toLowerCase().trim();
    if (k === 'instance') return 'individual';
    if (k === 'individual' || k === 'type' || k === 'predicate') return k;
    return '';
}

async function conceptExists(conceptId, fetchFn) {
    const id = normaliseVontologyId(conceptId);
    if (!id) return false;
    const url = `/vontology/api/vontology/node_content?identifier=${encodeURIComponent(id)}`;
    let res;
    try {
        res = await fetchFn(url, { method: 'GET', headers: { 'Accept': 'application/json' } });
    } catch (_) {
        // Network failures should not be treated as "missing".
        throw new Error('Failed to check concept existence');
    }
    if (res.ok) return true;
    if (res.status === 404) return false;
    throw new Error(`Concept existence check failed (HTTP ${res.status})`);
}

async function createConceptForId(conceptId, options, fetchFn) {
    const id = normaliseVontologyId(conceptId);
    if (!id) throw new Error('Concept ID is required');

    const createAsInstance = !!options?.createAsInstance;
    const parentId = normaliseVontologyId(options?.parentId || '#V#thing');
    const name = deriveNameFromConceptId(id);

    const payload = {
        new_concept_name: name,
        parent_id: parentId,
        create_as_instance: createAsInstance
    };

    const res = await fetchFn('/vontology/api/vontology/create_concept', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify(payload)
    });

    // Idempotency: treat conflicts as success (another click/window created it).
    if (res.status === 409) {
        return id;
    }

    const text = await res.text();
    if (!res.ok) {
        throw new Error(`Create concept failed (HTTP ${res.status}): ${text.slice(0, 240)}`);
    }

    try {
        const json = JSON.parse(text);
        return json?.concept_id || json?.concept?.concept_id || id;
    } catch (_) {
        return id;
    }
}

function defaultCreateOptionsFromKind(kind) {
    const k = normaliseKind(kind);
    return {
        createAsInstance: k === 'individual',
        parentId: k === 'predicate' ? '#V#predicate' : '#V#thing'
    };
}

function kindLabel(kind) {
    const k = normaliseKind(kind);
    if (k === 'individual') return 'individual';
    if (k === 'predicate') return 'predicate';
    return 'type';
}

function formatKindLabel(kind) {
    const k = normaliseKind(kind);
    if (k === 'predicate') return 'Predicate';
    if (k === 'individual') return 'Individual';
    return 'Type';
}

function normaliseKindClass(kind) {
    const k = normaliseKind(kind);
    if (k === 'type' || k === 'predicate' || k === 'individual') return k;
    return 'type';
}

async function fetchConceptMetadata(conceptId, fetchFn) {
    const id = normaliseVontologyId(conceptId);
    if (!id) return null;

    const url = `/vontology/api/vontology/node_content?identifier=${encodeURIComponent(id)}&raw_only=1`;
    let res;
    try {
        res = await fetchFn(url, { method: 'GET', headers: { 'Accept': 'application/json' } });
    } catch (_) {
        return null;
    }

    if (!res.ok) return null;

    try {
        const json = await res.json();
        const preferredLanguage = getPreferredLanguage();
        const names = json?.raw_doc?.names;
        const bestName = selectBestNameForContext(names, preferredLanguage);
        const shortestName = selectShortestNameForContext(names, preferredLanguage);
        const prefs = getCartoucheAppearanceSettings();
        const displayName = (prefs?.useShortestName ? (shortestName || bestName) : (bestName || shortestName)) || json?.display_name || null;
        return {
            displayName,
            bestName: bestName ? String(bestName) : null,
            shortestName: shortestName ? String(shortestName) : null,
            kind: json?.kind || json?.computed_kind || null,
            names: names || null
        };
    } catch (_) {
        return null;
    }
}

function updateCartouchesForConcept(conceptId, metadata) {
    const id = normaliseVontologyId(conceptId);
    if (!id || !metadata) return;

    const cartouches = Array.from(document.querySelectorAll('.vontology-cartouche'));
    for (const el of cartouches) {
        try {
            if (el?.dataset?.fullConceptId !== id) continue;

            const prefs = getCartoucheAppearanceSettings();
            const displayName = (prefs?.useShortestName
                ? (metadata.shortestName || metadata.displayName)
                : (metadata.displayName || metadata.shortestName)) || deriveNameFromConceptId(id);
            const nameEl = el.querySelector('.vontology-cartouche-name');
            if (nameEl) nameEl.textContent = displayName;

            el.classList.remove('vontology-cartouche-missing');

            const kind = metadata.kind || '';
            const kindEl = el.querySelector('.vontology-cartouche-kind');
            if (kindEl) {
                const kindClass = normaliseKindClass(kind);
                kindEl.className = `vontology-cartouche-kind ${kindClass}`;
                kindEl.textContent = formatKindLabel(kind);
            }
            el.dataset.kind = kind;
            applyCartoucheAppearance(el, prefs);
        } catch (_) {
            // Ignore per-cartouche update failures.
        }
    }
}

function updateCartouchesForMissingConcept(conceptId) {
    const id = normaliseVontologyId(conceptId);
    if (!id) return;

    const cartouches = Array.from(document.querySelectorAll('.vontology-cartouche'));
    for (const el of cartouches) {
        try {
            if (el?.dataset?.fullConceptId !== id) continue;

            el.classList.add('vontology-cartouche-missing');
            el.dataset.kind = '';

            const nameEl = el.querySelector('.vontology-cartouche-name');
            if (nameEl) nameEl.textContent = '';

            const kindEl = el.querySelector('.vontology-cartouche-kind');
            if (kindEl) {
                kindEl.className = 'vontology-cartouche-kind';
                kindEl.textContent = '';
            }
        } catch (_) {
            // Ignore per-cartouche update failures.
        }
    }
}

function _createModalElement() {
    const modal = document.createElement('div');
    modal.className = 'modal';
    modal.setAttribute('aria-hidden', 'true');
    modal.setAttribute('role', 'dialog');

    const content = document.createElement('div');
    content.className = 'modal-content';
    modal.appendChild(content);

    return { modal, content };
}

function _defaultParentForCreateKind(kind) {
    const k = normaliseKind(kind);
    return k === 'predicate' ? '#V#predicate' : '#V#thing';
}

/**
 * Option B: show a small modal to choose kind (type/individual/predicate) and optional parent.
 * Exported for testing.
 */
export function openCreateConceptModal(conceptId, initialKind) {
    const id = normaliseVontologyId(conceptId);
    const k0 = normaliseKind(initialKind) || 'type';

    if (!id) return Promise.resolve(null);

    return new Promise((resolve) => {
        const { modal, content } = _createModalElement();

        const titleId = `createConceptTitle_${Math.random().toString(16).slice(2)}`;
        modal.setAttribute('aria-labelledby', titleId);

        const h2 = document.createElement('h2');
        h2.id = titleId;
        h2.textContent = 'Create missing concept';

        const intro = document.createElement('p');
        intro.textContent = `Create ${id} in the Vontology.`;

        const form = document.createElement('div');
        form.className = 'create-concept-modal-form';

        const kindRow = document.createElement('div');
        kindRow.className = 'create-concept-modal-row';
        const kindLabelEl = document.createElement('label');
        kindLabelEl.textContent = 'Kind:';
        kindLabelEl.htmlFor = `${titleId}_kind`;
        const kindSelect = document.createElement('select');
        kindSelect.id = `${titleId}_kind`;
        kindSelect.innerHTML = [
            '<option value="type">Type</option>',
            '<option value="individual">Individual</option>',
            '<option value="predicate">Predicate</option>'
        ].join('');
        kindSelect.value = k0;
        kindRow.appendChild(kindLabelEl);
        kindRow.appendChild(kindSelect);

        const parentRow = document.createElement('div');
        parentRow.className = 'create-concept-modal-row';
        const parentLabelEl = document.createElement('label');
        parentLabelEl.textContent = 'Parent (optional):';
        parentLabelEl.htmlFor = `${titleId}_parent`;
        const parentInput = document.createElement('input');
        parentInput.id = `${titleId}_parent`;
        parentInput.type = 'text';
        parentInput.placeholder = _defaultParentForCreateKind(k0);
        parentInput.autocomplete = 'off';
        parentRow.appendChild(parentLabelEl);
        parentRow.appendChild(parentInput);

        const hint = document.createElement('p');
        hint.className = 'create-concept-modal-hint';
        hint.textContent = 'Leave parent blank to use the default.';

        form.appendChild(kindRow);
        form.appendChild(parentRow);
        form.appendChild(hint);

        const actions = document.createElement('div');
        actions.className = 'modal-actions';
        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.textContent = 'Cancel';
        const createBtn = document.createElement('button');
        createBtn.type = 'button';
        createBtn.textContent = 'Create';
        actions.appendChild(cancelBtn);
        actions.appendChild(createBtn);

        content.appendChild(h2);
        content.appendChild(intro);
        content.appendChild(form);
        content.appendChild(actions);

        function cleanup(result) {
            try {
                modal.classList.remove('open');
                modal.setAttribute('aria-hidden', 'true');
                modal.remove();
            } catch (_) { }
            resolve(result);
        }

        function computeResult() {
            const k = normaliseKind(kindSelect.value) || 'type';
            const parentRaw = (parentInput.value || '').toString().trim();
            const parentId = normaliseVontologyId(parentRaw || _defaultParentForCreateKind(k));
            if (!parentId) {
                showToast('Parent concept ID is invalid.', 'error');
                return null;
            }
            return {
                createAsInstance: k === 'individual' || k === 'predicate',
                parentId,
                kind: k
            };
        }

        kindSelect.addEventListener('change', () => {
            parentInput.placeholder = _defaultParentForCreateKind(kindSelect.value);
        });

        createBtn.addEventListener('click', () => {
            const result = computeResult();
            if (!result) return;
            cleanup(result);
        });
        cancelBtn.addEventListener('click', () => cleanup(null));

        modal.addEventListener('click', (e) => {
            // Backdrop click closes.
            if (e.target === modal) cleanup(null);
        });
        modal.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                e.preventDefault();
                cleanup(null);
            }
        });

        document.body.appendChild(modal);
        modal.classList.add('open');
        modal.setAttribute('aria-hidden', 'false');
        // Keep the header visible even if focus triggers scroll.
        try { content.scrollTop = 0; } catch (_) { }

        // Focus kind selector for quick keyboard interaction, but avoid scrolling.
        try {
            if (typeof kindSelect.focus === 'function') {
                kindSelect.focus({ preventScroll: true });
            }
        } catch (_) {
            try { kindSelect.focus(); } catch (_) { }
        }

        try { content.scrollTop = 0; } catch (_) { }
    });
}

function deriveKindFromCreateOptions(createOpts, fallbackKind) {
    const explicit = normaliseKind(createOpts?.kind);
    if (explicit) return explicit;

    const k = normaliseKind(fallbackKind);

    const parentId = normaliseVontologyId(createOpts?.parentId);
    if (createOpts?.createAsInstance && parentId === '#V#predicate') return 'predicate';
    if (createOpts?.createAsInstance) return 'individual';
    if (k) return k;
    return 'type';
}

/**
 * Handles the global 'von:selectConceptById' event detail payload.
 *
 * Deps must provide:
 * - createOrActivateConceptTab(conceptId, displayName, activate)
 * - activateTab(tabId)
 * - selectVontologyNodeByIdentifier(conceptId, createConceptTab)
 * Optional:
 * - fetchFn (default window.fetch)
 * - confirmFn/promptFn (default window.confirm/prompt)
 */
export async function handleSelectConceptByIdDetail(detail, deps) {
    const conceptIdRaw = detail?.conceptId;
    const createConceptTab = !!detail?.createConceptTab;
    const kind = detail?.kind || null;
    const modifierKeys = detail?.modifierKeys || {};

    const id = normaliseVontologyId(conceptIdRaw);
    if (!id) return;

    const fetchFn = deps?.fetchFn || fetch;

    if (!createConceptTab) {
        deps.activateTab('vontologyTab');
        setTimeout(() => {
            deps.selectVontologyNodeByIdentifier(id, createConceptTab);
        }, 0);
        return;
    }

    // Normal click: open in background.
    // Shift-click: open and switch to it.
    const shouldActivate = !!modifierKeys.shiftKey;

    // Open the tab immediately (optimistic) so the UI responds even if the backend
    // is busy (e.g., single-threaded server while chat generation is in flight).
    // Metadata will be hydrated below when available.
    try {
        deps.createOrActivateConceptTab(id, 'Loading…', shouldActivate);
    } catch (_) {
        // Best-effort; keep going.
    }

    let exists = false;
    try {
        exists = await conceptExists(id, fetchFn);
    } catch (err) {
        console.warn('[selectConceptById] existence check failed', err);
        // Fall back to existing behaviour: ensure the tab exists.
        deps.createOrActivateConceptTab(id, 'Loading…', shouldActivate);
        return;
    }

    if (exists) {
        const metadata = await fetchConceptMetadata(id, fetchFn);
        updateCartouchesForConcept(id, metadata);
        deps.createOrActivateConceptTab(id, metadata?.displayName || id, shouldActivate);
        return;
    }

    updateCartouchesForMissingConcept(id);

    // Concept does not exist: remove the optimistic tab rather than leaving a
    // dead "Loading…" tab around.
    try {
        deps?.closeDynamicConceptTab?.(id);
    } catch (_) {
        // Best-effort; keep going.
    }

    const chooseCreateOptionsFn = deps?.chooseCreateOptionsFn;
    const createOpts = chooseCreateOptionsFn
        ? await chooseCreateOptionsFn({ conceptId: id, kind, modifierKeys })
        : await openCreateConceptModal(id, kind);
    if (!createOpts) {
        try {
            deps?.closeDynamicConceptTab?.(id);
        } catch (_) {
            // Best-effort; keep going.
        }
        return;
    }

    // Re-open a loading tab now that the user has confirmed creation.
    try {
        deps.createOrActivateConceptTab(id, 'Loading…', shouldActivate);
    } catch (_) {
        // Best-effort; keep going.
    }

    try {
        const chosenKind = deriveKindFromCreateOptions(createOpts, kind);
        showToast(`Creating ${kindLabel(chosenKind)}…`, 'info');
        await createConceptForId(id, createOpts, fetchFn);
        const metadata = await fetchConceptMetadata(id, fetchFn);
        updateCartouchesForConcept(id, metadata);
        deps.createOrActivateConceptTab(id, metadata?.displayName || id, shouldActivate);
        showToast('Concept created.', 'info');
    } catch (err) {
        console.warn('[selectConceptById] create failed', err);
        showToast(`Failed to create concept: ${(err && err.message) ? err.message : 'Unknown error'}`, 'error');
    }
}
