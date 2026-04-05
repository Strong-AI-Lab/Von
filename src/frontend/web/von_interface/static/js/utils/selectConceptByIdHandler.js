import { getPreferredLanguage, selectBestNameForContext, selectShortestNameForContext } from './nameSelection.js';
import { applyCartoucheAppearance, getCartoucheAppearanceSettings } from './textDecorator.js';
import { showToast } from './toast.js';

export function normaliseVontologyId(value) {
    const raw = (value || '').toString().trim();
    if (!raw) return '';

    // Normalise prefix without changing slug case.
    // Canonical predicate IDs may be mixed-case (for example #V#hasName), so
    // downcasing here can turn existing concepts into false misses.
    let id = raw;
    if (/^[Vv]#/.test(id)) id = `#${id}`;
    if (id.startsWith('#v#')) id = `#V#${id.slice(3)}`;
    if (!id.startsWith('#V#')) id = `#V#${id}`;

    // Heuristic: strip common sentence-ending punctuation accidentally attached to concept IDs.
    // This fixes cases like "#V#llm_workflow." or "#V#ai_agent_workflow:".
    // We only strip from the end; periods inside IDs (e.g. initials) are preserved.
    const trailingJunk = new Set(['.', ',', ':', ';', '!', '?', ')', ']', '}', '…']);
    while (id.length > 3 && trailingJunk.has(id[id.length - 1])) {
        id = id.slice(0, -1);
    }

    // Canonicalise separators while preserving case.
    // This keeps mixed-case canonical IDs intact while still collapsing accidental
    // whitespace/punctuation runs to underscores.
    let slug = id.slice(3);
    try {
        slug = slug.normalize('NFKD').replace(/[\u0300-\u036f]/g, '');
        slug = slug.normalize('NFKC');
    } catch (_) {
        // Ignore normalisation errors and fall back to raw slug.
    }
    const canonicalSlug = slug
        .replace(/[^A-Za-z0-9]+/g, '_')
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

const IMPLICIT_PARENT_CONFIDENCE_THRESHOLD = 0.55;
const MAX_PARENT_SUGGESTIONS = 3;

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

function normaliseConfidence(value) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return null;
    if (value >= 0 && value <= 1) return value;
    if (value > 1 && value <= 100) return value / 100;
    return null;
}

function buildReadableName(value, fallbackId = '') {
    const raw = (value || '').toString().trim();
    if (raw) return raw;
    const core = deriveNameFromConceptId(fallbackId).replace(/_/g, ' ').trim();
    return core || fallbackId || '';
}

function normaliseParentSuggestion(entry) {
    if (!entry || typeof entry !== 'object') return null;
    const conceptId = normaliseVontologyId(entry.conceptId || entry.id || entry.parent_id || '');
    if (!conceptId) return null;
    return {
        conceptId,
        name: buildReadableName(entry.name || entry.display_name || '', conceptId),
        confidence: normaliseConfidence(entry.confidence ?? entry.relevance_score ?? entry.score),
        rationale: (entry.rationale || entry.reason || '').toString().trim(),
        provenance: (entry.provenance || entry.source || '').toString().trim(),
        exists: entry.exists !== false
    };
}

function mergeParentSuggestions(preferred, fallback) {
    const merged = [];
    const seen = new Set();
    const append = (items) => {
        if (!Array.isArray(items)) return;
        for (const item of items) {
            const normalised = normaliseParentSuggestion(item);
            if (!normalised || seen.has(normalised.conceptId)) continue;
            seen.add(normalised.conceptId);
            merged.push(normalised);
        }
    };
    append(preferred);
    append(fallback);
    return merged;
}

function deriveProvenanceCounts(parentSuggestions) {
    let explicitCount = 0;
    let implicitCount = 0;
    let unknownCount = 0;
    const rows = Array.isArray(parentSuggestions) ? parentSuggestions : [];
    for (const row of rows) {
        const provenance = (row?.provenance || '').toString().toLowerCase();
        if (!provenance) {
            unknownCount += 1;
            continue;
        }
        if (provenance.includes('implicit')) {
            implicitCount += 1;
            continue;
        }
        if (provenance.includes('explicit')) {
            explicitCount += 1;
            continue;
        }
        unknownCount += 1;
    }
    return { explicitCount, implicitCount, unknownCount };
}

function normaliseAnnotationSources(value) {
    const input = Array.isArray(value) ? value : (typeof value === 'string' ? [value] : []);
    const seen = new Set();
    const out = [];
    for (const item of input) {
        const source = (item || '').toString().trim().toLowerCase();
        if (!source || seen.has(source)) continue;
        seen.add(source);
        out.push(source);
    }
    return out;
}

function confidenceFromAnnotationSuggestion(suggestion) {
    const direct = normaliseConfidence(suggestion?.confidence_score ?? suggestion?.confidence);
    if (direct !== null) return direct;
    const candidate = Array.isArray(suggestion?.candidates) ? suggestion.candidates[0] : null;
    return normaliseConfidence(candidate?.confidence ?? candidate?.relevance_score ?? candidate?.score);
}

function deriveParentSearchQuery(conceptId, detail) {
    const contextText = (detail?.proposalContext?.text || detail?.contextText || '').toString().trim();
    if (contextText) return contextText;
    return deriveNameFromConceptId(conceptId).replace(/_/g, ' ').trim();
}

function deriveCreateProposal(conceptId, detail) {
    const proposedNameRaw = (detail?.createProposal?.proposedName
        || detail?.proposalContext?.proposed_name
        || detail?.proposedName
        || '').toString().trim();
    const proposedDescription = (detail?.createProposal?.proposedDescription
        || detail?.proposalContext?.proposed_description
        || detail?.proposedDescription
        || '').toString().trim();
    return {
        proposedName: proposedNameRaw || deriveNameFromConceptId(conceptId),
        proposedDescription,
        parentSuggestions: Array.isArray(detail?.createProposal?.parentSuggestions)
            ? detail.createProposal.parentSuggestions
            : (Array.isArray(detail?.proposalContext?.parent_suggestions)
                ? detail.proposalContext.parent_suggestions
                : []),
        breadcrumbs: Array.isArray(detail?.createProposal?.breadcrumbs) ? detail.createProposal.breadcrumbs : [],
        lowConfidence: !!detail?.createProposal?.lowConfidence
    };
}

async function fetchParentSuggestionsBySearch(conceptId, kind, detail, fetchFn) {
    const query = deriveParentSearchQuery(conceptId, detail);
    if (!query) return [];

    const params = new URLSearchParams();
    params.set('q', query);
    params.set('limit', '12');
    params.set('fallback_substring', '1');
    if (normaliseKind(kind) === 'predicate') {
        params.set('filter_kind', 'predicate,type');
    } else {
        params.set('filter_kind', 'type');
    }

    let res;
    try {
        res = await fetchFn(`/vontology/api/vontology/search?${params.toString()}`, {
            method: 'GET',
            headers: { 'Accept': 'application/json' }
        });
    } catch (_) {
        return [];
    }
    if (!res.ok) return [];

    let json;
    try {
        json = await res.json();
    } catch (_) {
        return [];
    }

    const rows = Array.isArray(json?.results) ? json.results : [];
    const suggestions = [];
    for (const row of rows) {
        const parentId = normaliseVontologyId(row?.id || row?.concept_id || '');
        if (!parentId || parentId === conceptId) continue;
        suggestions.push({
            conceptId: parentId,
            name: buildReadableName(row?.name || row?.display_name || '', parentId),
            confidence: normaliseConfidence(row?.relevance_score),
            rationale: `Matched ontology search for "${query}"`,
            provenance: 'explicit',
            exists: true
        });
        if (suggestions.length >= MAX_PARENT_SUGGESTIONS) break;
    }
    return suggestions;
}

async function fetchImplicitParentSuggestionsByAnnotations(conceptId, detail, fetchFn) {
    const queryText = deriveParentSearchQuery(conceptId, detail);
    if (!queryText) return [];

    const payload = {
        conversation_id: 'create-proposal',
        turn_id: `create-proposal-${Date.now()}`,
        speaker: 'user',
        text: queryText,
        metadata: {
            llm_enrich: true,
            match: true
        },
        context: {
            language: getPreferredLanguage()
        }
    };

    let res;
    try {
        res = await fetchFn('/api/annotations/turn', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: JSON.stringify(payload)
        });
    } catch (_) {
        return [];
    }
    if (!res.ok) return [];

    let json = null;
    try {
        if (typeof res.json === 'function') {
            json = await res.json();
        } else if (typeof res.text === 'function') {
            const text = await res.text();
            json = text ? JSON.parse(text) : null;
        }
    } catch (_) {
        json = null;
    }

    const rows = Array.isArray(json?.suggestions) ? json.suggestions : [];
    const derived = [];
    for (const row of rows) {
        const sources = normaliseAnnotationSources(row?.span?.source || row?.source);
        if (!sources.includes('llm') && !sources.includes('fallback')) continue;

        const suggestedTypeId = normaliseVontologyId(
            row?.suggested_type_id || row?.suggestedTypeId || row?.type_id || row?.typeId || ''
        );
        if (!suggestedTypeId || suggestedTypeId === conceptId) continue;

        const confidence = confidenceFromAnnotationSuggestion(row);
        if (confidence === null || confidence < IMPLICIT_PARENT_CONFIDENCE_THRESHOLD) continue;

        const matchingCandidate = Array.isArray(row?.candidates)
            ? row.candidates.find((candidate) => normaliseVontologyId(candidate?.concept_id || candidate?.conceptId || candidate?.id || '') === suggestedTypeId)
            : null;
        const name = buildReadableName(
            matchingCandidate?.name || matchingCandidate?.display_name || '',
            suggestedTypeId
        );
        const spanText = (row?.span?.text || '').toString().trim();
        const sourceLabel = sources.join('+') || 'annotation';
        const rationale = spanText
            ? `Implicit type inferred from "${spanText}" (${sourceLabel})`
            : `Implicit type inferred (${sourceLabel})`;

        derived.push({
            conceptId: suggestedTypeId,
            name,
            confidence,
            rationale,
            provenance: 'implicit(annotation)',
            exists: true
        });
    }
    return derived;
}

