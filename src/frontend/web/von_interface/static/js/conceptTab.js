import { deleteJson, getJson, patchJson, postJson, putJson } from './apiService.js';
import { elements, getCurrentUserConceptId, getUserClientId } from './domUtils.js';
import { populateLanguageSelect } from './languageConfig.js';
import { simpleMarkdownToHtml } from './markdownUtils.js';
import { checkDuplicateName, normalizeConceptName, recordNameNormalizationMetric } from './nameUtils.js';
import {
  defaultSelectedConceptType,
  getConceptTypeDisplayNames,
  getCurrentConceptType,
  getCurrentlySelectedConceptId,
  setCurrentConceptType,
  setCurrentlySelectedConceptId,
  setSelectedConceptOriginalName
} from './state.js';
import { annotateElementText } from './utils/textDecorator.js';
import { chooseBestTypeForIndividual, insertNodeIntoVontologyTree, selectVontologyNodeByIdentifier } from './vontology.js';

// Initialize concept tab state (moved to function to avoid module load issues)
export function initializeConceptTabState() {
  try {
    if (!getCurrentConceptType()) {
      setCurrentConceptType(defaultSelectedConceptType);
    }
  } catch (error) {
    console.warn('Could not initialize concept tab state:', error);
    // Fallback: set default directly without checking current state
    try {
      setCurrentConceptType(defaultSelectedConceptType);
    } catch (fallbackError) {
      console.warn('Could not set default concept type:', fallbackError);
    }
  }
}

// Global interaction state
let currentInteractionId = null;

// Save button state management
let originalNotes = '';
let updateSaveButtonStateTimeout = null;
let debouncedSaveHandler = null;
let currentNotesVersion = null; // store ETag/lastModified value if provided

// Lightweight telemetry emitter (extendable)
function emitTelemetry(eventName, data = {}) {
  try {
    const payload = { event: eventName, ts: Date.now(), ...data };
    // For now, log to console; could POST to /api/telemetry in future
    if (window?.console) console.debug('[telemetry]', payload);
  } catch (_) { /* no-op */ }
}

// Generic debounce helper (trailing)
function debounce(fn, delay = 600) {
  let timer;
  return function (...args) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), delay);
  };
}

// Relation Elicitation Functions
async function handleSuggestMissingInfo() {
  const currentConceptId = getCurrentlySelectedConceptId();
  if (!currentConceptId) {
    alert('No concept selected. Please select a concept first.');
    return;
  }

  try {
    // Get elicitation opportunities
    const response = await getJson(`/api/elicitation/opportunities/${currentConceptId}`);
    const opportunities = response.opportunities || [];

    if (opportunities.length === 0) {
      alert('No missing information suggestions available for this concept.');
      return;
    }

    // For now, just process the first opportunity
    const predicate = opportunities[0];

    // Get the question
    const questionResponse = await postJson('/api/elicitation/ask', {
      instance_id: currentConceptId,
      predicate: predicate
    });

    if (!questionResponse.question) {
      alert('Could not generate question for this relation.');
      return;
    }

    // Ask the user
    const answer = prompt(questionResponse.question);
    if (answer === null || answer.trim() === '') {
      return; // User cancelled or provided empty answer
    }

    // Submit the answer
    const submitResponse = await postJson('/api/elicitation/submit', {
      instance_id: currentConceptId,
      predicate: predicate,
      answer: answer.trim()
    });

    if (submitResponse.value) {
      alert(`Hypothesis created: ${predicate} = ${submitResponse.value} (confidence: ${submitResponse.confidence_score})`);
      // Refresh the concept view to show the new hypothesis
      loadconceptForCurrentSelection();
    } else {
      alert('Failed to create hypothesis from your answer.');
    }

  } catch (error) {
    console.error('Error in suggest missing info workflow:', error);
    alert('An error occurred while suggesting missing information.');
  }
}

function updateSaveButtonState() {
  // Throttle the function calls to prevent excessive logging
  if (updateSaveButtonStateTimeout) {
    clearTimeout(updateSaveButtonStateTimeout);
  }

  updateSaveButtonStateTimeout = setTimeout(() => {
    updateSaveButtonStateInternal();
  }, 10); // Small delay to batch multiple rapid calls
}

/**
 * Helper function to get element with suffix support
 */
function getSuffixElement(baseId, suffix = '') {
  if (!suffix) {
    return document.getElementById(baseId);
  }

  const candidates = new Set();
  candidates.add(`${baseId}_${suffix}`);
  candidates.add(`${baseId}${suffix}`);

  if (suffix.startsWith('_')) {
    const trimmed = suffix.replace(/^_+/, '');
    if (trimmed) {
      candidates.add(`${baseId}_${trimmed}`);
      candidates.add(`${baseId}__${trimmed}`);
      candidates.add(`${baseId}${trimmed.startsWith('_') ? trimmed : `_${trimmed}`}`);
    }
  } else {
    candidates.add(`${baseId}_${suffix}`);
  }

  for (const id of candidates) {
    const el = document.getElementById(id);
    if (el) return el;
  }
  return null;
}

function normaliseNameRecord(raw) {
  if (!raw) {
    return {
      name: '',
      language: 'en-NZ',
      type: 'NL',
      relationId: null,
      textValueId: null,
    };
  }
  if (typeof raw === 'string') {
    return {
      name: raw,
      language: 'en-NZ',
      type: 'NL',
      relationId: null,
      textValueId: null,
    };
  }

  const context = raw.context || {};
  const nameSource = raw.name ?? raw.text ?? '';
  const nameValue = (typeof nameSource === 'string' ? nameSource : String(nameSource || '')).trim();
  const languageSource = raw.language ?? raw.lang ?? 'en-NZ';
  const language = (typeof languageSource === 'string' ? languageSource : String(languageSource || 'en-NZ')).trim() || 'en-NZ';
  const typeSource = raw.type ?? context.name_type ?? 'NL';
  const typeCandidate = typeof typeSource === 'string' ? typeSource : String(typeSource || 'NL');
  const type = typeCandidate.trim().toUpperCase() || 'NL';

  return {
    name: nameValue,
    language,
    type,
    relationId: raw.relation_id ?? raw.relationId ?? null,
    textValueId: raw.text_value_id ?? raw.textValueId ?? null,
  };
}

function setNamesStatusMessage(suffix, message, colour, autoClear = false) {
  const namesStatus = getSuffixElement('namesStatus', suffix);
  if (!namesStatus) {
    return;
  }
  namesStatus.textContent = message;
  namesStatus.style.color = colour;
  if (autoClear) {
    const capturedMessage = message;
    setTimeout(() => {
      const latestStatus = getSuffixElement('namesStatus', suffix);
      if (latestStatus && latestStatus.textContent === capturedMessage) {
        latestStatus.textContent = '';
      }
    }, 3000);
  }
}

