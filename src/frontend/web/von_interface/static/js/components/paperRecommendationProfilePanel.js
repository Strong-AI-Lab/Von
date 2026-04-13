import {
  getJsonDetailed,
  getWindowSessionId,
  WINDOW_SESSION_HEADER,
} from '../apiService.js';
import {
  applyRecommendationProfileToElements,
  buildRecommendationProfilePayload,
} from './paperRecommendationUi.js';
import { showToast } from '../utils/toast.js';

function buildHeaders(extraHeaders = {}) {
  return {
    [WINDOW_SESSION_HEADER]: getWindowSessionId(),
    ...extraHeaders,
  };
}

function setStatus(statusElement, message, isError = false) {
  if (!statusElement) return;
  statusElement.textContent = message || '';
  statusElement.className = isError ? 'status-message error' : 'status-message success';
  statusElement.style.display = message ? 'block' : 'none';
}

function setDisabled(elements, disabled) {
  for (const element of elements) {
    if (element) {
      element.disabled = !!disabled;
    }
  }
}

function ensurePanelElement(stepContainer, suffix) {
  let panel = document.getElementById(`recommendationProfilePanel_${suffix}`);
  if (panel) return panel;

  panel = document.createElement('section');
  panel.id = `recommendationProfilePanel_${suffix}`;
  panel.className = 'speech-settings-section concept-recommendation-panel';
  panel.innerHTML = `
    <h3 class="speech-settings-title">Paper Recommendation Profile</h3>
    <p class="settings-note concept-recommendation-intro">
      This concept-owned profile steers future paper recommendations for this subject.
    </p>

    <div class="speech-settings-row">
      <label class="speech-settings-label" for="recommendationProjectDescriptionInput_${suffix}">Project / research description</label>
      <textarea id="recommendationProjectDescriptionInput_${suffix}" class="speech-settings-input" rows="4"
        placeholder="Describe the current research direction, project, or problem area."></textarea>
    </div>

    <div class="speech-settings-row">
      <label class="speech-settings-label" for="recommendationInterestTermsInput_${suffix}">Interest terms</label>
      <input id="recommendationInterestTermsInput_${suffix}" class="speech-settings-input" type="text"
        inputmode="text" placeholder="e.g., causal reasoning, knowledge graphs, neuro-symbolic AI" />
    </div>

    <div class="speech-settings-row">
      <label class="speech-settings-label" for="recommendationNegativeTermsInput_${suffix}">Avoid topics / keywords</label>
      <input id="recommendationNegativeTermsInput_${suffix}" class="speech-settings-input" type="text"
        inputmode="text" placeholder="e.g., federated learning, pure benchmarking papers" />
    </div>

    <div class="speech-settings-row">
      <label class="speech-settings-label" for="recommendationPreferredAuthorsInput_${suffix}">Preferred authors</label>
      <input id="recommendationPreferredAuthorsInput_${suffix}" class="speech-settings-input" type="text"
        inputmode="text" placeholder="Comma-separated author names" />
    </div>

    <div class="speech-settings-row">
      <label class="speech-settings-label" for="recommendationPreferredVenuesInput_${suffix}">Preferred venues</label>
      <input id="recommendationPreferredVenuesInput_${suffix}" class="speech-settings-input" type="text"
        inputmode="text" placeholder="Comma-separated venues or journals" />
    </div>

    <div class="speech-settings-row">
      <label class="speech-settings-label" for="recommendationProfileNotesInput_${suffix}">Notes</label>
      <textarea id="recommendationProfileNotesInput_${suffix}" class="speech-settings-input" rows="3"
        placeholder="Anything else the recommender should keep in mind."></textarea>
    </div>

    <div class="speech-settings-row">
      <label class="speech-settings-label">Observed research interests</label>
      <div id="recommendationObservedInterests_${suffix}" class="runtime-hint">
        Loading observed research-interest context...
      </div>
    </div>

    <div class="speech-settings-actions">
      <button id="saveRecommendationProfileButton_${suffix}" class="btn btn-secondary" type="button">
        Save paper recommendation profile
      </button>
      <span id="recommendationProfilePermissionHint_${suffix}" class="settings-note"></span>
    </div>

    <div id="recommendationProfileStatusMessage_${suffix}" class="status-message" role="alert"></div>
  `;
  stepContainer.appendChild(panel);
  return panel;
}