async function updateConceptDescription(conceptId, description, fetchFn) {
    const id = normaliseVontologyId(conceptId);
    const text = (description || '').toString().trim();
    if (!id || !text) return;

    try {
        const res = await fetchFn('/vontology/api/vontology/update_description', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: JSON.stringify({ identifier: id, description: text })
        });
        if (!res.ok) {
            console.warn('[selectConceptById] update description failed', res.status);
        }
    } catch (err) {
        console.warn('[selectConceptById] update description request failed', err);
    }
}

async function createConceptForId(conceptId, options, fetchFn) {
    const id = normaliseVontologyId(conceptId);
    if (!id) throw new Error('Concept ID is required');

    const createAsInstance = !!options?.createAsInstance;
    const parentId = normaliseVontologyId(options?.parentId || '#V#thing');
    const name = (options?.name || '').toString().trim() || deriveNameFromConceptId(id);
    const description = (options?.description || '').toString().trim();

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
        await updateConceptDescription(id, description, fetchFn);
        return id;
    }

    const text = await res.text();
    if (!res.ok) {
        throw new Error(`Create concept failed (HTTP ${res.status}): ${text.slice(0, 240)}`);
    }

    let resolvedId = id;
    try {
        const json = JSON.parse(text);
        resolvedId = json?.concept_id || json?.concept?.concept_id || id;
    } catch (_) {
        resolvedId = id;
    }
    await updateConceptDescription(resolvedId, description, fetchFn);
    return resolvedId;
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
            // Remove hide classes so CSS for missing cartouches can control visibility.
            // The ID must be visible; CSS hides name and kind for missing cartouches.
            el.classList.remove('cartouche-hide-id', 'cartouche-hide-name', 'cartouche-hide-kind', 'cartouche-kind-as-bg');
            el.dataset.kind = '';
            el.title = "This concept doesn't exist yet — click to create";

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