async function deleteTextRelationByPredicate(conceptId, predicate, text, options = {}) {
  const payload = { predicate, text };
  if (options.lang) {
    payload.lang = options.lang;
  }
  if (options.context && typeof options.context === 'object') {
    payload.context = options.context;
  }
  const res = await fetch(`/api/concepts/${encodeURIComponent(conceptId)}/texts`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return res.json();
}

function renderMissingConceptState(conceptId, suffix = '', options = {}) {
  const containerId = suffix ? `conceptTab_${suffix}` : 'conceptTab';
  const container = document.getElementById(containerId);
  if (!container) {
    return;
  }

  if (container.dataset.conceptMissing === '1') {
    return;
  }

  container.dataset.conceptMissing = '1';
  container.classList.add('concept-tab-missing');

  const messageText = options.message || 'Concept not found. It may have been deleted or is unavailable.';
  let banner = container.querySelector('.concept-missing-state');
  if (!banner) {
    banner = document.createElement('div');
    banner.className = 'concept-missing-state';
    banner.innerHTML = `
      <h3>Concept unavailable</h3>
      <p>${messageText}</p>
    `;
    container.insertBefore(banner, container.firstChild);
  } else {
    const paragraph = banner.querySelector('p');
    if (paragraph) {
      paragraph.textContent = messageText;
    }
  }

  const namesStatus = getSuffixElement('namesStatus', suffix);
  if (namesStatus) {
    namesStatus.textContent = messageText;
    namesStatus.style.color = '#b91c1c';
  }

  const conceptStatus = getSuffixElement('conceptStep1Status', suffix);
  if (conceptStatus) {
    conceptStatus.textContent = messageText;
    conceptStatus.style.color = '#b91c1c';
  }

  const interactiveElements = container.querySelectorAll('input, button, textarea, select, [contenteditable="true"]');
  interactiveElements.forEach((el) => {
    if (banner.contains(el)) {
      return;
    }
    if (el.dataset.allowWhenMissing === '1') {
      return;
    }
    try {
      el.disabled = true;
      el.setAttribute('aria-disabled', 'true');
    } catch (_) {
      /* no-op */
    }
  });

  try {
    document.dispatchEvent(new CustomEvent('concept-tab-missing', {
      detail: {
        conceptId,
        suffix,
        message: messageText
      }
    }));
  } catch (_) {
    /* ignore event dispatch errors */
  }
}

function updateSaveButtonStateWithSuffix(suffix = '') {
  const saveNotesButton = getSuffixElement('saveNotesButton', suffix);
  const conceptNotesInput = getSuffixElement('conceptNotes', suffix);
  if (!saveNotesButton) return;
  const currentNotes = conceptNotesInput ? conceptNotesInput.value : '';
  const hasChanges = currentNotes !== originalNotes;
  saveNotesButton.disabled = !hasChanges;
  saveNotesButton.textContent = 'Save';
  if (hasChanges) saveNotesButton.style.display = 'inline-block';
}

function updateSaveButtonStateInternal() {
  if (!elements.saveNotesButton) {
    console.log("updateSaveButtonState: saveNotesButton element not found");
    return;
  }
  const currentNotes = elements.conceptNotesInput ? elements.conceptNotesInput.value : '';
  const hasChanges = currentNotes !== originalNotes;
  elements.saveNotesButton.disabled = !hasChanges;
  elements.saveNotesButton.textContent = 'Save';
  if (hasChanges) elements.saveNotesButton.style.display = 'inline-block';
}

export async function fetchConceptList(conceptType) {
  const typeToUse = conceptType || getCurrentConceptType();

  if (!elements.conceptListUl) {
    console.warn("fetchConceptList: conceptListUl element not found");
    return;
  }

  console.log(`fetchConceptList called with conceptType: ${typeToUse}`);
  elements.conceptListUl.innerHTML = '<li>Loading concepts...</li>';

  try {
    const displayNames = getConceptTypeDisplayNames(typeToUse);
    // Use the concepts API that includes descendant instances
    const apiIdentifier = typeToUse || displayNames.apiType;
    const [conceptResponse, settingsResponse] = await Promise.all([
      getJson(`/api/concepts?concept_id=${encodeURIComponent(apiIdentifier)}&per_page=50`),
      getJson('/api/settings/')
    ]);

    elements.conceptListUl.innerHTML = '';

    if (conceptResponse.concepts && conceptResponse.concepts.length > 0) {
      const needsScroll = conceptResponse.concepts.length > 10;
      let listParent = elements.conceptListUl;
      if (needsScroll) {
        // If not already wrapped, create a scroll container and move UL inside
        if (!elements.conceptListUl.parentElement.classList.contains('vontology-instances-scroll')) {
          const wrapper = document.createElement('div');
          wrapper.className = 'vontology-instances-scroll';
          elements.conceptListUl.parentElement.insertBefore(wrapper, elements.conceptListUl);
          wrapper.appendChild(elements.conceptListUl);
          listParent = wrapper;
        }
      } else {
        // If list shrank below threshold and is wrapped, unwrap
        const parent = elements.conceptListUl.parentElement;
        if (parent && parent.classList && parent.classList.contains('vontology-instances-scroll')) {
          parent.parentElement.insertBefore(elements.conceptListUl, parent);
          try { parent.remove(); } catch (_) { }
        }
      }
      // Use browser-local preferences (no longer provided by backend settings)
      let currentUserPersonId = null;
      let currentOrganisationId = null;
      try { const storedUser = JSON.parse(localStorage.getItem('von_current_user') || 'null'); currentUserPersonId = storedUser?.id || null; } catch { }
      try { const storedOrg = JSON.parse(localStorage.getItem('von_current_org') || 'null'); currentOrganisationId = storedOrg?.id || null; } catch { }

      conceptResponse.concepts.forEach(concept => {
        const li = document.createElement('li');
        li.className = 'concept-list-item';

        // Build a compact button using CSS class styling
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'concept-item-button';

        // Display name plus optional direct type
        const displayNames = getConceptTypeDisplayNames(typeToUse);
        let displayName = concept.name;
        if (concept.direct_concept_name && concept.direct_concept_name !== displayNames.singular) {
          displayName += ` [${concept.direct_concept_name}]`;
        }

        // Personal/Org highlighting via emoji prefix
        const isCurrentUserconcept = currentUserPersonId && (
          concept.person_id === currentUserPersonId ||
          concept.creator_id === currentUserPersonId ||
          concept.user_id === currentUserPersonId ||
          concept._id === currentUserPersonId
        );
        const isCurrentOrgconcept = currentOrganisationId && (
          concept.organisation_id === currentOrganisationId ||
          concept.creator_id === currentOrganisationId ||
          concept._id === currentOrganisationId
        );

        let buttonLabel = displayName;
        if (isCurrentUserconcept) buttonLabel = `👤 ${buttonLabel}`;
        else if (isCurrentOrgconcept) buttonLabel = `🏢 ${buttonLabel}`;
        if (Array.isArray(concept.system_tags) && concept.system_tags.includes('demo_data')) {
          btn.innerHTML = `<del>${buttonLabel}</del> [test data]`;
          btn.style.color = '#888';
        } else {
          // Add a small "Direct" badge if this is a direct instance of the current type
          const isDirect = concept.direct_concept_name === displayNames.singular || !concept.direct_concept_name;
          if (isDirect) {
            const wrapper = document.createElement('span');
            wrapper.textContent = buttonLabel + ' ';
            const badge = document.createElement('span');
            badge.textContent = 'Direct';
            badge.className = 'concept-direct-badge';
            badge.style.marginLeft = '6px';
            badge.style.padding = '0 6px';
            badge.style.border = '1px solid #3b82f6';
            badge.style.borderRadius = '10px';
            badge.style.background = '#e6f0ff';
            badge.style.color = '#1d4ed8';
            badge.style.fontSize = '0.75em';
            wrapper.appendChild(badge);
            btn.innerHTML = '';
            btn.appendChild(wrapper);
          } else {
            btn.textContent = buttonLabel;
          }
        }

        // On click, open a NEW tab for this individual concept (do not mutate current tab)
        btn.addEventListener('click', () => {
          const conceptId = concept.concept_id || concept._id;
          const conceptName = concept.name || 'Concept';
          if (!conceptId) return;
          const event = new CustomEvent('open-concept-tab', {
            detail: { conceptId, conceptName, kind: 'individual', activate: true }
          });
          document.dispatchEvent(event);
        });

        li.appendChild(btn);
        elements.conceptListUl.appendChild(li);
      });
    } else {
      // If wrapped previously, unwrap to avoid empty scroll box
      const wrapParent = elements.conceptListUl.parentElement;
      if (wrapParent && wrapParent.classList && wrapParent.classList.contains('vontology-instances-scroll')) {
        wrapParent.parentElement.insertBefore(elements.conceptListUl, wrapParent);
        try { wrapParent.remove(); } catch (_) { }
      }
      elements.conceptListUl.innerHTML = `<li>No ${displayNames.plural.toLowerCase()} found.</li>`;
    }

    console.log(`Loaded ${conceptResponse.concepts?.length || 0} entities of type ${displayNames.singular}`);
    // Restore previously selected radio if applicable
    // No radio selection to restore in button list

  } catch (error) {
    console.error('Error in fetchConceptList:', error);
    elements.conceptListUl.innerHTML = '<li>Error loading concepts. Please try again.</li>';
  }
}

export function updateConceptTabUI() {
  const displayNames = getConceptTypeDisplayNames(getCurrentConceptType());
  // Determine whether current tab is a Type or Individual; default to 'type'
  let tabKind = 'type';
  try {
    const el = elements.conceptTypeDisplayNamePluralElement;
    if (el && el.id && el.id.startsWith('conceptTypeDisplayNamePluralElement_')) {
      const suffix = el.id.substring('conceptTypeDisplayNamePluralElement_'.length);
      const container = document.getElementById(`conceptTab_${suffix}`);
      if (container && container.dataset && container.dataset.tabKind) {
        tabKind = container.dataset.tabKind;
      }
    }
  } catch (_) { /* noop */ }
  const headerName = tabKind === 'individual' ? displayNames.singular : displayNames.plural;

  console.log('[updateConceptTabUI] Called. currentConceptType:', getCurrentConceptType());
  console.log('[updateConceptTabUI] Retrieved displayNames:', displayNames);

  // Update main display elements
  // NOTE: Do NOT display the selected Vontology type name in the header. That
  // information can be misleading (it reflects a selected type in the tree,
  // not the concept's actual instance_of relations). Keep the header span
  // present for badges, the #V# id chip and the JSON button (dynamicTabs
  // attaches those). Remove only accidental textual content.
  if (elements.conceptTypeDisplayNameElement) {
    // Keep the Instances-section display name updated (plural form).
    // This does not reintroduce the misleading main header label — it
    // only updates the 'Instances <span id="conceptTypeDisplayNameElement">'
    // text, which is useful and expected by tests/UI.
    try {
      elements.conceptTypeDisplayNameElement.textContent = displayNames.plural;
    } catch (e) {
      console.warn('Could not set conceptTypeDisplayNameElement text:', e);
    }
  }

  if (elements.conceptTypeDisplayNamePluralElement) {
    // Ensure header span exists but do not populate it with the selected type name.
    // dynamicTabs will insert ID chip and buttons; remove any stray text nodes.
    try {
      Array.from(elements.conceptTypeDisplayNamePluralElement.childNodes).forEach(node => {
        if (node.nodeType === Node.TEXT_NODE && node.textContent && node.textContent.trim()) {
          elements.conceptTypeDisplayNamePluralElement.removeChild(node);
        }
      });
    } catch (e) {
      // If anything goes wrong, don't block the UI; leave the element as-is
      console.warn('Could not clean header text nodes:', e);
    }
  }

  // Name input removed; no placeholder updates needed

  // Update status message if no concept selected
  if (elements.conceptStep1Status && !getCurrentlySelectedConceptId()) {
    elements.conceptStep1Status.textContent = `Select a ${displayNames.singular.toLowerCase()} or create a new one.`;
    elements.conceptStep1Status.style.color = "grey";
  }

  // Update button tooltips and make Add button visible
  if (elements.addNewConceptButton) {
    elements.addNewConceptButton.title = `Create new ${displayNames.singular.toLowerCase()}`;
    elements.addNewConceptButton.style.display = 'inline-block'; // Make the button visible
    elements.addNewConceptButton.onclick = () => {
      resetConceptTab();
      // After reset, user can add a name via the Names section
    };
  }

  // Removed legacy header delete button

  console.log(`concept Tab UI updated for type: ${displayNames.singular}`);
}

// Suffix-aware version for dynamic tabs
export function selectConceptWithSuffix(concept, suffix = '', radioId = null) {
  console.log(`Selecting concept with suffix "${suffix}":`, concept);

  // Get suffix-aware elements
  const getSuffixElement = (baseId) => {
    const id = suffix ? `${baseId}_${suffix}` : baseId;
    return document.getElementById(id);
  };

  // If an interaction is in progress, reset the view to stop it.
  if (currentInteractionId) {
    returnToconceptView();
    const conceptStep2Div = getSuffixElement('conceptStep2Div');
    if (conceptStep2Div) {
      conceptStep2Div.style.display = 'none';
    }
    currentInteractionId = null;
  }

  // Reset delete confirmation when selecting a new concept
  resetDeleteConfirmation();

  // IMPORTANT: Use ontological concept_id (#V#...) when available so that
  // PATCH /api/concepts/:id/notes targets the correct document. The backend
  // notes endpoint historically filtered only by concept_id (not Mongo _id),
  // so storing a raw _id here caused silent save failures.
  setCurrentlySelectedConceptId(concept.concept_id || concept.id || concept._id);
  setSelectedConceptOriginalName(concept.display_name || concept.name);

  // Store original notes for change detection - for now use direct access
  // TODO: Replace with proper API call to get full concept data
  originalNotes = concept.notes || '';

  // Update form title to show editing mode
  updateconceptFormTitle(true, concept.display_name || concept.name);

  // Update UI to show selected concept - use suffix-aware elements
  // Name field removed; rely on Names section for display

  const conceptNotesInput = getSuffixElement('conceptNotes');
  if (conceptNotesInput) {
    // First try to use notes data from the concept object directly
    const notesValue = concept.notes ||
      concept.concept_data?.notes ||
      concept.concept_data?.preserved_fields?.notes ||
      concept.description ||
      '';

    console.log(`[selectConceptWithSuffix] Found notes value: "${notesValue}"`);
    conceptNotesInput.value = notesValue;
    originalNotes = notesValue;
    refreshRenderedConceptNotes();

    // If no notes found in the object, try to fetch from API as fallback
    if (!notesValue) {
      const conceptId = concept.concept_id || concept._id;
      console.log(`[selectConceptWithSuffix] No notes in object, trying API fetch for ID: "${conceptId}"`);
      fetchFullConceptData(conceptId, suffix);
    }
  }

  // Update status with suffix-aware element
  const conceptStep1Status = getSuffixElement('conceptStep1Status');
  if (conceptStep1Status) {
    conceptStep1Status.textContent = `Selected: ${concept.display_name || concept.name}`;
    conceptStep1Status.style.color = "green";
  }

  // Removed legacy header delete button (suffix-aware)

  const saveNotesButton = getSuffixElement('saveNotesButton');
  if (saveNotesButton) {
    saveNotesButton.style.display = 'inline-block';
    // Initial save button state - disabled until changes are made
    saveNotesButton.disabled = true;
    saveNotesButton.textContent = 'Save';
  }

  // If this concept is an individual (has is_an_instance_of), try to select the best type in the Vontology tree
  try {
    const instanceOf = concept.relationships?.is_an_instance_of || concept.is_an_instance_of || [];
    let candidates = [];
    if (typeof instanceOf === 'string') candidates = [instanceOf];
    else if (Array.isArray(instanceOf)) candidates = instanceOf;

    if (candidates.length > 0) {
      // Choose best type using backend counts (run async without blocking)
      (async () => {
        try {
          const best = await chooseBestTypeForIndividual(candidates);
          if (best) {
            // Use the centralized selection helper which handles raw-tree fallback and visuals
            await selectVontologyNodeByIdentifier(best, /*createConceptTab*/ false);
          }
        } catch (e) {
          console.warn('Error choosing best type:', e);
        }
      })();
    }
  } catch (e) {
    console.warn('Could not auto-select type for individual:', e);
  }

  // If a radioId was provided (from a click), ensure it's checked (helps when lists refresh mid-action)
  if (radioId) {
    const clickedRadio = document.getElementById(radioId);
    if (clickedRadio) clickedRadio.checked = true;
  } else {
    // Otherwise, re-check based on currently selected ID within this tab
    const listUl = getSuffixElement('conceptListUl');
    if (listUl) {
      const currentId = concept.id || concept._id;
      const nameSelector = suffix ? `input[name="selectedconcept_${suffix}"]` : 'input[name="selectedconcept"]';
      const radioToCheck = Array.from(listUl.querySelectorAll(nameSelector)).find(r => r.value === currentId);
      if (radioToCheck) radioToCheck.checked = true;
    }
  }

  // Load names for the selected concept
  const conceptId = concept.concept_id || concept._id;
  if (conceptId) {
    loadConceptNames(conceptId, suffix);
  }
}

/**
 * Fetch full concept data from API and populate notes field
 * @param {string} conceptId - The concept ID
 * @param {string} suffix - The unique suffix for element IDs
 */
async function fetchFullConceptData(conceptId, suffix = '') {
  console.log(`[fetchFullConceptData] Called with conceptId: "${conceptId}", suffix: "${suffix}"`);

  if (!conceptId) {
    console.error('[fetchFullConceptData] No concept ID provided');
    return;
  }

  try {
    const getSuffixElement = (baseId) => {
      const id = suffix ? `${baseId}_${suffix}` : baseId;
      return document.getElementById(id);
    };

    // URL encode the concept ID to handle special characters like #
    const encodedConceptId = encodeURIComponent(conceptId);

    const response = await fetch(`/api/concepts/${encodedConceptId}`);

    if (!response.ok) {
      console.error(`Failed to fetch concept data: ${response.status}`);
      return;
    }

    const conceptData = await response.json();
    const conceptNotesInput = getSuffixElement('conceptNotes');

    if (conceptNotesInput && conceptData) {
      // Use the proper notes field from the API response
      const notes = conceptData.notes ||
        conceptData.concept_data?.notes ||
        conceptData.concept_data?.preserved_fields?.notes ||
        '';
      conceptNotesInput.value = notes;
      originalNotes = notes;
      refreshRenderedConceptNotes();
    }
  } catch (error) {
    console.error('Error fetching full concept data:', error);
  }
}

export function selectConcept(concept, radioId = null) {
  // Default behavior for original concept tab (no suffix)
  selectConceptWithSuffix(concept, '', radioId);
}

// Helper function to update form title
function updateconceptFormTitle(isEditing = false, conceptName = '') {
  const titleText = document.getElementById('conceptFormTitleText');
  if (titleText) {
    if (isEditing) {
      titleText.textContent = `Editing concept: ${conceptName}`;
    } else {
      titleText.textContent = 'Select or Create Concept';
    }
  }
}

export async function resetConceptTab(suffix = '', radioId = null) {
  console.log("Resetting concept tab...");

  // Reset form title to creation mode
  updateconceptFormTitle(false);

  // Reset delete confirmation
  resetDeleteConfirmation();

  // Clear state
  setCurrentlySelectedConceptId(null);
  setSelectedConceptOriginalName(null);
  originalNotes = '';
  currentInteractionId = null; // Also reset the interaction ID

  // Clear form inputs
  // Name field removed
  if (elements.conceptNotesInput) elements.conceptNotesInput.value = '';

  // Clear radio button selections (suffix-aware)
  if (suffix) {
    const container = document.getElementById(`conceptListUl_${suffix}`) || document;
    container.querySelectorAll(`input[name="selectedconcept_${suffix}"]`).forEach(radio => {
      radio.checked = false;
    });
  } else {
    document.querySelectorAll('input[name="selectedconcept"]').forEach(radio => {
      radio.checked = false;
    });
  }
  // Re-check the clicked radio if provided
  if (radioId) {
    const clicked = document.getElementById(radioId);
    if (clicked) {
      clicked.checked = true;
    }
  }

  // Reset UI elements
  if (elements.conceptStep1Status) {
    const displayNames = getConceptTypeDisplayNames(getCurrentConceptType());
    elements.conceptStep1Status.textContent = `Select a ${displayNames.singular.toLowerCase()} or create a new one.`;
    elements.conceptStep1Status.style.color = "grey";
  }

  // Hide/show appropriate buttons
  // Removed legacy header delete button (no display toggle)

  if (elements.saveNotesButton) {
    elements.saveNotesButton.style.display = 'inline-block';
    elements.saveNotesButton.disabled = true; // Disable until user enters content
    elements.saveNotesButton.textContent = 'Save';
  }

  // Clear selection from list
  document.querySelectorAll('.concept-list-item').forEach(item => {
    item.classList.remove('selected');
  });

  // Show step 1 and hide interaction steps
  if (elements.conceptStep1Div) elements.conceptStep1Div.style.display = 'block';
  if (elements.conceptStep2Div) elements.conceptStep2Div.style.display = 'none';
  if (elements.conceptStep3Div) elements.conceptStep3Div.style.display = 'none';

  // Clear names display
  await displayConceptNames([], suffix);
}

export async function handleSaveConceptOrNotes(suffix = '') {
  // Use suffix-aware elements if suffix is provided, otherwise use global elements
  const conceptNotesInput = suffix ? getSuffixElement('conceptNotes', suffix) : elements.conceptNotesInput;
  const conceptStep1Status = suffix ? getSuffixElement('conceptStep1Status', suffix) : elements.conceptStep1Status;
  const notes = conceptNotesInput?.value.trim();
  const saveBtn = suffix ? getSuffixElement('saveNotesButton', suffix) || elements.saveNotesButton : elements.saveNotesButton;

  function setSavingUI(msg = 'Saving...') {
    if (conceptStep1Status) {
      conceptStep1Status.textContent = msg;
      conceptStep1Status.style.color = 'blue';
    }
    if (saveBtn) {
      saveBtn.disabled = true;
      saveBtn.textContent = 'Saving...';
      saveBtn.dataset.originalLabel = saveBtn.dataset.originalLabel || 'Save';
    }
  }

  function setSavedUI(msg = 'Notes saved.') {
    if (conceptStep1Status) {
      conceptStep1Status.textContent = msg;
      conceptStep1Status.style.color = 'green';
    }
    if (saveBtn) {
      saveBtn.disabled = true;
      saveBtn.textContent = 'Saved';
      setTimeout(() => {
        // revert after brief confirmation if unchanged
        if (saveBtn && saveBtn.textContent === 'Saved') {
          saveBtn.textContent = saveBtn.dataset.originalLabel || 'Save';
        }
      }, 1200);
    }
  }

  function setNoChangesUI() {
    if (conceptStep1Status) {
      conceptStep1Status.textContent = 'No changes to save';
      conceptStep1Status.style.color = 'grey';
    }
    if (saveBtn) {
      saveBtn.disabled = true;
      saveBtn.textContent = saveBtn.dataset.originalLabel || 'Save';
    }
  }

  try {
    const displayNames = getConceptTypeDisplayNames(getCurrentConceptType());
    const currentlySelectedconceptId = getCurrentlySelectedConceptId();
    const isCreatingNew = !currentlySelectedconceptId;
    const isVontologyIdentifier = typeof currentlySelectedconceptId === 'string' && currentlySelectedconceptId.startsWith('#V#');
    const hasNotesChanged = (notes || '') !== (originalNotes || '');

    // Build payload - only include concept_id for new entities
    const payload = { notes: notes || '', client_id: getUserClientId() };

    // Only set concept_id when creating a new concept
    // For existing entities, preserve their current concept_id
    if (isCreatingNew) {
      // ONTOLOGICAL NOTE: concept tab creates INDIVIDUALS, not types
      // ===========================================================
      // Individuals are distinguished from types by having NO is_a_type_of relationships.
      // They only have is_an_instance_of relationships to their type.
      // Types can be both subtypes AND instances, but individuals are never subtypes.
      // Example: "myDocument.txt" is_an_instance_of "ComputerFile" (individual)
      //          vs "ComputerFile" is_a_type_of "DigitalObject" (type)

      // Create new individual concept via vontology API
      // Determine a safe parent_id: prefer the current concept type if it's a #V# Type,
      // else fall back to the tab's concept type derived from the suffix (if available).
      let parentIdForCreate = getCurrentConceptType();
      if (!parentIdForCreate || typeof parentIdForCreate !== 'string' || !parentIdForCreate.startsWith('#V#')) {
        // Try to infer from the suffix-specific header element that carries the type id
        try {
          const headerSpan = suffix ? document.getElementById(`conceptTypeDisplayNamePluralElement_${suffix}`) : null;
          const inferred = headerSpan?.dataset?.conceptId;
          if (inferred && inferred.startsWith('#V#')) parentIdForCreate = inferred;
        } catch (e) {
          // no-op, keep existing
        }
      }
      const result = await postJson('/vontology/api/vontology/create_concept', {
        new_concept_name: `Concept ${new Date().toISOString().slice(0, 10)}`,
        parent_id: parentIdForCreate,
        create_as_instance: true,  // TRUE = individual (no subtype relationships), FALSE = type
        // Persist initial notes entered by the user during creation
        notes: notes || ''
      });

      if (result && result.success) {
        const newConceptId = result.concept_id;
        setCurrentlySelectedConceptId(newConceptId);
        setSelectedConceptOriginalName('(unnamed)');
        if (conceptStep1Status) {
          conceptStep1Status.textContent = `${displayNames.singular} created successfully!`;
          conceptStep1Status.style.color = "green";
        }

        // Refresh the concept list (suffix-aware if in dynamic tab)
        if (suffix) {
          const hasConceptList = !!document.getElementById(`conceptListUl_${suffix}`);
          if (hasConceptList) {
            await fetchConceptListWithSuffix(getCurrentConceptType(), suffix);
            // Find and select the new concept in the refreshed list
            const entitiesResponse = await getJson(`/api/concepts?concept_id=${encodeURIComponent(getCurrentConceptType())}&per_page=50`);
            const entities = entitiesResponse.concepts || [];
            const newconcept = entities.find(e => (e.concept_id === newConceptId) || (e._id === newConceptId));
            if (newconcept) {
              selectConceptWithSuffix(newconcept, suffix);
            }
          } else {
            console.debug(`Skipping concept list refresh after create for suffix ${suffix}: no conceptListUl element (individual tab)`);
          }
        } else {
          await fetchConceptList(getCurrentConceptType());
          // Find and select the new concept
          const entitiesResponse = await getJson(`/api/concepts?concept_id=${encodeURIComponent(getCurrentConceptType())}&per_page=50`);
          const entities = entitiesResponse.concepts || [];
          const newconcept = entities.find(e => (e.concept_id === newConceptId) || (e._id === newConceptId));
          if (newconcept) {
            selectConcept(newconcept);
          }
        }
        return;
      } else {
        throw new Error(result?.message || 'Failed to create concept');
      }
    }

    let result;
    let newconcept = null;

    if (!isCreatingNew) {
      // Update existing concept notes only via dedicated PATCH endpoint for reliability
      if (!hasNotesChanged) {
        setNoChangesUI();
        return;
      }
      setSavingUI();
      const startTime = performance.now?.() || Date.now();
      emitTelemetry('notes_save_start', { id: currentlySelectedconceptId });
      try {
        const headers = currentNotesVersion ? { 'If-Match': currentNotesVersion } : {};
        result = await patchJson(`/api/concepts/${encodeURIComponent(currentlySelectedconceptId)}/notes`, { notes }, { headers });
        emitTelemetry('notes_save_patch_success', { id: currentlySelectedconceptId });
      } catch (e) {
        // Fallback to legacy PUT if PATCH route not available
        emitTelemetry('notes_save_patch_fail', { id: currentlySelectedconceptId, error: e.message });
        try {
          const headers = currentNotesVersion ? { 'If-Match': currentNotesVersion } : {};
          result = await putJson(`/api/concepts/${encodeURIComponent(currentlySelectedconceptId)}`, { notes }, { headers });
          emitTelemetry('notes_save_put_fallback_success', { id: currentlySelectedconceptId });
          if (window?.console) {
            console.warn('[notes-save] PATCH failed, used PUT fallback', e);
          }
        } catch (putErr) {
          emitTelemetry('notes_save_put_fallback_fail', { id: currentlySelectedconceptId, error: putErr.message });
          if (conceptStep1Status) {
            conceptStep1Status.textContent = `Error saving: ${putErr.message}`;
            conceptStep1Status.style.color = 'red';
          }
          if (saveBtn) {
            saveBtn.disabled = false; // allow retry
            saveBtn.textContent = saveBtn.dataset.originalLabel || 'Save';
          }
          throw putErr;
        }
      } finally {
        const duration = (performance.now?.() || Date.now()) - startTime;
        emitTelemetry('notes_save_complete', { id: currentlySelectedconceptId, duration_ms: Math.round(duration) });
        if (window?.console) {
          console.debug(`[notes-save] duration=${duration.toFixed(0)}ms`);
        }
      }
      setSavedUI(`${displayNames.singular} notes saved.`);
      // Update version if server returned one
      if (result && (result.etag || result.last_modified || result.version)) {
        currentNotesVersion = result.etag || result.last_modified || result.version;
      }
    }

    // Update original values after successful save
    setSelectedConceptOriginalName('(unnamed)');
    originalNotes = notes || '';  // Update original notes

    // Update save button state to reflect no changes
    if (suffix) {
      updateSaveButtonStateWithSuffix(suffix);
    } else {
      updateSaveButtonState();
    }

    // Refresh the concept list (suffix-aware if needed) and restore selection
    if (suffix) {
      const hasConceptList = !!document.getElementById(`conceptListUl_${suffix}`);
      if (hasConceptList) {
        await fetchConceptListWithSuffix(getCurrentConceptType(), suffix);
        // Ensure the just-saved concept remains selected
        const listUl = document.getElementById(`conceptListUl_${suffix}`);
        if (listUl) {
          const radio = listUl.querySelector(`input[name="selectedconcept_${suffix}"][value="${CSS.escape(getCurrentlySelectedConceptId())}"]`);
          if (radio) radio.checked = true;
        }
      } else {
        console.debug(`Skipping concept list refresh for suffix ${suffix}: no conceptListUl element (individual tab)`);
      }
      if (newconcept) {
        selectConceptWithSuffix(newconcept, suffix);
      }
    } else {
      await fetchConceptList(getCurrentConceptType());
      if (newconcept) {
        selectConcept(newconcept);
      } else {
        // Re-check selection in default tab
        const listUl = elements.conceptListUl;
        if (listUl) {
          const radio = listUl.querySelector(`input[name="selectedconcept"][value="${CSS.escape(getCurrentlySelectedConceptId())}"]`);
          if (radio) radio.checked = true;
        }
      }
    }

  } catch (error) {
    console.error('Error saving concept:', error);
    if (conceptStep1Status) {
      conceptStep1Status.textContent = `Error saving: ${error.message}`;
      conceptStep1Status.style.color = "red";
    }
  }
}

// Track delete confirmation state
let deleteConfirmationPending = false;
let deleteConfirmationTimeout = null;

export async function handleDeleteConcept() {
  const currentlySelectedconceptId = getCurrentlySelectedConceptId();
  if (!currentlySelectedconceptId || currentlySelectedconceptId === 'undefined') {
    console.warn("No concept selected for deletion");
    return;
  }

  const displayNames = getConceptTypeDisplayNames(getCurrentConceptType());
  // Derive a friendly name for messaging from current names list if available
  let conceptName = 'this concept';
  try {
    if (Array.isArray(currentConceptNames) && currentConceptNames.length > 0) {
      conceptName = currentConceptNames[0].text || conceptName;
    } else if (currentDisplayName) {
      conceptName = currentDisplayName;
    }
  } catch (_) { }

  if (!deleteConfirmationPending) {
    // First click - show confirmation state
    deleteConfirmationPending = true;

    // Removed legacy header delete button; confirmation UI now only via status text

    if (elements.conceptStep1Status) {
      elements.conceptStep1Status.textContent = `Click delete button again to confirm deletion of "${conceptName}"`;
      elements.conceptStep1Status.style.color = "#dc3545";
    }

    // Reset confirmation after 3 seconds
    deleteConfirmationTimeout = setTimeout(() => {
      resetDeleteConfirmation();
    }, 3000);

    return;
  }

  // Second click - proceed with deletion
  if (deleteConfirmationTimeout) {
    clearTimeout(deleteConfirmationTimeout);
  }
  resetDeleteConfirmation();

  try {
    // Ensure ID is URL-encoded to handle characters like '#'
    await deleteJson(`/api/concepts/${encodeURIComponent(currentlySelectedconceptId)}`);

    if (elements.conceptStep1Status) {
      elements.conceptStep1Status.textContent = `${displayNames.singular} deleted successfully!`;
      elements.conceptStep1Status.style.color = "green";
    }

    resetConceptTab();
    await fetchConceptList();

  } catch (error) {
    console.error('Error deleting concept:', error);
    if (elements.conceptStep1Status) {
      elements.conceptStep1Status.textContent = `Error deleting: ${error.message}`;
      elements.conceptStep1Status.style.color = "red";
    }
  }
}

function resetDeleteConfirmation() {
  deleteConfirmationPending = false;
  if (deleteConfirmationTimeout) {
    clearTimeout(deleteConfirmationTimeout);
    deleteConfirmationTimeout = null;
  }

  // Removed legacy header delete button reset
}

// Ensure the interaction Save button is visible and properly enabled/disabled
function ensureInteractionSaveButtonState() {
  const saveBtn = document.getElementById('saveNotesButtonInteraction');
  const notesContent = elements.updatedNotesContent;

  if (saveBtn && notesContent && elements.updatedNotesDisplay) {
    // Make sure the notes display is visible
    elements.updatedNotesDisplay.style.display = 'block';

    // Make sure the save button is visible
    saveBtn.style.display = 'inline-block';

    // Compare current notes with original notes to determine if button should be enabled
    const currentNotesValue = notesContent.value.trim();
    const originalNotesValue = originalNotes ? originalNotes.trim() : '';
    const hasChanged = currentNotesValue !== originalNotesValue;

    saveBtn.disabled = !hasChanged;
  }
}

// Handle start interaction button click
export async function handleStartInteraction() {
  const currentlySelectedconceptId = getCurrentlySelectedConceptId();
  console.log("handleStartInteraction called. Currently selected concept ID:", currentlySelectedconceptId);
  const displayNames = getConceptTypeDisplayNames(getCurrentConceptType());

  if (!currentlySelectedconceptId) {
    alert(`Please select a ${displayNames.singular.toLowerCase()} to start an interaction.`);
    return;
  }

  const criticalElements = [
    ['conceptStep1Status', elements.conceptStep1Status],
    ['conceptStep2Status', elements.conceptStep2Status]
  ];

  const missingCritical = criticalElements
    .filter(([, el]) => !el)
    .map(([name]) => name);

  if (missingCritical.length) {
    console.error(`[handleStartInteraction] Missing critical DOM elements: ${missingCritical.join(', ')}`);
    alert("Error: UI elements for interaction are missing. Cannot proceed.");
    return;
  }

  elements.conceptStep1Status.textContent = "Starting interaction...";
  elements.conceptStep1Status.style.color = "blue";

  // Immediately update UI for responsiveness
  // Hide step 1, show step 2
  if (elements.conceptStep1Div) elements.conceptStep1Div.style.display = 'none';
  if (elements.conceptStep2Div) {
    elements.conceptStep2Div.style.display = 'block';
    elements.conceptStep2Div.style.border = '2px solid #007cba';
    elements.conceptStep2Div.style.borderRadius = '5px';
    elements.conceptStep2Div.style.padding = '10px';
    elements.conceptStep2Div.style.backgroundColor = '#f8f9fa';
  }

  // Show and populate the notes area for editing during interaction
  if (elements.updatedNotesDisplay) {
    elements.updatedNotesDisplay.style.display = 'block';
  }
  if (elements.updatedNotesContent) {
    elements.updatedNotesContent.value = originalNotes; // Use correct notes source
    elements.updatedNotesContent.readOnly = false;

    // Always show the save button during interaction
    const saveBtn = document.getElementById('saveNotesButtonInteraction');
    if (saveBtn) {
      saveBtn.style.display = 'inline-block';
      saveBtn.disabled = true; // Initially disabled until user makes changes
    }

    // Remove any previous event listeners to avoid duplicates
    elements.updatedNotesContent.oninput = null;

    // Store the original notes value for comparison
    const originalNotesValue = originalNotes;

    elements.updatedNotesContent.addEventListener('input', () => {
      if (elements.conceptNotesInput) {
        elements.conceptNotesInput.value = elements.updatedNotesContent.value;
      }
      // Enable/disable the interaction save button based on whether notes have changed
      if (saveBtn) {
        const hasChanged = elements.updatedNotesContent.value.trim() !== originalNotesValue.trim();
        saveBtn.disabled = !hasChanged;
      }
      updateSaveButtonState();
    });

    // Initial check to see if notes are different from original
    const initiallyDifferent = elements.updatedNotesContent.value.trim() !== originalNotesValue.trim();
    if (saveBtn) {
      saveBtn.disabled = !initiallyDifferent;
    }
  }
  if (elements.updatedNotesTitle) {
    elements.updatedNotesTitle.textContent = 'concept Notes'; // Reset title
  }

  // Hide any previously displayed Q&A exchange
  if (elements.lastQADisplay) elements.lastQADisplay.style.display = 'none';

  // Clear previous question
  if (elements.followUpQuestionP) {
    annotateElementText(elements.followUpQuestionP, '');
  }
  elements.conceptStep2Status.textContent = "Generating first question...";
  elements.conceptStep2Status.style.color = "blue";

  try {
    const userClientId = getUserClientId();
    console.log("[handleStartInteraction] Sending initial_notes:", originalNotes);
    console.log("[handleStartInteraction] originalNotes length:", originalNotes.length);

    // Only send initial_notes if they are different from what's already in the database
    // to avoid duplication in the backend's note combination logic
    const requestBody = {};
    // Don't send initial_notes if they're the same as the current concept notes
    // The backend will combine them with existing notes, causing duplication
    console.log("[handleStartInteraction] Not sending initial_notes to prevent duplication");

    const response = await fetch(`/api/concepts/${encodeURIComponent(currentlySelectedconceptId)}/start_interaction`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-User-Concept-ID': getCurrentUserConceptId()
      },
      body: JSON.stringify(requestBody) // Send empty body to use only existing notes
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({ error: "Failed to start interaction. Server returned an error." }));
      throw new Error(errorData.error || `HTTP error! status: ${response.status}`);
    }

    const data = await response.json();
    currentInteractionId = data.interaction_id;

    // Now that interaction has started, ensure save button state is correct
    updateSaveButtonState();

    const questionResponse = await fetch(`/api/concepts/${encodeURIComponent(currentlySelectedconceptId)}/generate_initial_question`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-User-Concept-ID': getCurrentUserConceptId()
      },
      body: JSON.stringify({ initial_notes: originalNotes }) // Pass the same notes to question generation
    });

    if (!questionResponse.ok) {
      throw new Error(`Failed to generate initial question: ${questionResponse.status}`);
    }

    const questionData = await questionResponse.json();
    console.log("Question response data:", questionData);
    console.log("Full JSON response:", JSON.stringify(questionData, null, 2));

    // Display the generated question - properly extract from JSON response
    if (elements.followUpQuestionP) {
      let questionText = '';

      // Check different possible response structures (matching legacy script logic)
      if (typeof questionData.question === 'string') {
        questionText = questionData.question;
      } else if (typeof questionData === 'string') {
        questionText = questionData;
      } else if (questionData.question && typeof questionData.question === 'object' && questionData.question.question) {
        questionText = questionData.question.question;
      } else if (questionData.message) {
        questionText = questionData.message;
      } else {
        // Fallback - try to extract from any nested structure
        questionText = questionData.question || questionData.message || 'Question not available';
        if (typeof questionText !== 'string') {
          questionText = JSON.stringify(questionText);
        }
      }

      annotateElementText(elements.followUpQuestionP, questionText);
      elements.followUpQuestionP.style.fontWeight = 'bold';
      elements.followUpQuestionP.style.color = '#007cba';
      console.log("Displayed question:", questionText);
    }

    // Clear previous answer and enable input
    if (elements.conceptAnswerInput) {
      elements.conceptAnswerInput.value = '';
      elements.conceptAnswerInput.disabled = false;
    }

    elements.conceptStep2Status.textContent = "Question generated. Please provide your answer.";
    elements.conceptStep2Status.style.color = "green";

  } catch (error) {
    console.error("Error in handleStartInteraction:", error);
    elements.conceptStep1Status.textContent = `Error: ${error.message}`;
    elements.conceptStep1Status.style.color = "red";
  }
}