function getPanelElements(suffix) {
  return {
    panel: document.getElementById(`recommendationProfilePanel_${suffix}`),
    projectDescriptionInput: document.getElementById(`recommendationProjectDescriptionInput_${suffix}`),
    interestTermsInput: document.getElementById(`recommendationInterestTermsInput_${suffix}`),
    negativeTermsInput: document.getElementById(`recommendationNegativeTermsInput_${suffix}`),
    preferredAuthorsInput: document.getElementById(`recommendationPreferredAuthorsInput_${suffix}`),
    preferredVenuesInput: document.getElementById(`recommendationPreferredVenuesInput_${suffix}`),
    notesInput: document.getElementById(`recommendationProfileNotesInput_${suffix}`),
    observedInterestsElement: document.getElementById(`recommendationObservedInterests_${suffix}`),
    saveButton: document.getElementById(`saveRecommendationProfileButton_${suffix}`),
    statusElement: document.getElementById(`recommendationProfileStatusMessage_${suffix}`),
    permissionHint: document.getElementById(`recommendationProfilePermissionHint_${suffix}`),
  };
}

export async function ensureRecommendationProfilePanelForConceptTab({
  conceptId,
  suffix,
}) {
  const stepContainer =
    document.getElementById(`conceptStep1_${suffix}`) || document.getElementById('conceptStep1');
  if (!stepContainer) return;

  ensurePanelElement(stepContainer, suffix);
  const elements = getPanelElements(suffix);
  if (!elements.panel) return;
  elements.panel.dataset.subjectConceptId = conceptId;

  const editableElements = [
    elements.projectDescriptionInput,
    elements.interestTermsInput,
    elements.negativeTermsInput,
    elements.preferredAuthorsInput,
    elements.preferredVenuesInput,
    elements.notesInput,
    elements.saveButton,
  ];

  setDisabled(editableElements, true);
  setStatus(elements.statusElement, 'Loading recommendation profile...');

  try {
    const { data } = await getJsonDetailed(
      `/api/concepts/${encodeURIComponent(conceptId)}/paper_recommendation_profile`,
      { cache: 'no-store' },
    );

    if (elements.panel.dataset.subjectConceptId !== conceptId) return;

    applyRecommendationProfileToElements(
      {
        projectDescriptionInput: elements.projectDescriptionInput,
        interestTermsInput: elements.interestTermsInput,
        negativeTermsInput: elements.negativeTermsInput,
        preferredAuthorsInput: elements.preferredAuthorsInput,
        preferredVenuesInput: elements.preferredVenuesInput,
        notesInput: elements.notesInput,
        observedInterestsElement: elements.observedInterestsElement,
      },
      data,
    );

    const canEdit = data?.permissions?.can_edit === true;
    setDisabled(editableElements, !canEdit);
    if (elements.permissionHint) {
      elements.permissionHint.textContent = canEdit
        ? 'Editable from this current user or organisation context.'
        : 'Read-only here. Open the matching current user or organisation context to edit it.';
    }
    setStatus(
      elements.statusElement,
      canEdit
        ? 'Recommendation profile loaded.'
        : 'Recommendation profile loaded in read-only mode.',
    );

    if (elements.saveButton) {
      elements.saveButton.onclick = async () => {
        setStatus(elements.statusElement, 'Saving recommendation profile...');
        try {
          const response = await fetch(
            `/api/concepts/${encodeURIComponent(conceptId)}/paper_recommendation_profile`,
            {
              method: 'POST',
              headers: buildHeaders({ 'Content-Type': 'application/json' }),
              body: JSON.stringify(
                buildRecommendationProfilePayload({
                  projectDescription: elements.projectDescriptionInput?.value,
                  statedInterestTerms: elements.interestTermsInput?.value,
                  negativeInterestTerms: elements.negativeTermsInput?.value,
                  preferredAuthors: elements.preferredAuthorsInput?.value,
                  preferredVenues: elements.preferredVenuesInput?.value,
                  notes: elements.notesInput?.value,
                }),
              ),
            },
          );
          if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
          }
          const payload = await response.json();
          applyRecommendationProfileToElements(
            {
              projectDescriptionInput: elements.projectDescriptionInput,
              interestTermsInput: elements.interestTermsInput,
              negativeTermsInput: elements.negativeTermsInput,
              preferredAuthorsInput: elements.preferredAuthorsInput,
              preferredVenuesInput: elements.preferredVenuesInput,
              notesInput: elements.notesInput,
              observedInterestsElement: elements.observedInterestsElement,
            },
            payload,
          );
          setStatus(elements.statusElement, 'Recommendation profile saved.');
          showToast('Recommendation profile saved', 'success');
        } catch (error) {
          console.warn('[paperRecommendationProfilePanel] Failed to save profile', error);
          setStatus(elements.statusElement, 'Failed to save recommendation profile.', true);
          showToast('Failed to save recommendation profile', 'error');
        }
      };
    }
  } catch (error) {
    console.warn('[paperRecommendationProfilePanel] Failed to load profile', error);
    if (elements.observedInterestsElement) {
      elements.observedInterestsElement.textContent = 'Recommendation profile is unavailable for this concept.';
    }
    if (elements.permissionHint) {
      elements.permissionHint.textContent = '';
    }
    setDisabled(editableElements, true);
    setStatus(elements.statusElement, 'Failed to load recommendation profile.', true);
  }
}