export async function hydrateConceptCartouchesInRoot(root, { fetchFn: explicitFetchFn } = {}) {
    if (!root || typeof root.querySelectorAll !== 'function') {
        return [];
    }

    const cartouches = Array.from(root.querySelectorAll('.vontology-cartouche[data-full-concept-id]'));
    if (!cartouches.length) {
        return [];
    }

    const uniqueIds = [];
    const seen = new Set();
    for (const el of cartouches) {
        const fullId = normaliseVontologyId(el?.dataset?.fullConceptId || '');
        if (!fullId || seen.has(fullId)) continue;
        seen.add(fullId);
        uniqueIds.push(fullId);
    }
    if (!uniqueIds.length) {
        return [];
    }

    const fetchFn = explicitFetchFn || fetch;
    await Promise.all(uniqueIds.map(async (conceptId) => {
        try {
            const exists = await conceptExists(conceptId, fetchFn);
            if (!exists) {
                updateCartouchesForMissingConcept(conceptId);
                return;
            }
            const metadata = await fetchConceptMetadata(conceptId, fetchFn);
            if (metadata) {
                updateCartouchesForConcept(conceptId, metadata);
            }
        } catch (_) {
            // Keep placeholder state if hydration fails transiently.
        }
    }));

    return uniqueIds;
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
 * Show a creation modal with editable defaults plus ranked parent suggestions.
 * Exported for testing.
 */
export function openCreateConceptModal(conceptId, initialKind, proposal = null) {
    const id = normaliseVontologyId(conceptId);
    const k0 = normaliseKind(initialKind) || 'type';
    if (!id) return Promise.resolve(null);

    const proposalData = proposal && typeof proposal === 'object' ? proposal : {};
    const mergedSuggestions = mergeParentSuggestions(proposalData.parentSuggestions, []);
    const parentSuggestions = mergedSuggestions.slice(0, MAX_PARENT_SUGGESTIONS);
    const proposedNameDefault = (proposalData.proposedName || '').toString().trim() || deriveNameFromConceptId(id);
    const proposedDescriptionDefault = (proposalData.proposedDescription || '').toString().trim();
    const breadcrumbs = Array.isArray(proposalData.breadcrumbs)
        ? proposalData.breadcrumbs.filter((item) => !!normaliseVontologyId(item))
        : [];

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

        if (breadcrumbs.length) {
            const breadcrumb = document.createElement('p');
            breadcrumb.className = 'create-concept-modal-breadcrumb';
            breadcrumb.textContent = `Create path: ${breadcrumbs.join(' > ')}`;
            form.appendChild(breadcrumb);
        }

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

        const nameRow = document.createElement('div');
        nameRow.className = 'create-concept-modal-row';
        const nameLabel = document.createElement('label');
        nameLabel.textContent = 'Name:';
        nameLabel.htmlFor = `${titleId}_name`;
        const nameInput = document.createElement('input');
        nameInput.id = `${titleId}_name`;
        nameInput.type = 'text';
        nameInput.autocomplete = 'off';
        nameInput.value = proposedNameDefault;
        nameRow.appendChild(nameLabel);
        nameRow.appendChild(nameInput);

        const descriptionRow = document.createElement('div');
        descriptionRow.className = 'create-concept-modal-row';
        const descriptionLabel = document.createElement('label');
        descriptionLabel.textContent = 'Description (optional):';
        descriptionLabel.htmlFor = `${titleId}_description`;
        const descriptionInput = document.createElement('textarea');
        descriptionInput.id = `${titleId}_description`;
        descriptionInput.rows = 3;
        descriptionInput.value = proposedDescriptionDefault;
        descriptionRow.appendChild(descriptionLabel);
        descriptionRow.appendChild(descriptionInput);

        const parentSuggestionChoiceName = `${titleId}_parent_choice`;
        const parentSuggestionRow = document.createElement('div');
        parentSuggestionRow.className = 'create-concept-modal-row create-concept-parent-suggestions-row';
        const parentSuggestionLabel = document.createElement('label');
        parentSuggestionLabel.textContent = 'Parent suggestions:';
        parentSuggestionRow.appendChild(parentSuggestionLabel);
        const parentSuggestionList = document.createElement('div');
        parentSuggestionList.className = 'create-concept-parent-suggestions';
        parentSuggestionRow.appendChild(parentSuggestionList);

        const parentRow = document.createElement('div');
        parentRow.className = 'create-concept-modal-row';
        const parentLabelEl = document.createElement('label');
        parentLabelEl.textContent = 'Manual parent ID (optional):';
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
        hint.textContent = 'Choose a suggested parent or enter one manually.';

        const parentPreview = document.createElement('div');
        parentPreview.className = 'create-concept-parent-preview';

        const suggestionOptionRows = [];
        for (let idx = 0; idx < parentSuggestions.length; idx += 1) {
            const suggestion = parentSuggestions[idx];
            const optionLabel = document.createElement('label');
            optionLabel.className = 'create-concept-parent-suggestion';

            const optionInput = document.createElement('input');
            optionInput.type = 'radio';
            optionInput.name = parentSuggestionChoiceName;
            optionInput.value = suggestion.conceptId;
            optionInput.checked = idx === 0;
            optionLabel.appendChild(optionInput);

            const optionTextWrap = document.createElement('span');
            optionTextWrap.className = 'create-concept-parent-suggestion-text';
            const optionTitle = document.createElement('span');
            optionTitle.className = 'create-concept-parent-suggestion-title';
            optionTitle.textContent = `${suggestion.name} (${suggestion.conceptId})`;
            optionTextWrap.appendChild(optionTitle);

            const metaParts = [];
            if (typeof suggestion.confidence === 'number') {
                metaParts.push(`confidence ${(suggestion.confidence * 100).toFixed(0)}%`);
            }
            if (suggestion.provenance) {
                metaParts.push(suggestion.provenance);
            }
            if (suggestion.rationale) {
                metaParts.push(suggestion.rationale);
            }
            if (suggestion.exists === false) {
                metaParts.push('missing (can be created first)');
            }
            if (metaParts.length) {
                const optionMeta = document.createElement('span');
                optionMeta.className = 'create-concept-parent-suggestion-meta';
                optionMeta.textContent = metaParts.join(' | ');
                optionTextWrap.appendChild(optionMeta);
            }

            optionLabel.appendChild(optionTextWrap);
            parentSuggestionList.appendChild(optionLabel);
            suggestionOptionRows.push({ input: optionInput, suggestion });
        }

        const manualOptionLabel = document.createElement('label');
        manualOptionLabel.className = 'create-concept-parent-suggestion';
        const manualOptionInput = document.createElement('input');
        manualOptionInput.type = 'radio';
        manualOptionInput.name = parentSuggestionChoiceName;
        manualOptionInput.value = '__manual__';
        manualOptionInput.checked = suggestionOptionRows.length === 0;
        manualOptionLabel.appendChild(manualOptionInput);
        const manualOptionText = document.createElement('span');
        manualOptionText.className = 'create-concept-parent-suggestion-text';
        manualOptionText.textContent = 'Manual parent';
        manualOptionLabel.appendChild(manualOptionText);
        parentSuggestionList.appendChild(manualOptionLabel);

        if (suggestionOptionRows.length === 0) {
            parentSuggestionLabel.textContent = 'Parent:';
            parentSuggestionRow.style.display = 'none';
        }

        form.appendChild(kindRow);
        form.appendChild(nameRow);
        form.appendChild(descriptionRow);
        form.appendChild(parentSuggestionRow);
        form.appendChild(parentPreview);
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

        function getSelectedSuggestion() {
            for (const option of suggestionOptionRows) {
                if (option.input.checked) return option.suggestion;
            }
            return null;
        }

        function isManualParentMode() {
            return !!manualOptionInput.checked;
        }

        function updateParentModeUi() {
            const manual = isManualParentMode();
            parentRow.style.display = manual ? '' : 'none';
            hint.textContent = manual
                ? 'Leave manual parent blank to use the kind default.'
                : 'You can switch to manual parent if none of the suggestions fit.';
            const selectedSuggestion = getSelectedSuggestion();
            if (manual) {
                parentPreview.textContent = 'Manual parent mode. Enter a concept ID, or leave blank for the kind default.';
            } else if (selectedSuggestion) {
                const previewLines = [];
                previewLines.push(`Selected parent: ${selectedSuggestion.name} (${selectedSuggestion.conceptId})`);
                if (typeof selectedSuggestion.confidence === 'number') {
                    previewLines.push(`Confidence: ${(selectedSuggestion.confidence * 100).toFixed(0)}%`);
                }
                if (selectedSuggestion.provenance) {
                    previewLines.push(`Provenance: ${selectedSuggestion.provenance}`);
                }
                if (selectedSuggestion.rationale) {
                    previewLines.push(`Rationale: ${selectedSuggestion.rationale}`);
                }
                if (selectedSuggestion.exists === false) {
                    previewLines.push('This parent does not currently exist and can be created inline.');
                }
                parentPreview.textContent = previewLines.join(' ');
            } else {
                parentPreview.textContent = '';
            }
        }

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
            const selectedSuggestion = getSelectedSuggestion();
            const parentRaw = (parentInput.value || '').toString().trim();
            const chosenParentRaw = isManualParentMode()
                ? (parentRaw || _defaultParentForCreateKind(k))
                : (selectedSuggestion?.conceptId || _defaultParentForCreateKind(k));
            const parentId = normaliseVontologyId(chosenParentRaw);
            if (!parentId) {
                showToast('Parent concept ID is invalid.', 'error');
                return null;
            }
            if (parentId === id) {
                showToast('Parent concept cannot equal concept ID.', 'error');
                return null;
            }
            const name = (nameInput.value || '').toString().trim() || deriveNameFromConceptId(id);
            const description = (descriptionInput.value || '').toString().trim();
            const topSuggestion = parentSuggestions[0] || null;
            return {
                createAsInstance: k === 'individual' || k === 'predicate',
                parentId,
                kind: k,
                name,
                description,
                selectedParentSuggested: !isManualParentMode() && !!selectedSuggestion,
                selectedParentConfidence: selectedSuggestion?.confidence ?? null,
                selectedParentRationale: selectedSuggestion?.rationale || '',
                selectedParentProvenance: selectedSuggestion?.provenance || '',
                selectedParentExists: selectedSuggestion?.exists !== false,
                parentDecision: isManualParentMode()
                    ? (topSuggestion ? 'manual_override' : 'manual_default')
                    : (selectedSuggestion?.conceptId === topSuggestion?.conceptId ? 'accept_top_suggestion' : 'accept_suggestion'),
                parentSuggestions: parentSuggestions.map((item) => ({ ...item }))
            };
        }

        kindSelect.addEventListener('change', () => {
            parentInput.placeholder = _defaultParentForCreateKind(kindSelect.value);
        });
        parentSuggestionList.addEventListener('keydown', (event) => {
            const isArrow = event.key === 'ArrowDown' || event.key === 'ArrowUp';
            if (!isArrow) return;
            const radios = Array.from(parentSuggestionList.querySelectorAll(`input[name="${parentSuggestionChoiceName}"]`));
            if (!radios.length) return;
            const activeIndex = radios.findIndex((radio) => radio.checked);
            const currentIndex = activeIndex >= 0 ? activeIndex : 0;
            const delta = event.key === 'ArrowDown' ? 1 : -1;
            const nextIndex = (currentIndex + delta + radios.length) % radios.length;
            radios[nextIndex].checked = true;
            radios[nextIndex].dispatchEvent(new Event('change', { bubbles: true }));
            event.preventDefault();
        });
        parentSuggestionList.addEventListener('change', updateParentModeUi);

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
        updateParentModeUi();

        try { content.scrollTop = 0; } catch (_) { }
        try {
            if (typeof nameInput.focus === 'function') {
                nameInput.focus({ preventScroll: true });
            }
        } catch (_) {
            try { nameInput.focus(); } catch (_) { }
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

function shouldHydrateCreateProposal(detail, chooseCreateOptionsFn, deps) {
    if (detail?.skipCreateProposalHydration) return false;
    if (!detail || typeof detail !== 'object') return true;
    if (detail.createProposal) return true;
    if (detail.proposalContext) return true;
    if (typeof deps?.fetchCreateProposalFn === 'function') return true;
    return true;
}

async function buildCreateProposalForConcept(conceptId, kind, detail, fetchFn, fetchCreateProposalFn) {
    const baseProposal = deriveCreateProposal(conceptId, detail);
    const fetcher = fetchCreateProposalFn;
    let fetchedSuggestions = [];
    let annotationSuggestions = [];
    if (typeof fetcher === 'function') {
        try {
            const fetched = await fetcher({ conceptId, kind, detail });
            if (fetched && Array.isArray(fetched.parentSuggestions)) {
                fetchedSuggestions = fetched.parentSuggestions;
                if (!baseProposal.proposedName && fetched.proposedName) {
                    baseProposal.proposedName = fetched.proposedName;
                }
                if (!baseProposal.proposedDescription && fetched.proposedDescription) {
                    baseProposal.proposedDescription = fetched.proposedDescription;
                }
            }
        } catch (err) {
            console.warn('[selectConceptById] custom proposal fetch failed', err);
        }
    } else {
        fetchedSuggestions = await fetchParentSuggestionsBySearch(conceptId, kind, detail, fetchFn);
    }
    try {
        annotationSuggestions = await fetchImplicitParentSuggestionsByAnnotations(conceptId, detail, fetchFn);
    } catch (err) {
        console.warn('[selectConceptById] annotation suggestion fetch failed', err);
    }
    const preferredSuggestions = mergeParentSuggestions(baseProposal.parentSuggestions, fetchedSuggestions);
    const mergedParentSuggestions = mergeParentSuggestions(preferredSuggestions, annotationSuggestions)
        .slice(0, MAX_PARENT_SUGGESTIONS);
    return {
        proposedName: baseProposal.proposedName || deriveNameFromConceptId(conceptId),
        proposedDescription: baseProposal.proposedDescription || '',
        parentSuggestions: mergedParentSuggestions,
        breadcrumbs: baseProposal.breadcrumbs || [],
        lowConfidence: baseProposal.lowConfidence || mergedParentSuggestions.length === 0
    };
}

function buildCreateDecisionTelemetryPayload({ conceptId, stage, createOpts, createProposal, error }) {
    const normalisedConceptId = normaliseVontologyId(conceptId);
    const parentId = normaliseVontologyId(createOpts?.parentId || '');
    const chosenKind = deriveKindFromCreateOptions(createOpts, createOpts?.kind);
    const provenanceCounts = deriveProvenanceCounts(createProposal?.parentSuggestions);
    return {
        conceptId: normalisedConceptId,
        stage,
        kind: chosenKind,
        parentId: parentId || null,
        parentDecision: createOpts?.parentDecision || null,
        suggestionCount: Array.isArray(createProposal?.parentSuggestions) ? createProposal.parentSuggestions.length : 0,
        explicitSuggestionCount: provenanceCounts.explicitCount,
        implicitSuggestionCount: provenanceCounts.implicitCount,
        unknownSuggestionCount: provenanceCounts.unknownCount,
        selectedParentProvenance: createOpts?.selectedParentProvenance || null,
        lowConfidenceProposal: !!createProposal?.lowConfidence,
        selectedParentSuggested: !!createOpts?.selectedParentSuggested,
        selectedParentConfidence: createOpts?.selectedParentConfidence ?? null,
        error: error ? String(error.message || error) : null
    };
}

async function emitCreateDecisionTelemetry(deps, payload) {
    if (!payload) return;
    try {
        const trackCreateDecisionFn = deps?.trackCreateDecisionFn;
        if (typeof trackCreateDecisionFn === 'function') {
            await trackCreateDecisionFn(payload);
            return;
        }
        console.info('[selectConceptById] create telemetry', payload);
    } catch (err) {
        console.warn('[selectConceptById] telemetry dispatch failed', err);
    }
}

async function ensureParentChainExistsForCreate({
    conceptId,
    createOpts,
    fallbackKind,
    detail,
    fetchFn,
    chooseCreateOptionsFn,
    fetchCreateProposalFn,
    stack = []
}) {
    const id = normaliseVontologyId(conceptId);
    const parentId = normaliseVontologyId(createOpts?.parentId || '');
    if (!id || !parentId) return;
    if (parentId === '#V#thing' || parentId === '#V#predicate') return;
    if (parentId === id) {
        throw new Error('Parent concept cannot equal concept ID.');
    }
    if (stack.includes(parentId)) {
        throw new Error('Circular parent chain detected.');
    }

    const exists = await conceptExists(parentId, fetchFn);
    if (exists) return;

    const nextStack = [...stack, id];
    const parentKind = normaliseKind(fallbackKind) === 'predicate' ? 'predicate' : 'type';
    const breadcrumbs = [...nextStack, parentId];
    const recursiveDetail = {
        conceptId: parentId,
        kind: parentKind,
        modifierKeys: detail?.modifierKeys || {},
        proposalContext: detail?.proposalContext || null,
        createProposal: {
            proposedName: deriveNameFromConceptId(parentId),
            parentSuggestions: [],
            breadcrumbs
        }
    };
    const recursiveProposal = await buildCreateProposalForConcept(
        parentId,
        parentKind,
        recursiveDetail,
        fetchFn,
        fetchCreateProposalFn
    );
    const recursiveCreateOpts = chooseCreateOptionsFn
        ? await chooseCreateOptionsFn({
            conceptId: parentId,
            kind: parentKind,
            modifierKeys: detail?.modifierKeys || {},
            isRecursiveParentCreate: true,
            childConceptId: id,
            proposal: recursiveProposal
        })
        : await openCreateConceptModal(parentId, parentKind, recursiveProposal);
    if (!recursiveCreateOpts) {
        throw new Error(`Parent creation cancelled for ${parentId}`);
    }

    await ensureParentChainExistsForCreate({
        conceptId: parentId,
        createOpts: recursiveCreateOpts,
        fallbackKind: parentKind,
        detail: recursiveDetail,
        fetchFn,
        chooseCreateOptionsFn,
        fetchCreateProposalFn,
        stack: nextStack
    });
    await createConceptForId(parentId, recursiveCreateOpts, fetchFn);
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
 * - chooseCreateOptionsFn(payload)
 * - fetchCreateProposalFn(payload)
 * - trackCreateDecisionFn(payload)
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
    const openConceptTab = (displayName) => {
        if (detail?.promoteExistingTab) {
            deps.createOrActivateConceptTab(id, displayName, shouldActivate, {
                promoteExistingTab: true
            });
            return;
        }
        deps.createOrActivateConceptTab(id, displayName, shouldActivate);
    };

    // Open the tab immediately (optimistic) so the UI responds even if the backend
    // is busy (e.g., single-threaded server while chat generation is in flight).
    // Metadata will be hydrated below when available.
    try {
        openConceptTab('Loading…');
    } catch (_) {
        // Best-effort; keep going.
    }

    let exists = false;
    try {
        exists = await conceptExists(id, fetchFn);
    } catch (err) {
        console.warn('[selectConceptById] existence check failed', err);
        // Fall back to existing behaviour: ensure the tab exists.
        openConceptTab('Loading…');
        return;
    }

    if (exists) {
        const metadata = await fetchConceptMetadata(id, fetchFn);
        updateCartouchesForConcept(id, metadata);
        openConceptTab(metadata?.displayName || id);
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
    const fetchCreateProposalFn = deps?.fetchCreateProposalFn;
    let createProposal = deriveCreateProposal(id, detail);
    if (shouldHydrateCreateProposal(detail, chooseCreateOptionsFn, deps)) {
        try {
            createProposal = await buildCreateProposalForConcept(
                id,
                kind,
                detail,
                fetchFn,
                fetchCreateProposalFn
            );
        } catch (err) {
            console.warn('[selectConceptById] proposal hydration failed', err);
            createProposal = deriveCreateProposal(id, detail);
        }
    }

    const createOpts = chooseCreateOptionsFn
        ? await chooseCreateOptionsFn({ conceptId: id, kind, modifierKeys, proposal: createProposal })
        : await openCreateConceptModal(id, kind, createProposal);
    if (!createOpts) {
        await emitCreateDecisionTelemetry(deps, buildCreateDecisionTelemetryPayload({
            conceptId: id,
            stage: 'cancelled',
            createOpts: null,
            createProposal
        }));
        try {
            deps?.closeDynamicConceptTab?.(id);
        } catch (_) {
            // Best-effort; keep going.
        }
        return;
    }

    // Re-open a loading tab now that the user has confirmed creation.
    try {
        openConceptTab('Loading…');
    } catch (_) {
        // Best-effort; keep going.
    }

    await emitCreateDecisionTelemetry(deps, buildCreateDecisionTelemetryPayload({
        conceptId: id,
        stage: 'confirmed',
        createOpts,
        createProposal
    }));

    try {
        const chosenKind = deriveKindFromCreateOptions(createOpts, kind);
        showToast(`Creating ${kindLabel(chosenKind)}…`, 'info');
        try {
            const existsAfterConfirmation = await conceptExists(id, fetchFn);
            if (existsAfterConfirmation) {
                const existingMetadata = await fetchConceptMetadata(id, fetchFn);
                updateCartouchesForConcept(id, existingMetadata);
                openConceptTab(existingMetadata?.displayName || id);
                showToast('Concept already exists; opened existing concept.', 'info');
                await emitCreateDecisionTelemetry(deps, buildCreateDecisionTelemetryPayload({
                    conceptId: id,
                    stage: 'revisited_existing',
                    createOpts,
                    createProposal
                }));
                return;
            }
        } catch (_) {
            // If re-check fails, proceed with create and rely on idempotent 409 handling.
        }
        await ensureParentChainExistsForCreate({
            conceptId: id,
            createOpts,
            fallbackKind: kind,
            detail,
            fetchFn,
            chooseCreateOptionsFn,
            fetchCreateProposalFn,
            stack: []
        });
        await createConceptForId(id, createOpts, fetchFn);
        const metadata = await fetchConceptMetadata(id, fetchFn);
        updateCartouchesForConcept(id, metadata);
        openConceptTab(metadata?.displayName || id);
        showToast('Concept created.', 'info');
        await emitCreateDecisionTelemetry(deps, buildCreateDecisionTelemetryPayload({
            conceptId: id,
            stage: 'succeeded',
            createOpts,
            createProposal
        }));
    } catch (err) {
        const message = (err && err.message) ? String(err.message) : 'Unknown error';
        console.warn('[selectConceptById] create failed', err);
        if (message.toLowerCase().includes('cancelled')) {
            showToast('Creation cancelled.', 'info');
        } else {
            showToast(`Failed to create concept: ${message}`, 'error');
        }
        await emitCreateDecisionTelemetry(deps, buildCreateDecisionTelemetryPayload({
            conceptId: id,
            stage: 'failed',
            createOpts,
            createProposal,
            error: err
        }));
    }
}