// Handle cancel interaction button click
async function handleCancelInteraction() {
  console.log("handleCancelInteraction called. Interaction ID:", currentInteractionId);

  if (!currentInteractionId) {
    console.log("No active interaction to cancel");
    return;
  }

  try {
    // Reset interaction state
    currentInteractionId = null;

    // Detect if we're in a dynamic tab by checking the active tab
    const activeTab = document.querySelector('.tab-content.active');
    let conceptStep1Status = null;
    let conceptStep3Content = null;

    if (activeTab && activeTab.id.startsWith('conceptTab__')) {
      // We're in a dynamic concept tab, extract suffix
      const suffix = activeTab.id.replace('conceptTab_', '');
      conceptStep1Status = document.getElementById(`conceptStep1Status_${suffix}`);
      conceptStep3Content = document.getElementById(`conceptStep3Content_${suffix}`);
    } else {
      // We're in the regular concept tab
      const elements = getConceptTabElements();
      conceptStep1Status = elements.conceptStep1Status;
      conceptStep3Content = elements.conceptStep3Content;
    }

    // Clear any interaction UI elements
    if (conceptStep3Content) {
      conceptStep3Content.innerHTML = '';
    }
    if (conceptStep1Status) {
      conceptStep1Status.textContent = 'Interaction cancelled';
      conceptStep1Status.style.color = 'orange';
    }

    // Restore the selection view and hide interaction step
    if (typeof returnToconceptView === 'function') {
      returnToconceptView();
    }
    const step2 = document.querySelector('.tab-content.active')?.querySelector('[id^="conceptStep2" ]') || elements.conceptStep2Div;
    if (step2) step2.style.display = 'none';
    const step1 = document.querySelector('.tab-content.active')?.querySelector('[id^="conceptStep1" ]') || elements.conceptStep1Div;
    if (step1) step1.style.display = 'block';

    // Re-enable radio selection explicitly (defensive; radios aren't programmatically disabled elsewhere)
    document.querySelectorAll('input[type="radio"][name^="selectedconcept"]').forEach(r => r.disabled = false);

    // Update button states
    if (typeof ensureInteractionSaveButtonState === 'function') {
      ensureInteractionSaveButtonState();
    }

    console.log("Interaction cancelled successfully");
  } catch (error) {
    console.error("Error in handleCancelInteraction:", error);
  }
}

// Handle end interaction button click
async function handleEndInteraction() {
  console.log("handleEndInteraction called. Interaction ID:", currentInteractionId);

  if (!currentInteractionId) {
    console.log("No active interaction to end");
    return;
  }

  try {
    // Reset interaction state
    currentInteractionId = null;

    // Detect if we're in a dynamic tab by checking the active tab
    const activeTab = document.querySelector('.tab-content.active');
    let conceptStep1Status = null;

    if (activeTab && activeTab.id.startsWith('conceptTab__')) {
      // We're in a dynamic concept tab, extract suffix
      const suffix = activeTab.id.replace('conceptTab_', '');
      conceptStep1Status = document.getElementById(`conceptStep1Status_${suffix}`);
    } else {
      // We're in the regular concept tab
      const elements = getConceptTabElements();
      conceptStep1Status = elements.conceptStep1Status;
    }

    // Clear any interaction UI elements
    if (conceptStep1Status) {
      conceptStep1Status.textContent = 'Interaction ended';
      conceptStep1Status.style.color = 'blue';
    }

    // Restore the selection view and hide interaction step
    if (typeof returnToconceptView === 'function') {
      returnToconceptView();
    }
    const step2 = document.querySelector('.tab-content.active')?.querySelector('[id^="conceptStep2" ]') || elements.conceptStep2Div;
    if (step2) step2.style.display = 'none';
    const step1 = document.querySelector('.tab-content.active')?.querySelector('[id^="conceptStep1" ]') || elements.conceptStep1Div;
    if (step1) step1.style.display = 'block';

    // Re-enable radio selection explicitly
    document.querySelectorAll('input[type="radio"][name^="selectedconcept"]').forEach(r => r.disabled = false);

    // Update button states
    if (typeof ensureInteractionSaveButtonState === 'function') {
      ensureInteractionSaveButtonState();
    }

    console.log("Interaction ended successfully");
  } catch (error) {
    console.error("Error in handleEndInteraction:", error);
  }
}

// Handle submit answer button click
async function handleSubmitAnswer() {
  const currentlySelectedconceptId = getCurrentlySelectedConceptId();
  console.log("handleSubmitAnswer called. Interaction ID:", currentInteractionId);
  const answer = elements.conceptAnswerInput ? elements.conceptAnswerInput.value.trim() : "";

  // Capture the current question for display later
  const currentQuestion = elements.followUpQuestionP ? elements.followUpQuestionP.textContent : "";

  if (!currentInteractionId) {
    alert("No active interaction session.");
    return;
  }
  if (!answer) {
    alert("Please provide an answer.");
    if (elements.conceptAnswerInput) elements.conceptAnswerInput.focus();
    return;
  }

  if (!elements.conceptStep2Status || !elements.conceptStep3Div || !elements.finalResultP ||
    !elements.resetConceptTabButton || !elements.conceptStep2Div || !elements.conceptAnswerInput) {
    console.error("handleSubmitAnswer: One or more required DOM elements for concept tab interaction are missing.");
    alert("Error: UI elements for interaction are missing. Cannot proceed.");
    return;
  }

  elements.conceptStep2Status.textContent = "Submitting answer...";
  elements.conceptStep2Status.style.color = "blue";

  try {
    const userClientId = getUserClientId();
    const response = await fetch(`/api/concepts/${encodeURIComponent(currentlySelectedconceptId)}/submit_answer`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-User-Concept-ID': getCurrentUserConceptId()
      },
      body: JSON.stringify({
        interaction_id: currentInteractionId,
        answer: answer
      })
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({ error: "Failed to submit answer. Server returned an error." }));
      throw new Error(errorData.error || `HTTP error! status: ${response.status}`);
    }

    const data = await response.json();
    if (elements.conceptAnswerInput) elements.conceptAnswerInput.value = ''; // Clear the input field

    // Store the current Q&A for display
    const questionForDisplay = currentQuestion;
    const answerForDisplay = answer;
    const synthesisForDisplay = data.synthesis || "No synthesis generated";

    // Update concept notes if provided
    if (data.concept && data.concept.hasOwnProperty('notes')) {
      const newNotes = data.concept.notes || '';
      if (elements.conceptNotesInput) {
        elements.conceptNotesInput.value = newNotes;
      }
      if (elements.updatedNotesContent) {
        elements.updatedNotesContent.value = newNotes;
      }
      if (elements.updatedNotesTitle) {
        elements.updatedNotesTitle.textContent = 'Updated concept Notes';
      }
      originalNotes = newNotes; // Update originalNotes to new baseline
      refreshRenderedConceptNotes();

      // Update the interaction Save button state after AI updates
      ensureInteractionSaveButtonState();
    }

    // Always show notes during interaction, and make them editable.
    if (elements.updatedNotesDisplay && elements.updatedNotesContent) {
      elements.updatedNotesDisplay.style.display = 'block';
    }

    if (elements.saveNotesButton) {
      elements.saveNotesButton.style.display = 'inline-block';
      updateSaveButtonState();
    }

    // Display last Q&A exchange
    if (elements.lastQADisplay && elements.lastQuestion && elements.lastAnswer && elements.lastSynthesis) {
      annotateElementText(elements.lastQuestion, questionForDisplay);
      annotateElementText(elements.lastAnswer, answerForDisplay);
      annotateElementText(elements.lastSynthesis, synthesisForDisplay);
      elements.lastQADisplay.style.display = 'block';
    }

    console.log("Checking response data for next steps:");
    console.log("data.status:", data.status);
    console.log("data.next_step_content:", data.next_step_content);

    if (data.status === "completed") {
      currentInteractionId = null; // Reset interaction ID

      // Show completion message
      if (elements.conceptStep2Div) elements.conceptStep2Div.style.display = 'none';
      if (elements.conceptStep3Div) elements.conceptStep3Div.style.display = 'block';
      if (elements.finalResultP) elements.finalResultP.textContent = data.message || "Interaction completed.";

      // Set up the reset button to return to concept view
      if (elements.resetConceptTabButton) {
        elements.resetConceptTabButton.textContent = "Continue with concept";
        elements.resetConceptTabButton.onclick = returnToconceptView;
      }

      // Auto-return to concept view after a short delay
      setTimeout(() => {
        returnToconceptView();
      }, 3000);

    } else if (data.next_step_content) {
      // Display the next question - properly extract from JSON if needed
      console.log("Received next_step_content:", data.next_step_content);
      if (elements.followUpQuestionP) {
        let questionText = '';

        // Handle different formats for next_step_content
        if (typeof data.next_step_content === 'string') {
          questionText = data.next_step_content;
        } else if (data.next_step_content.question) {
          questionText = data.next_step_content.question;
        } else {
          questionText = JSON.stringify(data.next_step_content);
        }

        annotateElementText(elements.followUpQuestionP, questionText);
        elements.followUpQuestionP.style.fontWeight = 'bold';
        elements.followUpQuestionP.style.color = '#007cba';
        console.log("Set next question:", questionText);
      }

      elements.conceptStep2Status.textContent = "Answer processed. Please respond to the next question.";
      elements.conceptStep2Status.style.color = "green";

      // Ensure the Save button remains visible and properly managed
      ensureInteractionSaveButtonState();
    }

  } catch (error) {
    console.error("Error in handleSubmitAnswer:", error);
    elements.conceptStep2Status.textContent = `Error: ${error.message}`;
    elements.conceptStep2Status.style.color = "red";
  }
}

// Return to concept view after interaction completion
function returnToconceptView() {
  if (elements.conceptStep3Div) elements.conceptStep3Div.style.display = 'none';
  if (elements.conceptStep1Div) {
    elements.conceptStep1Div.style.display = 'block';
    elements.conceptStep1Div.style.border = 'none';
    elements.conceptStep1Div.style.backgroundColor = '';
  }
  if (elements.resetConceptTabButton) {
    elements.resetConceptTabButton.textContent = "Start New Interaction";
    elements.resetConceptTabButton.onclick = null;
  }

  if (elements.updatedNotesDisplay && elements.updatedNotesContent) {
    elements.updatedNotesDisplay.style.display = 'none';
    elements.updatedNotesContent.contentEditable = false;
    elements.updatedNotesContent.style.border = 'none';
    elements.updatedNotesContent.style.backgroundColor = 'transparent';
    elements.updatedNotesContent.style.padding = '0';
  }

  // Restore the original notes value to the main concept notes field
  if (elements.conceptNotesInput && originalNotes !== undefined) {
    elements.conceptNotesInput.value = originalNotes;
    console.log("Restored original notes to conceptNotesInput:", originalNotes);
  }

  // Refresh concept data to ensure we have the latest
  const currentlySelectedconceptId = getCurrentlySelectedConceptId();
  if (currentlySelectedconceptId) {
    // The concept is already selected, just refresh the display
    console.log("Returned to concept view for:", currentlySelectedconceptId);
  }
}

// Export initialization function for dynamic loading
export function initializeConceptTab() {
  console.log("Initializing concept tab...");

  // Initialize concept tab state
  initializeConceptTabState();

  // Re-initialize DOM elements specific to concept tab since they were loaded dynamically
  initializeConceptTabDomElements();

  // Set up event listeners
  setupConceptTabEventListeners();

  // Initialize names form with language options
  initializeNamesForm().catch(e => console.warn('Failed to initialize names form:', e));

  // Update UI (this will use the current concept type from state)
  updateConceptTabUI();

  // Don't call fetchConceptList() here - it will be called by loadTabData()

  console.log("concept tab initialized successfully");
}

// Initialize DOM elements specific to concept tab
function initializeConceptTabDomElements() {
  console.log("initializeConceptTabDomElements: Starting DOM element initialization...");

  // concept Tab Elements
  elements.conceptTypeDisplayNameElement = document.getElementById('conceptTypeDisplayName');
  elements.conceptTypeDisplayNamePluralElement = document.getElementById('conceptTypeDisplayNamePluralElement');
  elements.conceptStep1Div = document.getElementById('conceptStep1');
  elements.startInteractionButton = document.getElementById('startInteractionButton');
  // Name input removed (managed via Names section now)
  elements.conceptNotesInput = document.getElementById('conceptNotes');
  elements.conceptStep1Status = document.getElementById('conceptStep1Status');
  elements.conceptStep2Div = document.getElementById('conceptStep2');
  elements.followUpQuestionP = document.getElementById('followUpQuestion');
  elements.conceptAnswerInput = document.getElementById('conceptAnswer');
  elements.submitAnswerButton = document.getElementById('submitAnswerButton');
  elements.conceptStep2Status = document.getElementById('conceptStep2Status');

  // Read-only notes display elements
  elements.updatedNotesDisplay = document.getElementById('updatedNotesDisplay');
  elements.updatedNotesContent = document.getElementById('updatedNotesContent');
  elements.conceptNotesRendered = document.getElementById('conceptNotesRendered');
  elements.toggleNotesRenderButton = document.getElementById('toggleNotesRenderButton');

  // Last Q&A display elements
  elements.lastQADisplay = document.getElementById('lastQADisplay');
  elements.lastQuestion = document.getElementById('lastQuestion');
  elements.lastAnswer = document.getElementById('lastAnswer');
  elements.lastSynthesis = document.getElementById('lastSynthesis');

  elements.conceptStep3Div = document.getElementById('conceptStep3');
  elements.finalResultP = document.getElementById('finalResult');
  elements.resetConceptTabButton = document.getElementById('resetConceptTabButton');
  elements.conceptListUl = document.getElementById('conceptListUl');
  console.log("initializeConceptTabDomElements: conceptListUl element found:", elements.conceptListUl);
  elements.refreshConceptListButton = document.getElementById('refreshConceptListButton');
  elements.saveNotesButton = document.getElementById('saveNotesButton');
  elements.removeCurrentConceptButton = document.getElementById('removeCurrentConceptButton');
  elements.addNewConceptButton = document.getElementById('addNewConceptButton');
  elements.cancelInteractionButton = document.getElementById('cancelInteractionButton');
  elements.endInteractionButton = document.getElementById('endInteractionButton');
}

// Keep track of whether listeners have been added to prevent duplicates
let eventListenersAdded = false;

// Setup concept tab event listeners
export function setupConceptTabEventListeners() {
  if (eventListenersAdded) {
    console.log("Event listeners already added, skipping...");
    return;
  }

  console.log("Setting up concept tab event listeners...");

  // Name input removed: no listener needed

  // concept notes input - update save button state when notes change (debounced save)
  if (elements.conceptNotesInput) {
    elements.conceptNotesInput.addEventListener('input', () => {
      updateSaveButtonState();
      if (!debouncedSaveHandler) {
        debouncedSaveHandler = debounce(() => handleSaveConceptOrNotes(), 800);
      }
      debouncedSaveHandler();
    });
    // On blur, force immediate save if there are changes
    elements.conceptNotesInput.addEventListener('blur', () => {
      const currentNotes = elements.conceptNotesInput.value;
      if (currentNotes !== originalNotes) {
        handleSaveConceptOrNotes();
      }
    });
  }

  // Start interaction button
  if (elements.startInteractionButton) {
    elements.startInteractionButton.addEventListener('click', handleStartInteraction);
  }

  // Submit answer button
  if (elements.submitAnswerButton) {
    elements.submitAnswerButton.addEventListener('click', handleSubmitAnswer);
  }

  // Save button
  if (elements.saveNotesButton) {
    elements.saveNotesButton.addEventListener('click', handleSaveConceptOrNotes);
  }

  // Suggest Missing Info button
  if (elements.suggestInfoButton) {
    elements.suggestInfoButton.addEventListener('click', handleSuggestMissingInfo);
  }

  // Delete button
  if (elements.removeCurrentConceptButton) {
    elements.removeCurrentConceptButton.addEventListener('click', handleDeleteConcept);
  }

  // Refresh list button
  if (elements.refreshConceptListButton) {
    elements.refreshConceptListButton.addEventListener('click', () => fetchConceptList());
  }
  if (elements.conceptAnswerInput) {
    elements.conceptAnswerInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        handleSubmitAnswer();
      }
    });
  }
  if (elements.updatedNotesContent) {
    elements.updatedNotesContent.addEventListener('input', () => {
      if (elements.conceptNotesInput) {
        elements.conceptNotesInput.value = elements.updatedNotesContent.value;
      }
      refreshRenderedConceptNotes();
      updateSaveButtonState();
      if (!debouncedSaveHandler) {
        debouncedSaveHandler = debounce(() => handleSaveConceptOrNotes(), 800);
      }
      debouncedSaveHandler();
    });
    elements.updatedNotesContent.addEventListener('blur', () => {
      const currentNotes = elements.updatedNotesContent.value;
      if (currentNotes !== originalNotes) {
        handleSaveConceptOrNotes();
      }
    });
  }

  if (elements.toggleNotesRenderButton && !elements.toggleNotesRenderButton.__wired) {
    elements.toggleNotesRenderButton.__wired = true;
    elements.toggleNotesRenderButton.addEventListener('click', () => {
      // Prefer description block if present in suffixed/dynamic tab, else fallback to notes textarea logic
      const suffixMatch = elements.toggleNotesRenderButton.id && elements.toggleNotesRenderButton.id.startsWith('toggleNotesRenderButton_')
        ? elements.toggleNotesRenderButton.id.replace('toggleNotesRenderButton_', '') : '';
      const descriptionEl = suffixMatch ? document.getElementById(`conceptDescription_${suffixMatch}`) : document.getElementById('conceptDescription');
      let renderedContainer;
      if (descriptionEl && suffixMatch) {
        toggleDescriptionRenderedState(suffixMatch, elements.toggleNotesRenderButton);
        return; // Do not fall through to notes rendering
      }
      if (!elements.conceptNotesRendered) return; // legacy fallback
      const hidden = elements.conceptNotesRendered.classList.contains('hidden');
      if (hidden) {
        refreshRenderedConceptNotes();
        elements.conceptNotesRendered.classList.remove('hidden');
        elements.toggleNotesRenderButton.textContent = 'Show Raw Markdown';
      } else {
        elements.conceptNotesRendered.classList.add('hidden');
        elements.toggleNotesRenderButton.textContent = 'Show Rendered Markdown';
      }
    });
  }

  // Add event listener for the interaction save button
  const saveBtnInteraction = document.getElementById('saveNotesButtonInteraction');
  if (saveBtnInteraction) {
    saveBtnInteraction.addEventListener('click', async () => {
      const notes = elements.updatedNotesContent ? elements.updatedNotesContent.value.trim() : '';
      const currentlySelectedconceptId = getCurrentlySelectedConceptId();
      if (!currentlySelectedconceptId) return;
      try {
        try {
          await patchJson(`/api/concepts/${encodeURIComponent(currentlySelectedconceptId)}/notes`, { notes });
        } catch (e) {
          await putJson(`/api/concepts/${encodeURIComponent(currentlySelectedconceptId)}`, { notes });
        }
        if (elements.updatedNotesTitle) elements.updatedNotesTitle.textContent = 'Concept Notes (saved)';
        originalNotes = notes;
        saveBtnInteraction.disabled = true;
        updateSaveButtonState();
      } catch (error) {
        alert('Error saving notes: ' + error.message);
      }
    });
  }

  // Names management event listeners
  const addNameButton = document.getElementById('addNameButton');
  if (addNameButton) {
    addNameButton.addEventListener('click', () => addNewName());
  }

  const newNameInput = document.getElementById('newNameInput');
  if (newNameInput) {
    newNameInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        addNewName();
      }
    });
  }

  // Mark that event listeners have been added
  eventListenersAdded = true;
}

/**
 * Initialize concept tab DOM elements with unique suffix for dynamic tabs
 * @param {string} suffix - The unique suffix for element IDs
 */
export function initializeConceptTabDomElementsWithSuffix(suffix) {
  console.log(`initializeConceptTabDomElementsWithSuffix: Starting DOM element initialization with suffix: ${suffix}...`);

  // concept Tab Elements with suffix
  elements.conceptTypeDisplayNameElement = document.getElementById(`conceptTypeDisplayName_${suffix}`);
  elements.conceptTypeDisplayNamePluralElement = document.getElementById(`conceptTypeDisplayNamePluralElement_${suffix}`);
  elements.conceptStep1Div = document.getElementById(`conceptStep1_${suffix}`);
  elements.startInteractionButton = document.getElementById(`startInteractionButton_${suffix}`);
  // Name field removed for suffixed tabs
  elements.conceptNotesInput = document.getElementById(`conceptNotes_${suffix}`);
  elements.conceptStep1Status = document.getElementById(`conceptStep1Status_${suffix}`);
  elements.conceptStep2Div = document.getElementById(`conceptStep2_${suffix}`);
  elements.followUpQuestionP = document.getElementById(`followUpQuestion_${suffix}`);
  elements.conceptAnswerInput = document.getElementById(`conceptAnswer_${suffix}`);
  elements.submitAnswerButton = document.getElementById(`submitAnswerButton_${suffix}`);
  elements.cancelInteractionButton = document.getElementById(`cancelInteractionButton_${suffix}`);
  elements.endInteractionButton = document.getElementById(`endInteractionButton_${suffix}`);
  elements.conceptStep2Status = document.getElementById(`conceptStep2Status_${suffix}`);

  // Read-only notes display elements
  elements.updatedNotesDisplay = document.getElementById(`updatedNotesDisplay_${suffix}`);
  elements.updatedNotesContent = document.getElementById(`updatedNotesContent_${suffix}`);
  elements.conceptNotesRendered = document.getElementById(`conceptNotesRendered_${suffix}`);
  elements.toggleNotesRenderButton = document.getElementById(`toggleNotesRenderButton_${suffix}`);

  // Last Q&A display elements
  elements.lastQADisplay = document.getElementById(`lastQADisplay_${suffix}`);
  elements.lastQuestion = document.getElementById(`lastQuestion_${suffix}`);
  elements.lastAnswer = document.getElementById(`lastAnswer_${suffix}`);
  elements.lastSynthesis = document.getElementById(`lastSynthesis_${suffix}`);

  elements.conceptStep3Div = document.getElementById(`conceptStep3_${suffix}`);
  elements.finalResultP = document.getElementById(`finalResult_${suffix}`);
  elements.resetConceptTabButton = document.getElementById(`resetConceptTabButton_${suffix}`);
  elements.conceptListUl = document.getElementById(`conceptListUl_${suffix}`);
  elements.refreshConceptListButton = document.getElementById(`refreshConceptListButton_${suffix}`);
  elements.addNewConceptButton = document.getElementById(`addNewConceptButton_${suffix}`);

  if (elements.conceptListUl) {
    console.log(`initializeConceptTabDomElementsWithSuffix: conceptListUl element found:`, elements.conceptListUl);
  } else {
    console.warn(`initializeConceptTabDomElementsWithSuffix: conceptListUl element not found with suffix: ${suffix}`);
  }

  console.log(`initializeConceptTabDomElementsWithSuffix: DOM initialization complete for suffix: ${suffix}`);
}

/**
 * Setup concept tab event listeners with suffix support for dynamic tabs
 * @param {string} suffix - The unique suffix for element IDs
 */
export function setupConceptTabEventListenersWithSuffix(suffix = '') {
  console.log(`Setting up concept tab event listeners with suffix: ${suffix}`);

  const getSuffixElement = (baseId) => {
    const id = suffix ? `${baseId}_${suffix}` : baseId;
    return document.getElementById(id);
  };

  // Get suffix-aware elements
  // Name input removed
  const conceptNotesInput = getSuffixElement('conceptNotes');
  const startInteractionButton = getSuffixElement('startInteractionButton');
  const submitAnswerButton = getSuffixElement('submitAnswerButton');
  const cancelInteractionButton = getSuffixElement('cancelInteractionButton');
  const endInteractionButton = getSuffixElement('endInteractionButton');
  const saveNotesButton = getSuffixElement('saveNotesButton');
  const removeCurrentConceptButton = getSuffixElement('removeCurrentConceptButton');

  // concept name input - update save button state when name changes
  // No name input listener (field removed)

  // concept notes input - update save button state when notes change
  if (conceptNotesInput) {
    console.log(`Setting up notes input listener for suffix: ${suffix}, element found: ${!!conceptNotesInput}`);
    conceptNotesInput.addEventListener('input', () => {
      console.log(`Notes input changed for suffix: ${suffix}`);
      updateSaveButtonStateWithSuffix(suffix);
    });
  } else {
    console.log(`conceptNotesInput not found for suffix: ${suffix}, looking for: conceptNotes${suffix}`);
  }

  // Start interaction button
  if (startInteractionButton) {
    startInteractionButton.addEventListener('click', handleStartInteraction);
  }

  // Submit answer button
  if (submitAnswerButton) {
    submitAnswerButton.addEventListener('click', handleSubmitAnswer);
  }

  // Cancel interaction button
  if (cancelInteractionButton) {
    cancelInteractionButton.addEventListener('click', handleCancelInteraction);
  }

  // End interaction button
  if (endInteractionButton) {
    endInteractionButton.addEventListener('click', handleEndInteraction);
  }

  // Save button
  if (saveNotesButton) {
    saveNotesButton.addEventListener('click', () => handleSaveConceptOrNotes(suffix));
  }

  // Delete button
  if (removeCurrentConceptButton) {
    removeCurrentConceptButton.addEventListener('click', handleDeleteConcept);
  }

  // Direct-only filter toggle (if present in this tab)
  const directOnlyToggle = getSuffixElement('conceptDirectOnlyToggle');
  if (directOnlyToggle) {
    directOnlyToggle.addEventListener('change', async () => {
      const hasConceptList = !!document.getElementById(`conceptListUl_${suffix}`);
      if (hasConceptList) {
        await fetchConceptListWithSuffix(getCurrentConceptType(), suffix);
      } else {
        console.debug(`Direct-only toggle changed for suffix ${suffix}, but no concept list present; skipping refresh.`);
      }
    });
  }

  // Names management event listeners
  const addNameButton = getSuffixElement('addNameButton', suffix);
  if (addNameButton) {
    addNameButton.addEventListener('click', () => addNewName(suffix));
  }

  const newNameInput = getSuffixElement('newNameInput', suffix);
  if (newNameInput) {
    newNameInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        addNewName(suffix);
      }
    });
  }

  // Create subtype/instance event listeners
  const createSubtypeButton = getSuffixElement('createSubtypeButton');
  if (createSubtypeButton) {
    createSubtypeButton.addEventListener('click', () => handleCreateSubtype(suffix));
  }

  const createInstanceButton = getSuffixElement('createInstanceButton');
  if (createInstanceButton) {
    createInstanceButton.addEventListener('click', () => handleCreateInstance(suffix));
  }

  const newSubtypeInput = getSuffixElement('newSubtypeInput');
  if (newSubtypeInput) {
    newSubtypeInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        handleCreateSubtype(suffix);
      }
    });
  }

  const newInstanceInput = getSuffixElement('newInstanceInput');
  if (newInstanceInput) {
    newInstanceInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        handleCreateInstance(suffix);
      }
    });
  }

  // Markdown render toggle (was only wired for base tab; add for suffixed tabs)
  const toggleNotesRenderButton = getSuffixElement('toggleNotesRenderButton');
  if (toggleNotesRenderButton && !toggleNotesRenderButton.__wired) {
    toggleNotesRenderButton.__wired = true;
    toggleNotesRenderButton.addEventListener('click', () => {
      // Toggle description rendered state if present
      const descriptionEl = document.getElementById(`conceptDescription_${suffix}`);
      if (descriptionEl) { toggleDescriptionRenderedState(suffix, toggleNotesRenderButton); return; }
      // Fallback: notes rendering if description not present
      const notesRendered = document.getElementById(`conceptNotesRendered_${suffix}`);
      const notesTextarea = document.getElementById(`conceptNotes_${suffix}`);
      if (!notesRendered || !notesTextarea) return;
      const hidden = notesRendered.classList.contains('hidden');
      if (hidden) {
        try {
          notesRendered.innerHTML = simpleMarkdownToHtml(notesTextarea.value || '');
        } catch (e) {
          notesRendered.textContent = notesTextarea.value || '';
        }
        notesRendered.classList.remove('hidden');
        toggleNotesRenderButton.textContent = 'Show Raw Markdown';
      } else {
        notesRendered.classList.add('hidden');
        toggleNotesRenderButton.textContent = 'Show Rendered Markdown';
      }
    });
  }


  // Shared: toggle description rendered state for a given suffix
  function toggleDescriptionRenderedState(suffix, buttonEl) {
    const descId = suffix ? `conceptDescription_${suffix}` : 'conceptDescription';
    const descriptionEl = document.getElementById(descId);
    if (!descriptionEl) return;
    let renderedContainer = document.getElementById(`conceptDescriptionRendered_${suffix}`);
    if (!renderedContainer) {
      renderedContainer = document.createElement('div');
      renderedContainer.id = `conceptDescriptionRendered_${suffix}`;
      renderedContainer.className = 'concept-notes-rendered hidden';
      descriptionEl.insertAdjacentElement('afterend', renderedContainer);
    }
    const isHidden = renderedContainer.classList.contains('hidden');
    if (isHidden) {
      try {
        const raw = descriptionEl.textContent || '';
        renderedContainer.innerHTML = simpleMarkdownToHtml(raw);
      } catch (e) {
        renderedContainer.textContent = descriptionEl.textContent || '';
      }
      renderedContainer.classList.remove('hidden');
      if (buttonEl) buttonEl.textContent = 'Show Raw Markdown';
      descriptionEl.style.display = 'none';
    } else {
      renderedContainer.classList.add('hidden');
      if (buttonEl) buttonEl.textContent = 'Show Rendered Markdown';
      descriptionEl.style.removeProperty('display');
    }
  }

  // Note: ensureDescriptionRendered moved to top-level (attached to window) for dynamic tab access
  console.log(`Event listeners setup complete for suffix: ${suffix}`);
}

// Top-level helper to ensure description is rendered (export via window for non-module contexts)
function ensureDescriptionRendered(suffix) {
  const btn = document.getElementById(`toggleNotesRenderButton_${suffix}`);
  const renderedContainer = document.getElementById(`conceptDescriptionRendered_${suffix}`);
  const descEl = document.getElementById(`conceptDescription_${suffix}`);
  if (!descEl) return;
  if (!renderedContainer || renderedContainer.classList.contains('hidden')) {
    // Reuse inner scope helper if available; replicate minimal logic otherwise
    try {
      // Attempt to call existing toggle function within a temp context
      const descriptionEl = descEl; // alias
      let rc = renderedContainer;
      if (!rc) {
        rc = document.createElement('div');
        rc.id = `conceptDescriptionRendered_${suffix}`;
        rc.className = 'concept-notes-rendered hidden';
        descriptionEl.insertAdjacentElement('afterend', rc);
      }
      if (rc.classList.contains('hidden')) {
        try { rc.innerHTML = simpleMarkdownToHtml(descriptionEl.textContent || ''); } catch { rc.textContent = descriptionEl.textContent || ''; }
        rc.classList.remove('hidden');
        if (btn) btn.textContent = 'Show Raw Markdown';
        descriptionEl.style.display = 'none';
      }
    } catch (_) { /* no-op */ }
  }
}
// Expose for dynamicTabs
try { window.ensureDescriptionRendered = ensureDescriptionRendered; } catch (_) { }

/**
 * Fetch concept list with unique suffix for dynamic tabs
 * @param {string} conceptType - The concept type
 * @param {string} suffix - The unique suffix for element IDs
 */
export async function fetchConceptListWithSuffix(conceptType, suffix) {
  console.log(`fetchConceptListWithSuffix called with conceptType: ${conceptType}, suffix: ${suffix}`);

  const conceptListUl = document.getElementById(`conceptListUl_${suffix}`);
  if (!conceptListUl) {
    // This is expected for individual dynamic tabs which do not render an instances list
    console.debug(`fetchConceptListWithSuffix: no conceptListUl for suffix: ${suffix} (likely individual tab) – skipping fetch.`);
    return;
  }

  if (!conceptType) {
    console.warn('fetchConceptListWithSuffix: No concept type specified.');
    return;
  }

  conceptListUl.innerHTML = '<li>Loading concepts...</li>';

  try {
    const displayNames = getConceptTypeDisplayNames(conceptType);
    const apiIdentifier = conceptType || displayNames.apiType;
    const [conceptResponse, settingsResponse] = await Promise.all([
      getJson(`/api/concepts?concept_id=${encodeURIComponent(apiIdentifier)}&per_page=50`),
      getJson('/api/settings/')
    ]);

    conceptListUl.innerHTML = '';
    const noteEl = document.getElementById(`conceptListNote_${suffix}`) || document.getElementById('conceptListNote');
    const toggleEl = document.getElementById(`conceptDirectOnlyToggle_${suffix}`) || document.getElementById('conceptDirectOnlyToggle');
    const directOnly = !!(toggleEl && toggleEl.checked);

    if (conceptResponse?.concepts?.length > 0) {
      let currentUserPersonId = null;
      let currentOrganisationId = null;
      try { const su = JSON.parse(localStorage.getItem('von_current_user') || 'null'); currentUserPersonId = su?.id || null; } catch { }
      try { const so = JSON.parse(localStorage.getItem('von_current_org') || 'null'); currentOrganisationId = so?.id || null; } catch { }

      conceptResponse.concepts.forEach((concept) => {
        if (directOnly) {
          const isDirect = (concept.direct_concept_name === displayNames.singular);
          if (!isDirect) return;
        }
        const li = document.createElement('li');
        li.className = 'concept-list-item';

        // Build a compact button instead of a radio + label
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'concept-item-button';
        btn.classList.add('individual'); // Visual cue: individual (pure instance)
        btn.style.display = 'inline-block';
        btn.style.padding = '2px 6px';
        btn.style.margin = '2px 4px 2px 0';
        btn.style.borderRadius = '4px';
        btn.style.border = '1px solid #ccc';
        btn.style.backgroundColor = '#fafafa';
        btn.style.cursor = 'pointer';

        // Display only the concept (instance) name in the main button; type is rendered as a separate chip
        let displayName = concept.name;

        // Emoji prefix for user/org ownership
        const isCurrentUserconcept = currentUserPersonId && (
          concept.person_id === currentUserPersonId ||
          concept.creator_id === currentUserPersonId ||
          concept.user_id === currentUserPersonId ||
          concept._id === currentUserPersonId
        );
        const isCurrentOrgconcept = currentOrganisationId && (
          concept.organisation_id === currentOrganisationId ||
          concept.creator_id === currentOrganisationId ||
          concept._id === currentOrganisationId
        );
        let buttonLabel = displayName;
        if (isCurrentUserconcept) buttonLabel = `👤 ${buttonLabel}`;
        else if (isCurrentOrgconcept) buttonLabel = `🏢 ${buttonLabel}`;

        if (Array.isArray(concept.system_tags) && concept.system_tags.includes('demo_data')) {
          btn.innerHTML = `<del>${buttonLabel}</del> [test data]`;
          btn.style.color = '#888';
        } else {
          const isDirect = concept.direct_concept_name === displayNames.singular || !concept.direct_concept_name;
          if (isDirect) {
            const wrapper = document.createElement('span');
            wrapper.textContent = buttonLabel + ' ';
            const badge = document.createElement('span');
            badge.textContent = 'Direct';
            badge.className = 'concept-direct-badge';
            badge.style.marginLeft = '6px';
            badge.style.padding = '0 6px';
            badge.style.border = '1px solid #3b82f6';
            badge.style.borderRadius = '10px';
            badge.style.background = '#e6f0ff';
            badge.style.color = '#1d4ed8';
            badge.style.fontSize = '0.75em';
            wrapper.appendChild(badge);
            btn.innerHTML = '';
            btn.appendChild(wrapper);
          } else {
            btn.textContent = buttonLabel;
          }
        }

        // On click, open a NEW tab for this individual concept (do not mutate current tab)
        btn.addEventListener('click', () => {
          const conceptId = concept.concept_id || concept._id;
          const conceptName = concept.name || 'Concept';
          if (!conceptId) return;
          const event = new CustomEvent('open-concept-tab', {
            detail: { conceptId, conceptName, kind: 'individual', activate: true }
          });
          document.dispatchEvent(event);
        });

        li.appendChild(btn);

        // If we know the direct type, add a separate clickable chip to open the TYPE concept
        const directTypeName = concept.direct_concept_name;
        // Prefer the explicit direct_concept_id supplied by the API; fallback to concept.concept_id if it looks like a Vontology ID
        const directTypeId = concept.direct_concept_id || (String(concept.concept_id || '').startsWith('#V#') ? concept.concept_id : '');
        if (directTypeName && directTypeName !== displayNames.singular && directTypeId && String(directTypeId).startsWith('#V#')) {
          const typeBtn = document.createElement('button');
          typeBtn.type = 'button';
          typeBtn.className = 'concept-item-button type'; // Visual cue: type
          typeBtn.style.display = 'inline-block';
          typeBtn.style.padding = '2px 6px';
          typeBtn.style.margin = '2px 0 2px 6px';
          typeBtn.style.borderRadius = '10px';
          typeBtn.style.cursor = 'pointer';
          typeBtn.title = 'Open type';
          typeBtn.textContent = directTypeName;
          typeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const evt = new CustomEvent('open-concept-tab', {
              detail: { conceptId: directTypeId, conceptName: directTypeName, kind: 'unknown', activate: true }
            });
            document.dispatchEvent(evt);
          });
          li.appendChild(typeBtn);
        }
        conceptListUl.appendChild(li);
      });
    } else {
      conceptListUl.innerHTML = `<li>No ${displayNames.plural.toLowerCase()} found.</li>`;
    }

    console.log(`Loaded ${conceptResponse.concepts?.length || 0} entities of type ${displayNames.singular}`);

    // Update note text based on filter mode
    if (noteEl) {
      noteEl.style.display = '';
      if (directOnly) {
        noteEl.textContent = 'Showing only direct instances of this type.';
      } else {
        noteEl.textContent = 'List may include indirect (typed) instances. Use the toggle to show only direct instances.';
      }
    }

    // Update the concept count display
    const conceptCountElement = document.getElementById(`conceptCount_${suffix}`);
    if (conceptCountElement) {
      conceptCountElement.textContent = conceptResponse.concepts?.length || 0;
    }

    // No radio selection to restore in button list

  } catch (error) {
    console.error('fetchConceptListWithSuffix: Error fetching concept list:', error);
    conceptListUl.innerHTML = '<li class="error">Error loading concepts. Please try again.</li>';
  }
}

// Populate the Subtypes list (Type-only) for the current type tab
export async function fetchSubtypesWithSuffix(conceptId, suffix) {
  try {
    const listEl = document.getElementById(`subtypesList_${suffix}`);
    const statusEl = document.getElementById(`subtypesStatus_${suffix}`);
    if (!listEl) return;
    listEl.innerHTML = '<li>Loading subtypes...</li>';
    if (statusEl) statusEl.textContent = '';

    const data = await getJson(`/vontology/api/vontology/children?node_id=${encodeURIComponent(conceptId)}`);
    listEl.innerHTML = '';
    const children = Array.isArray(data?.children) ? data.children : [];
    if (!children.length) {
      listEl.innerHTML = '<li><i>No subtypes.</i></li>';
      return;
    }
    children.forEach(child => {
      const li = document.createElement('li');
      li.className = 'concept-list-item';
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'concept-item-button';
      // Safety fallback: if no name, derive from concept_id like "#V#some_type" -> "Some Type"
      const fallback = (child.id || child.node_id || child._id || '').toString().replace(/^#V#/, '').replace(/_/g, ' ').replace(/\s+/g, ' ').trim();
      const pretty = fallback ? (fallback.charAt(0).toUpperCase() + fallback.slice(1)) : 'Type';
      btn.textContent = (child.name && String(child.name).trim()) || pretty;
      btn.addEventListener('click', () => {
        const id = child.id || child.node_id || child._id;
        if (!id) return;
        const evt = new CustomEvent('open-concept-tab', {
          detail: { conceptId: id, conceptName: (child.name && String(child.name).trim()) || pretty, kind: 'unknown', activate: true }
        });
        document.dispatchEvent(evt);
      });
      li.appendChild(btn);
      listEl.appendChild(li);
    });
  } catch (err) {
    const listEl = document.getElementById(`subtypesList_${suffix}`);
    if (listEl) listEl.innerHTML = '<li class="error">Error loading subtypes.</li>';
  }
}

// Populate the Instances list (Type-only) for the current type tab
export async function fetchInstancesWithSuffix(conceptId, suffix) {
  try {
    const listEl = document.getElementById(`instancesList_${suffix}`);
    const statusEl = document.getElementById(`instancesStatus_${suffix}`);
    if (!listEl) return;
    listEl.innerHTML = '<li>Loading instances...</li>';
    if (statusEl) statusEl.textContent = '';

    const data = await getJson(`/vontology/api/vontology/instances?node_id=${encodeURIComponent(conceptId)}&include_subtypes=true`);
    listEl.innerHTML = '';
    const instances = Array.isArray(data?.instances) ? data.instances : [];
    if (!instances.length) {
      listEl.innerHTML = '<li><i>No instances.</i></li>';
      return;
    }
    instances.forEach(inst => {
      const li = document.createElement('li');
      li.className = 'concept-list-item';
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'concept-item-button';
      // Safety fallback for instances
      const fallback = (inst.id || inst._id || '').toString().replace(/^#V#/, '').replace(/_/g, ' ').replace(/\s+/g, ' ').trim();
      const pretty = fallback ? (fallback.charAt(0).toUpperCase() + fallback.slice(1)) : 'Individual';
      btn.textContent = (inst.name && String(inst.name).trim()) || pretty;
      btn.addEventListener('click', () => {
        const id = inst.id || inst._id;
        if (!id) return;
        const evt = new CustomEvent('open-concept-tab', {
          detail: { conceptId: id, conceptName: (inst.name && String(inst.name).trim()) || pretty, kind: 'individual', activate: true }
        });
        document.dispatchEvent(evt);
      });
      li.appendChild(btn);
      listEl.appendChild(li);
    });
  } catch (err) {
    const listEl = document.getElementById(`instancesList_${suffix}`);
    if (listEl) listEl.innerHTML = '<li class="error">Error loading instances.</li>';
  }
}

// Names Management Functions
let currentConceptNames = [];

/**
 * Get the preferred language from settings
 * @returns {Promise<string>} The preferred language code
 */
async function getPreferredLanguage() {
  try {
    const response = await fetch('/api/settings/');
    if (response.ok) {
      const settings = await response.json();
      return settings.preferred_language || 'en-NZ';
    }
  } catch (error) {
    console.warn('Could not load preferred language setting:', error);
  }
  return 'en-NZ'; // Default fallback
}

/**
 * Initialize the names form with default values
 * @param {string} suffix - Optional suffix for dynamic tabs
 */
export async function initializeNamesForm(suffix = '') {
  const newNameLanguage = getSuffixElement('newNameLanguage', suffix);
  const newNameType = getSuffixElement('newNameType', suffix);

  if (newNameLanguage) {
    try {
      const preferredLanguage = await getPreferredLanguage();
      // Populate language options using shared configuration
      populateLanguageSelect(newNameLanguage, preferredLanguage);
    } catch (error) {
      console.warn('Failed to populate language select:', error);
      // Fallback: add at least English option
      if (newNameLanguage.options.length === 0) {
        const fallbackOption = document.createElement('option');
        fallbackOption.value = 'en-NZ';
        fallbackOption.textContent = 'English (New Zealand)';
        newNameLanguage.appendChild(fallbackOption);
        newNameLanguage.value = 'en-NZ';
      }
    }
  }

  if (newNameType) {
    // Populate name type options if empty (defensive for dynamic tabs)
    if (newNameType.options.length === 0) {
      const nameTypes = [
        { value: 'NL', label: 'Natural Language' },
        { value: 'CODE', label: 'Code' },
        { value: 'ABBR', label: 'Abbreviation' }
      ];

      for (const type of nameTypes) {
        const option = document.createElement('option');
        option.value = type.value;
        option.textContent = type.label;
        newNameType.appendChild(option);
      }
    }

    newNameType.value = 'NL'; // Default to Natural Language
  }
}

/**
 * Display the names array for the current concept
 * @param {Array} names - Array of name objects {name, language, type}
 * @param {string} suffix - Optional suffix for dynamic tabs
 */
export async function displayConceptNames(names = [], suffix = '') {
  console.log('displayConceptNames called with:', names, 'suffix:', suffix);

  const namesList = getSuffixElement('namesList', suffix);

  if (!namesList) {
    console.warn('Names list element not found, looking for:', suffix ? `namesList_${suffix}` : 'namesList');
    return;
  }

  console.log('Found namesList element:', namesList);

  // Get user's preferred language for sorting
  const preferredLanguage = await getPreferredLanguage();
  console.log('User preferred language:', preferredLanguage);

  // Normalize to consistent objects: { name, language, type }
  if (Array.isArray(names)) {
    currentConceptNames = names.map(normaliseNameRecord);

    // Sort names: preferred language first, then by type priority (NL > ABBR > CODE)
    currentConceptNames.sort((a, b) => {
      // Preferred language comes first
      const aMatchesLang = a.language === preferredLanguage;
      const bMatchesLang = b.language === preferredLanguage;
      if (aMatchesLang && !bMatchesLang) return -1;
      if (!aMatchesLang && bMatchesLang) return 1;

      // Within same language preference, sort by type priority
      const typePriority = { 'NL': 0, 'ABBR': 1, 'CODE': 2 };
      const aPriority = typePriority[a.type] ?? 99;
      const bPriority = typePriority[b.type] ?? 99;
      return aPriority - bPriority;
    });
  } else {
    currentConceptNames = [];
  }
  namesList.innerHTML = '';

  console.log('currentConceptNames set to:', currentConceptNames);

  // Always show the names section so users can add names even if none exist yet
  const namesSection = getSuffixElement('namesSection', suffix);
  if (namesSection) {
    namesSection.style.display = 'block';
  }

  if (currentConceptNames.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'names-empty-state';
    empty.style.color = '#6b7280';
    empty.style.fontStyle = 'italic';
    empty.textContent = 'No names yet. Add one below.';
    namesList.appendChild(empty);
    return;
  }

  console.log('Showing names section');

  console.log('About to render', currentConceptNames.length, 'name cartouches');

  currentConceptNames.forEach((nameObj, index) => {
    console.log('Rendering cartouche for name:', nameObj, 'at index:', index);

    const cartouche = document.createElement('div');
    cartouche.className = 'name-cartouche';

    // Mark the first NL name as primary
    const isFirstNL = nameObj.type === 'NL' && !currentConceptNames.slice(0, index).some(n => n.type === 'NL');
    if (isFirstNL) {
      cartouche.classList.add('primary');
    }

    console.log('Created cartouche element:', cartouche);

    // Name text (click to edit)
    const nameText = document.createElement('span');
    nameText.className = 'name-text';
    nameText.textContent = nameObj.name || nameObj.text || '';
    nameText.title = nameObj.name || nameObj.text || '';

    // Helpers for gated CamelCase spacing
    const isLatinLanguageCode = (code) => {
      if (!code) return false;
      const c = String(code).toLowerCase();
      // Allow en, en-nz, es, fr, mi (Māori uses Latin script)
      return c === 'en' || c === 'en-nz' || c === 'es' || c === 'fr' || c === 'mi';
    };
    const containsCJKorHangul = (s) => {
      if (!s) return false;
      // CJK Unified Ideographs, Hiragana, Katakana, Hangul
      const cjk = /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f]/;
      const hangul = /[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]/;
      return cjk.test(s) || hangul.test(s);
    };
    // Auto-insert spaces for CamelCase/PascalCase when proposing edits
    const autoSpaceCamelCase = (s) => {
      if (!s) return s;
      // Insert space between lowercase-to-uppercase and between acronyms followed by words
      let spaced = s.replace(/([a-z])([A-Z])/g, '$1 $2');
      spaced = spaced.replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2');
      return spaced;
    };

    // Inline edit behavior
    nameText.addEventListener('click', () => {
      try {
        const original = nameObj.name || nameObj.text || '';
        // Only propose CamelCase spacing for Latin languages and when text looks Latin-script
        const lang = (nameObj.language || '').toString();
        const shouldSpace = isLatinLanguageCode(lang) && !containsCJKorHangul(original);
        const proposed = shouldSpace ? autoSpaceCamelCase(original) : original;
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'name-edit-input';
        input.value = proposed !== original ? proposed : original;
        input.setAttribute('aria-label', 'Edit name text');
        // Keep badges and delete button; replace only the text span
        cartouche.replaceChild(input, nameText);
        input.focus();
        input.select();

        let finished = false;
        const finish = async (save) => {
          if (finished) return; // one-shot guard to avoid double-invoke on Enter+blur
          finished = true;
          const newVal = (input.value || '').trim();
          // Restore text span in DOM
          cartouche.replaceChild(nameText, input);
          if (!save) {
            return; // cancelled
          }
          if (!newVal) {
            // Do not save empty strings; keep original
            return;
          }
          if (newVal === original) {
            return; // no-op
          }
          // Update local model and persist
          await updateConceptNameEntry(index, newVal, suffix);
        };

        input.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter') {
            ev.preventDefault();
            finish(true);
          } else if (ev.key === 'Escape') {
            ev.preventDefault();
            finish(false);
          }
        });
        input.addEventListener('blur', () => finish(true));
      } catch (e) {
        console.warn('Inline name edit failed:', e);
      }
    });

    // Meta info (type and language)
    const nameMeta = document.createElement('div');
    nameMeta.className = 'name-meta';

    const typeBadge = document.createElement('span');
    typeBadge.className = 'name-type-badge';
    typeBadge.textContent = nameObj.type || 'NL';

    const languageBadge = document.createElement('span');
    languageBadge.className = 'name-language-badge';
    languageBadge.textContent = nameObj.language || 'en-NZ';

    nameMeta.appendChild(typeBadge);
    nameMeta.appendChild(languageBadge);

    // Delete button (disabled if it's the only name)
    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'name-delete-button';
    deleteBtn.innerHTML = '×';
    deleteBtn.title = 'Remove this name';
    deleteBtn.disabled = currentConceptNames.length === 1;

    if (!deleteBtn.disabled) {
      deleteBtn.addEventListener('click', () => deleteName(index, suffix));
    }

    cartouche.appendChild(nameText);
    cartouche.appendChild(nameMeta);
    cartouche.appendChild(deleteBtn);

    console.log('About to append cartouche to namesList:', cartouche);
    namesList.appendChild(cartouche);
    console.log('Cartouche appended. namesList now has', namesList.children.length, 'children');
  });
}

/**
 * Add a new name to the concept
 * @param {string} suffix - Optional suffix for dynamic tabs
 */
export async function addNewName(suffix = '') {
  const newNameInput = getSuffixElement('newNameInput', suffix);
  const newNameLanguage = getSuffixElement('newNameLanguage', suffix);
  const newNameType = getSuffixElement('newNameType', suffix);

  if (!newNameInput || !newNameLanguage || !newNameType) {
    console.error('Name input elements not found');
    return;
  }

  const name = newNameInput.value.trim();
  const language = newNameLanguage.value;
  const type = newNameType.value;

  if (!name) {
    setNamesStatusMessage(suffix, 'Please enter a name', 'red');
    newNameInput.focus();
    return;
  }

  // Check for duplicates
  const duplicate = currentConceptNames.find(n =>
    n.name === name && n.language === language && n.type === type
  );

  if (duplicate) {
    setNamesStatusMessage(suffix, 'This name already exists', 'red');
    return;
  }

  // Add to local array
  const newNameObj = { name, language, type };
  currentConceptNames.push(newNameObj);

  // Update display
  await displayConceptNames(currentConceptNames, suffix);

  const conceptId = getCurrentlySelectedConceptId();
  if (!conceptId) {
    console.error('No concept selected');
    return;
  }

  try {
    setNamesStatusMessage(suffix, 'Saving...', 'blue');
    await postJson(`/api/concepts/${encodeURIComponent(conceptId)}/texts`, {
      predicate: 'hasName',
      text: name,
      lang: language,
      context: { name_type: type },
    });
    await loadConceptNames(conceptId, suffix);
    setNamesStatusMessage(suffix, 'Name added successfully', 'green', true);
  } catch (error) {
    console.error('Error saving new name:', error);
    setNamesStatusMessage(suffix, `Error saving name: ${error.message}`, 'red');
    await loadConceptNames(conceptId, suffix);
    return;
  }

  // Notify dynamic tabs that names for this concept changed so labels can update
  try {
    const conceptId = getCurrentlySelectedConceptId();
    const evt = new CustomEvent('concept-names-changed', { detail: { conceptId } });
    document.dispatchEvent(evt);
  } catch (_) { /* ignore */ }

  // Clear input
  newNameInput.value = '';
}

/**
 * Delete a name from the concept
 * @param {number} index - Index of the name to delete
 * @param {string} suffix - Optional suffix for dynamic tabs
 */
export async function deleteName(index, suffix = '') {
  if (currentConceptNames.length <= 1) {
    setNamesStatusMessage(suffix, 'Every concept needs at least one name', 'red', true);
    return; // Don't allow deleting the last name
  }

  const conceptId = getCurrentlySelectedConceptId();
  if (!conceptId) {
    console.error('No concept selected');
    return;
  }

  const nameToDelete = currentConceptNames[index];

  try {
    setNamesStatusMessage(suffix, 'Deleting...', 'blue');

    if (nameToDelete?.relationId) {
      await deleteJson(`/api/concepts/${encodeURIComponent(conceptId)}/texts/${encodeURIComponent(nameToDelete.relationId)}`);
    } else {
      await deleteTextRelationByPredicate(conceptId, 'hasName', nameToDelete.name, {
        lang: nameToDelete.language,
        context: { name_type: nameToDelete.type },
      });
    }

    await loadConceptNames(conceptId, suffix);
    setNamesStatusMessage(suffix, 'Name removed successfully', 'green', true);

    // Notify that names changed
    try {
      const evt = new CustomEvent('concept-names-changed', { detail: { conceptId } });
      document.dispatchEvent(evt);
    } catch (_) { }

  } catch (error) {
    console.error('Error deleting name:', error);
    setNamesStatusMessage(suffix, `Error deleting name: ${error.message}`, 'red');
    await loadConceptNames(conceptId, suffix);
  }
}

/**
 * Save the current names array to the backend
 * @param {string} suffix - Optional suffix for dynamic tabs
 */
async function updateConceptNameEntry(index, newValue, suffix = '') {
  const conceptId = getCurrentlySelectedConceptId();
  if (!conceptId) {
    console.error('No concept selected');
    return;
  }

  const entry = currentConceptNames[index];
  if (!entry) {
    return;
  }

  const lang = entry.language || 'en-NZ';
  const relationId = entry.relationId;

  if (!relationId) {
    // No relation id available, fallback by deleting + re-adding the name.
    await deleteTextRelationByPredicate(conceptId, 'hasName', entry.name, {
      lang: entry.language,
      context: { name_type: entry.type },
    });
    await postJson(`/api/concepts/${encodeURIComponent(conceptId)}/texts`, {
      predicate: 'hasName',
      text: newValue,
      lang,
      context: { name_type: entry.type || 'NL' },
    });
  } else {
    await patchJson(`/api/concepts/${encodeURIComponent(conceptId)}/texts/${encodeURIComponent(relationId)}`, {
      text: newValue,
      lang,
    });
  }

  await loadConceptNames(conceptId, suffix);
  setNamesStatusMessage(suffix, 'Name updated successfully', 'green', true);

  try {
    const evt = new CustomEvent('concept-names-changed', { detail: { conceptId } });
    document.dispatchEvent(evt);
  } catch (_) { }
}

/**
 * Load names for a concept from the backend
 * @param {string} conceptId - The concept ID
 * @param {string} suffix - Optional suffix for dynamic tabs
 */
export async function loadConceptNames(conceptId, suffix = '') {
  console.log('loadConceptNames called with conceptId:', conceptId, 'suffix:', suffix);

  if (!conceptId) {
    displayConceptNames([], suffix);
    await initializeNamesForm(suffix); // Initialize form even for empty concept
    return;
  }

  const namesStatus = getSuffixElement('namesStatus', suffix);
  const containerId = suffix ? `conceptTab_${suffix}` : 'conceptTab';
  const container = document.getElementById(containerId);
  if (container && container.dataset.conceptMissing === '1') {
    if (namesStatus) {
      namesStatus.textContent = 'Concept not found. It may have been deleted or is unavailable.';
      namesStatus.style.color = '#b91c1c';
    }
    return { status: 'not_found_cached' };
  }

  try {
    const response = await fetch(`/api/concepts/${encodeURIComponent(conceptId)}`);
    console.log('API response status:', response.status, response.ok);

    if (!response.ok) {
      if (response.status === 404) {
        const message = 'Concept not found. It may have been deleted or is unavailable.';
        console.warn(`[conceptTab] Concept ${conceptId} returned 404`);
        await displayConceptNames([], suffix);
        renderMissingConceptState(conceptId, suffix, { message });
        return { status: 'not_found', message };
      }
      throw new Error(`HTTP ${response.status}`);
    }

    const concept = await response.json();
    console.log('Loaded concept data:', concept);

    const names = concept.names || [];
    console.log('Extracted names array:', names);

    await displayConceptNames(names, suffix);
    await initializeNamesForm(suffix); // Initialize form after loading concept

    if (namesStatus) {
      namesStatus.textContent = '';
    }

    return { status: 'ok', namesCount: Array.isArray(names) ? names.length : 0 };

  } catch (error) {
    console.error('Error loading concept names:', error);
    await displayConceptNames([], suffix);
    await initializeNamesForm(suffix); // Initialize form even on error
    if (namesStatus) {
      namesStatus.textContent = `Error loading names: ${error.message}`;
      namesStatus.style.color = 'red';
    }
    return { status: 'error', error };
  }
}

/**
 * Load and display text relation attributes for a concept (excluding names)
 * @param {string} conceptId - The concept ID
 * @param {string} suffix - Optional suffix for dynamic tabs
 */
export async function loadConceptAttributes(conceptId, suffix = '') {
  console.log('loadConceptAttributes called with conceptId:', conceptId, 'suffix:', suffix);

  const getSuffixElement = (baseId) => {
    const id = suffix ? `${baseId}_${suffix}` : baseId;
    return document.getElementById(id);
  };

  const attributesList = getSuffixElement('attributesList');
  if (!attributesList) {
    console.warn('Attributes list element not found');
    return;
  }

  // Clear existing attributes
  attributesList.innerHTML = '';

  if (!conceptId) {
    const empty = document.createElement('div');
    empty.className = 'attributes-empty-state';
    empty.style.color = '#6b7280';
    empty.style.fontStyle = 'italic';
    empty.textContent = 'No attributes to display.';
    attributesList.appendChild(empty);
    return;
  }

  try {
    const response = await fetch(`/vontology/api/vontology/text_relations?concept_id=${encodeURIComponent(conceptId)}&limit=200`);

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const data = await response.json();
    console.log('Text relations data:', data);

    // Filter out hasName predicates (those are shown in Names section)
    const attributes = (data.text_relations || []).filter(
      rel => rel.predicate && rel.predicate !== 'hasName' && rel.predicate !== '#V#hasName'
    );

    console.log('Filtered attributes (excluding names):', attributes);

    if (attributes.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'attributes-empty-state';
      empty.style.color = '#6b7280';
      empty.style.fontStyle = 'italic';
      empty.textContent = 'No attributes defined yet.';
      attributesList.appendChild(empty);
      return;
    }

    // Display each attribute
    attributes.forEach(attr => {
      const cartouche = document.createElement('div');
      cartouche.className = 'attribute-cartouche';
      cartouche.style.display = 'flex';
      cartouche.style.alignItems = 'center';
      cartouche.style.gap = '8px';
      cartouche.style.padding = '6px 12px';
      cartouche.style.backgroundColor = '#f3f4f6';
      cartouche.style.borderRadius = '4px';
      cartouche.style.marginBottom = '6px';

      // Predicate label
      const predicateLabel = document.createElement('span');
      predicateLabel.className = 'attribute-predicate';
      predicateLabel.style.fontWeight = '600';
      predicateLabel.style.color = '#374151';
      predicateLabel.style.minWidth = '120px';
      // Remove #V# prefix for display
      const displayPredicate = attr.predicate.replace(/^#V#/, '');
      predicateLabel.textContent = displayPredicate + ':';

      // Text value
      const textValue = document.createElement('span');
      textValue.className = 'attribute-text';
      textValue.style.flex = '1';
      textValue.style.color = '#1f2937';
      textValue.textContent = attr.text || '';
      textValue.title = attr.text || '';

      // Language badge (if not default)
      const langBadge = document.createElement('span');
      langBadge.className = 'attribute-language-badge';
      langBadge.style.fontSize = '0.75rem';
      langBadge.style.padding = '2px 6px';
      langBadge.style.borderRadius = '3px';
      langBadge.style.backgroundColor = '#e5e7eb';
      langBadge.style.color = '#6b7280';
      if (attr.language && attr.language !== 'en-NZ') {
        langBadge.textContent = attr.language;
      }

      cartouche.appendChild(predicateLabel);
      cartouche.appendChild(textValue);
      if (langBadge.textContent) {
        cartouche.appendChild(langBadge);
      }

      attributesList.appendChild(cartouche);
    });

  } catch (error) {
    console.error('Error loading concept attributes:', error);
    attributesList.innerHTML = '<div style="color: #dc2626; font-style: italic;">Error loading attributes</div>';
  }
}

/**
 * Handle creating a new subtype from the concept tab
 * @param {string} suffix - The tab suffix
 */
async function handleCreateSubtype(suffix = '') {
  const getSuffixElement = (baseId) => {
    const id = suffix ? `${baseId}_${suffix}` : baseId;
    return document.getElementById(id);
  };

  const newSubtypeInput = getSuffixElement('newSubtypeInput');
  const subtypesStatus = getSuffixElement('subtypesStatus');
  const createSubtypeButton = getSuffixElement('createSubtypeButton');

  const rawSubtypeInput = newSubtypeInput?.value || '';
  const newConceptName = rawSubtypeInput.trim();
  const parentId = getCurrentlySelectedConceptId();

  if (!newConceptName) {
    if (subtypesStatus) {
      subtypesStatus.textContent = "Please enter a subtype name.";
      subtypesStatus.style.color = "red";
    }
    return;
  }

  if (!parentId) {
    if (subtypesStatus) {
      subtypesStatus.textContent = "No parent type selected.";
      subtypesStatus.style.color = "red";
    }
    return;
  }

  try {
    if (createSubtypeButton) createSubtypeButton.disabled = true;
    if (subtypesStatus) {
      subtypesStatus.textContent = "Creating subtype...";
      subtypesStatus.style.color = "black";
    }

    // Normalize subtype name via shared util
    const { normalized: normalizedSubtype, changed: changedSubtype, notes: subtypeNotes } = normalizeConceptName(newConceptName);
    let finalSubtypeName = normalizedSubtype;
    if (subtypesStatus && changedSubtype) {
      subtypesStatus.textContent = `Normalized name to: "${finalSubtypeName}" (${subtypeNotes})`;
      subtypesStatus.style.color = 'orange';
    }
    if (checkDuplicateName(finalSubtypeName, '.current-subtypes-list .subtype-entry')) {
      if (subtypesStatus) {
        subtypesStatus.textContent = `Warning: A subtype named "${finalSubtypeName}" already exists (possible duplicate).`;
        subtypesStatus.style.color = 'darkorange';
      }
    }

    const response = await postJson('/vontology/api/vontology/create_concept', {
      new_concept_name: finalSubtypeName,
      parent_id: parentId,
      create_as_instance: false  // Create as type (subtype)
    });
    recordNameNormalizationMetric('subtype');

    const createdConceptId = response?.concept_id || response?.concept?.concept_id;
    const createdConceptName = response?.concept?.name || finalSubtypeName;
    const createdMongoId = response?.id || response?.concept?.mongo_id || response?.concept?._id || null;

    if (subtypesStatus) {
      subtypesStatus.textContent = `Subtype "${createdConceptName}" created successfully!`;
      subtypesStatus.style.color = "green";
    }

    // Clear input
    if (newSubtypeInput) {
      newSubtypeInput.value = '';
    }

    if (createdConceptId) {
      await insertNodeIntoVontologyTree(parentId, { id: createdConceptId, name: createdConceptName, mongo_id: createdMongoId });
    }

    // Refresh the subtypes list
    await fetchSubtypesWithSuffix(parentId, suffix);

    // Open a tab for the new subtype
    if (createdConceptId) {
      const evt = new CustomEvent('open-concept-tab', {
        detail: {
          conceptId: createdConceptId,
          conceptName: createdConceptName,
          kind: 'unknown',
          activate: false,
          newlyCreated: true
        }
      });
      document.dispatchEvent(evt);
    }

    // Clear status after a delay
    setTimeout(() => {
      if (subtypesStatus) {
        subtypesStatus.textContent = '';
      }
    }, 3000);

  } catch (error) {
    console.error('Error creating subtype:', error);
    if (subtypesStatus) {
      const msg = (error && error.message) || 'Unknown error creating subtype';
      subtypesStatus.textContent = `Error: ${msg}`;
      subtypesStatus.style.color = "red";
    }
  } finally {
    if (createSubtypeButton) createSubtypeButton.disabled = false;
  }
}

/**
 * Handle creating a new instance from the concept tab
 * @param {string} suffix - The tab suffix
 */
async function handleCreateInstance(suffix = '') {
  const getSuffixElement = (baseId) => {
    const id = suffix ? `${baseId}_${suffix}` : baseId;
    return document.getElementById(id);
  };

  const newInstanceInput = getSuffixElement('newInstanceInput');
  const instancesStatus = getSuffixElement('instancesStatus');
  const createInstanceButton = getSuffixElement('createInstanceButton');

  const rawInputName = newInstanceInput?.value || '';
  const newConceptName = rawInputName.trim();
  const parentId = getCurrentlySelectedConceptId();

  if (!newConceptName) {
    if (instancesStatus) {
      instancesStatus.textContent = "Please enter an instance name.";
      instancesStatus.style.color = "red";
    }
    return;
  }

  // Normalize concept name via shared util
  const { normalized, changed, notes: normNotes } = normalizeConceptName(newConceptName);
  let sanitizedName = normalized;
  if (instancesStatus && changed) {
    instancesStatus.textContent = `Normalized name to: "${sanitizedName}" (${normNotes})`;
    instancesStatus.style.color = "orange";
  }
  if (checkDuplicateName(sanitizedName, '.current-instances-list .instance-entry')) {
    if (instancesStatus) {
      instancesStatus.textContent = `Warning: An instance named "${sanitizedName}" already exists (possible duplicate).`;
      instancesStatus.style.color = 'darkorange';
    }
  }

  if (!parentId) {
    if (instancesStatus) {
      instancesStatus.textContent = "No parent type selected.";
      instancesStatus.style.color = "red";
    }
    return;
  }

  try {
    if (createInstanceButton) createInstanceButton.disabled = true;
    if (instancesStatus) {
      instancesStatus.textContent = "Creating instance...";
      instancesStatus.style.color = "black";
    }

    const response = await postJson('/vontology/api/vontology/create_concept', {
      new_concept_name: sanitizedName,
      parent_id: parentId,
      create_as_instance: true  // Create as instance (individual)
    });
    recordNameNormalizationMetric('instance');

    const createdConceptId = response?.concept_id || response?.concept?.concept_id;
    const createdConceptName = response?.concept?.name || sanitizedName;

    if (instancesStatus) {
      instancesStatus.textContent = `Instance "${createdConceptName}" created successfully!`;
      instancesStatus.style.color = "green";
    }

    // Clear input
    if (newInstanceInput) {
      newInstanceInput.value = '';
    }

    // Refresh the unified Instances list (Current Concept List)
    await fetchConceptListWithSuffix(parentId, suffix);

    // Open a tab for the new instance
    if (createdConceptId) {
      const evt = new CustomEvent('open-concept-tab', {
        detail: {
          conceptId: createdConceptId,
          conceptName: createdConceptName,
          kind: 'individual',
          activate: false,
          newlyCreated: true
        }
      });
      document.dispatchEvent(evt);
    }

    // Clear status after a delay
    setTimeout(() => {
      if (instancesStatus) {
        instancesStatus.textContent = '';
      }
    }, 3000);

  } catch (error) {
    console.error('Error creating instance:', error);
    if (instancesStatus) {
      const msg = (error && error.message) || 'Unknown error creating instance';
      instancesStatus.textContent = `Error: ${msg}`;
      instancesStatus.style.color = "red";
    }
  } finally {
    if (createInstanceButton) createInstanceButton.disabled = false;
  }
}

// Name normalization now provided by nameUtils.js

function refreshRenderedConceptNotes() {
  if (!elements || !elements.conceptNotesRendered) return;
  const raw = (elements.updatedNotesContent && elements.updatedNotesContent.value) || (elements.conceptNotesInput && elements.conceptNotesInput.value) || '';
  elements.conceptNotesRendered.innerHTML = simpleMarkdownToHtml(raw.trim());
}

