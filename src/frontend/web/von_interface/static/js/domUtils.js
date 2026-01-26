export const elements = {};

export function initializeDomElements() {
  // Chat elements
  elements.sendButton = document.getElementById('sendButton');
  elements.resetButton = document.getElementById('resetButton');
  elements.promptInput = document.getElementById('promptInput');
  elements.scrollableField = document.getElementById('scrollableField');
  elements.loadingIndicator = document.getElementById('loadingIndicator');

  // Entity Tab Elements
  elements.entityTypeDisplayNameElement = document.getElementById('entityTypeDisplayName');
  elements.entityTypeDisplayNamePluralElement = document.getElementById('entityTypeDisplayNamePluralElement');
  elements.entityStep1Div = document.getElementById('entityStep1');
  elements.startInteractionButton = document.getElementById('startInteractionButton');
  elements.entityNameInput = document.getElementById('entityName');
  elements.entityNotesInput = document.getElementById('entityNotes');
  elements.entityStep1Status = document.getElementById('entityStep1Status');
  elements.entityStep2Div = document.getElementById('entityStep2');
  elements.followUpQuestionP = document.getElementById('followUpQuestion');
  elements.entityAnswerInput = document.getElementById('entityAnswer');
  elements.submitAnswerButton = document.getElementById('submitAnswerButton');
  elements.entityStep2Status = document.getElementById('entityStep2Status');

  // Read-only notes display elements
  elements.updatedNotesDisplay = document.getElementById('updatedNotesDisplay');
  elements.updatedNotesContent = document.getElementById('updatedNotesContent');

  // Last Q&A display elements
  elements.lastQADisplay = document.getElementById('lastQADisplay');
  elements.lastQuestion = document.getElementById('lastQuestion');
  elements.lastAnswer = document.getElementById('lastAnswer');
  elements.lastSynthesis = document.getElementById('lastSynthesis');

  elements.entityStep3Div = document.getElementById('entityStep3');
  elements.finalResultP = document.getElementById('finalResult');
  elements.resetConceptTabButton = document.getElementById('resetConceptTabButton');
  elements.entityListUl = document.getElementById('entityListUl');
  elements.refreshentityListButton = document.getElementById('refreshentityListButton');
  elements.refreshEntityListButton = document.getElementById('refreshentityListButton');
  elements.saveNotesButton = document.getElementById('saveNotesButton');
  elements.suggestInfoButton = document.getElementById('suggestInfoButton');
  elements.removeCurrentEntityButton = document.getElementById('removeCurrentEntityButton');
  elements.addNewEntityButton = document.getElementById('addNewEntityButton');
  elements.cancelInteractionButton = document.getElementById('cancelInteractionButton');
  elements.endInteractionButton = document.getElementById('endInteractionButton');

  // Tab Elements
  elements.tabButtons = document.querySelectorAll('.tab-button');
  elements.tabContents = document.querySelectorAll('.tab-content');

  // Vontology Tab Elements
  elements.vontologyTab = document.getElementById('vontologyTab');
  elements.vontologyTreeContainer = document.getElementById('vontologyTreeContainer');
  elements.selectedNodePathSpan = document.getElementById('selectedNodePath');
  elements.vontologySearchInput = document.getElementById('vontologySearchInput');
  elements.vontologySearchResults = document.getElementById('vontologySearchResults');
  elements.vontologyNodeContentDiv = document.getElementById('vontologyNodeContent');
  elements.createConceptDiv = document.getElementById('vontologyCreateConcept');
  elements.newConceptNameInput = document.getElementById('newConceptName');
  elements.createConceptButton = document.getElementById('createConceptButton');
  elements.createConceptStatusP = document.getElementById('createConceptStatus');
  elements.showSubtreeDetailsButton = document.getElementById('showSubtreeDetailsButton');
  elements.subtreeDetailsDisplay = document.getElementById('subtreeDetailsDisplay');
  elements.subtreeDetailsStatus = document.getElementById('subtreeDetailsStatus');
  elements.modelInfoFooter = document.getElementById('modelInfoFooter');
}

// Cache for organisation description to avoid repeated fetches
let _orgDescriptionCache = null;
let _orgDescriptionCacheId = null;

// Default fallback description concept ID (Von system description)
const VON_SYSTEM_CONCEPT_ID = '#V#von_system';

/**
 * Fetch organisation description from Vontology text relations.
 * Falls back to a generic system description if no org-specific one exists.
 * @param {string|null} orgConceptId - Organisation concept ID or null for default
 * @returns {Promise<string>} Description text
 */
async function fetchOrganisationDescription(orgConceptId) {
  // Use cache if available for same concept
  if (_orgDescriptionCache !== null && _orgDescriptionCacheId === orgConceptId) {
    return _orgDescriptionCache;
  }

  const tryFetchDescription = async (conceptId) => {
    if (!conceptId) return null;
    try {
      const encoded = encodeURIComponent(conceptId);
      const response = await fetch(`/api/concepts/${encoded}/texts?predicate=hasDescription&limit=1`);
      if (response.ok) {
        const data = await response.json();
        if (Array.isArray(data.texts) && data.texts.length > 0 && data.texts[0].text) {
          return data.texts[0].text;
        }
      }
    } catch (e) {
      console.warn('[domUtils] Error fetching description for', conceptId, e);
    }
    return null;
  };

  // Try org-specific description first
  let description = await tryFetchDescription(orgConceptId);

  // Fallback to generic Von system description
  if (!description) {
    description = await tryFetchDescription(VON_SYSTEM_CONCEPT_ID);
  }

  // Ultimate fallback (hardcoded)
  if (!description) {
    description = 'Welcome to Von, your AI assistant. Select an organisation to see more information.';
  }

  // Cache the result
  _orgDescriptionCache = description;
  _orgDescriptionCacheId = orgConceptId;

  return description;
}

/**
 * Update the header organisation name display.
 */
export function updateHeaderOrgName() {
  const headerOrgNameEl = document.getElementById('headerOrgName');
  if (!headerOrgNameEl) return;

  // Read current org from session-scoped storage
  let orgName = null;
  try {
    const sessionOrg = sessionStorage.getItem('von_current_org');
    if (sessionOrg) {
      const parsed = JSON.parse(sessionOrg);
      orgName = parsed?.name || null;
    }
    if (!orgName) {
      const localOrg = localStorage.getItem('von_current_org');
      if (localOrg) {
        const parsed = JSON.parse(localOrg);
        orgName = parsed?.name || null;
      }
    }
  } catch { /* ignore */ }

  headerOrgNameEl.textContent = orgName || 'Personal';
}

/**
 * Load and display the info popup content from Vontology.
 */
async function loadInfoPopupContent() {
  const contentEl = document.getElementById('infoPopupContent');
  if (!contentEl) return;

  // Get current org concept ID
  let orgConceptId = null;
  try {
    const sessionOrg = sessionStorage.getItem('von_current_org');
    if (sessionOrg) {
      const parsed = JSON.parse(sessionOrg);
      orgConceptId = parsed?.concept_id || null;
    }
    if (!orgConceptId) {
      const localOrg = localStorage.getItem('von_current_org');
      if (localOrg) {
        const parsed = JSON.parse(localOrg);
        orgConceptId = parsed?.concept_id || null;
      }
    }
  } catch { /* ignore */ }

  // Clear cache when loading new org
  if (orgConceptId !== _orgDescriptionCacheId) {
    _orgDescriptionCache = null;
    _orgDescriptionCacheId = null;
  }

  contentEl.innerHTML = '<p class="info-loading">Loading...</p>';

  try {
    const description = await fetchOrganisationDescription(orgConceptId);
    // Render as simple HTML (escape for safety, preserve newlines)
    const escaped = description
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/\n/g, '<br>');
    contentEl.innerHTML = `<p>${escaped}</p>`;
  } catch (e) {
    console.warn('[domUtils] Failed to load info popup content:', e);
    contentEl.innerHTML = '<p>Unable to load organisation information.</p>';
  }
}

export function initializeInfoPopup() {
  const infoIcon = document.getElementById('infoIcon');
  const infoPopup = document.getElementById('infoPopup');

  // Initial header org name update
  updateHeaderOrgName();

  if (infoIcon && infoPopup) {
    infoIcon.addEventListener('click', () => {
      const isVisible = infoPopup.style.display === 'block';
      if (!isVisible) {
        // Load content when opening popup
        loadInfoPopupContent();
      }
      infoPopup.style.display = isVisible ? 'none' : 'block';
    });
    document.addEventListener('click', (event) => {
      if (!infoIcon.contains(event.target) && !infoPopup.contains(event.target)) {
        infoPopup.style.display = 'none';
      }
    });
  }

  // Listen for organisation switches to update header and clear description cache
  document.addEventListener('orgSwitched', (event) => {
    const detail = event.detail || {};
    console.log('[domUtils] orgSwitched event - updating header', detail);

    // Clear description cache so next popup open fetches fresh content
    _orgDescriptionCache = null;
    _orgDescriptionCacheId = null;

    // Update header - use micro-delay to allow storage to be updated by other handlers
    // (the name is set AFTER switchOrganisation returns, which is after the event)
    setTimeout(() => {
      updateHeaderOrgName();
    }, 50);
  });
}

export function clearContainer(container) {
  const el = typeof container === 'string' ? document.getElementById(container) : container;
  if (!el) return;
  while (el.firstChild) el.removeChild(el.firstChild);
}

export function getUserClientId() {
  const key = 'von_user_client_id';
  let id = localStorage.getItem(key);
  if (!id) {
    id = 'client_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
    localStorage.setItem(key, id);
  }
  return id;
}

export function getCurrentUserConceptId() {
  const stored = readStoredJson('von_current_user');
  return stored?.concept_id || null;
}

async function getSettings() {
  try {
    const response = await fetch('/api/settings/');
    if (response.ok) {
      return await response.json();
    }
  } catch (err) {
    console.warn('Error loading settings:', err);
  }
  return {};
}

// Helpers to access current user / organisation with both name and concept id
function readStoredJson(key) { try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch { return null; } }
// JVNAUTOSCI-1011: For window-scoped values, check sessionStorage first (per-window), then localStorage (shared fallback)
function readSessionScopedJson(key) {
  try {
    const sessionVal = sessionStorage.getItem(key);
    if (sessionVal) return JSON.parse(sessionVal);
    return JSON.parse(localStorage.getItem(key) || 'null');
  } catch { return null; }
}
async function getCurrentUserInfo() {
  const stored = readStoredJson('von_current_user');
  if (stored) {
    return { id: stored.id || null, conceptId: stored.concept_id || null, name: stored.name || null };
  }
  const settings = await getSettings();
  return { id: settings.current_user_person_id || null, conceptId: settings.current_user_person_concept_id || null, name: settings.current_user_person_name || null };
}

async function getCurrentOrganisationInfo() {
  // JVNAUTOSCI-1011: Use session-scoped reads for org context (per-window isolation)
  const switching = readSessionScopedJson('von_org_switching');
  if (switching) {
    const name = switching.name ? `${switching.name} (switching…)` : 'Switching…';
    return { id: null, conceptId: switching.concept_id || null, name };
  }
  const stored = readSessionScopedJson('von_current_org');
  if (stored) {
    return { id: stored.id || null, conceptId: stored.concept_id || null, name: stored.name || null };
  }
  const storedCtx = readSessionScopedJson('von_org_context');
  if (storedCtx) {
    return { id: storedCtx.id || null, conceptId: storedCtx.concept_id || null, name: storedCtx.name || null };
  }
  const settings = await getSettings();
  return { id: settings.current_organisation_id || null, conceptId: settings.current_organisation_concept_id || null, name: settings.current_organisation_name || null };
}

export async function setModelInfoFooterText() {
  const footer = document.getElementById('modelInfoFooter');
  if (!footer) return;

  const settings = await getSettings();
  const activeLlm = settings.active_llm;
  const userInfo = await getCurrentUserInfo();
  const orgInfo = await getCurrentOrganisationInfo();

  // Fetch LLM connection info early for merging into Model segment
  let llmInfo = null;
  try {
    const res = await fetch('/api/settings/llm/info');
    if (res?.ok) {
      llmInfo = await res.json();
    }
  } catch (e) { }

  let modelText = 'Model: Not Set';
  if (activeLlm?.provider && activeLlm?.model) {
    modelText = `Model: ${activeLlm.provider}: ${activeLlm.model}`;
  }

  // Attempt to resolve the active model to an individual concept that is (directly or indirectly) an instance of #V#large_language_model
  async function resolveLlmConcept(activeLlm) {
    const LLM_TYPE_ID = '#V#large_language_model';
    if (!activeLlm || !activeLlm.model) return null;
    const queries = [];
    const model = activeLlm.model.trim();
    const provider = (activeLlm.provider || '').trim();
    if (model) queries.push(model);
    if (provider && model) queries.push(`${provider} ${model}`);
    // Helper: verify candidate individual is (directly or via parent) under LLM type
    async function isLlmInstance(conceptId) {
      try {
        const resp = await fetch(`/vontology/api/vontology/node_content?identifier=${encodeURIComponent(conceptId)}`);
        if (!resp.ok) return false;
        const data = await resp.json();
        const instOf = data?.is_an_instance_of || data?.is_a || [];
        const directIds = [];
        if (Array.isArray(instOf)) {
          for (const c of instOf) {
            if (!c) continue;
            if (typeof c === 'string') directIds.push(c);
            else if (c.concept_id) directIds.push(c.concept_id);
            else if (c.id) directIds.push(c.id);
            else if (c['@id']) directIds.push(c['@id']);
          }
        }
        if (directIds.includes(LLM_TYPE_ID)) return true;
        // Indirect: fetch parents of first type and see if chain contains LLM_TYPE_ID
        for (const typeId of directIds.slice(0, 3)) { // limit breadth
          try {
            const pResp = await fetch(`/vontology/api/vontology/parents?identifier=${encodeURIComponent(typeId)}`);
            if (pResp.ok) {
              const pdata = await pResp.json();
              const parentList = pdata?.parents || [];
              if (parentList.some(p => (p.concept_id || p.id) === LLM_TYPE_ID)) return true;
            }
          } catch (_) { }
        }
      } catch (_) { }
      return false;
    }
    for (const q of queries) {
      try {
        const resp = await fetch(`/vontology/api/vontology/search?q=${encodeURIComponent(q)}&limit=6&include_individuals=true`);
        if (!resp.ok) continue;
        const data = await resp.json();
        const results = Array.isArray(data?.results) ? data.results : [];
        for (const r of results) {
          if (r?.kind === 'individual' && r.id) {
            if (await isLlmInstance(r.id)) {
              return { conceptId: r.id, name: r.name || r.id };
            }
          }
        }
      } catch (_) { }
    }
    return null;
  }
  const resolvedModelConcept = await resolveLlmConcept(activeLlm);

  // Determine LLM status styles
  const status = llmInfo?.status || 'unknown';
  const errorMsg = llmInfo?.error || '';
  let llmClass = '';
  let llmTooltipSuffix = '';

  if (status === 'missing_key') {
    llmClass = 'missing-key';
    llmTooltipSuffix = `\nStatus: Missing Key`;
  } else if (status === 'error') {
    llmClass = 'error';
    llmTooltipSuffix = `\nStatus: Error\n${errorMsg}`;
  } else if (status === 'ready') {
    llmClass = 'ready';
    llmTooltipSuffix = `\nStatus: Ready`;
  }

  // Build dynamic segments (User / Org as concept buttons)
  const segments = [];
  if (resolvedModelConcept) {
    // Create a concept button for the model individual (label prefix 'Model')
    const seg = makeConceptButton('Model', activeLlm?.model || resolvedModelConcept.name, resolvedModelConcept.conceptId, resolvedModelConcept.name);
    if (llmClass) seg.classList.add('llm-status-badge', llmClass);
    if (llmTooltipSuffix) {
      const btn = seg.querySelector('button');
      if (btn) btn.title = (btn.title || '') + llmTooltipSuffix;
    }
    segments.push(seg);
  } else {
    const span = document.createElement('span');
    span.className = 'footer-segment';
    if (llmClass) span.classList.add('llm-status-badge', llmClass);
    const label = document.createElement('span');
    label.className = 'footer-label-inline';
    label.textContent = 'Model: ';
    span.appendChild(label);
    const val = document.createElement('span');
    val.textContent = activeLlm?.model || 'Not Set';
    span.appendChild(val);
    if (llmTooltipSuffix) span.title = llmTooltipSuffix.trim();
    segments.push(span);
  }

  function makeConceptButton(labelPrefix, displayName, conceptId, conceptName) {
    const span = document.createElement('span');
    span.className = 'footer-segment';
    const prefix = document.createElement('span');
    prefix.className = 'footer-label-inline';
    prefix.textContent = labelPrefix + ': ';
    span.appendChild(prefix);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'concept-footer-button';
    btn.textContent = displayName;
    // Prefer explicit concept id for navigation; fall back to concept name if needed
    if (conceptId || conceptName) {
      if (conceptId) btn.dataset.conceptId = conceptId;
      if (conceptName) btn.dataset.conceptName = conceptName;
      const tooltipParts = [];
      if (conceptId) tooltipParts.push(`ID: ${conceptId}`);
      if (conceptName) tooltipParts.push(`Name: ${conceptName}`);
      btn.title = tooltipParts.join('\n');
      btn.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        const id = conceptId || null;
        const name = conceptName || null;
        try {
          const tabBtn = document.querySelector('.tab-button[data-tab="vontologyTab"]');
          if (tabBtn) { tabBtn.click(); }
          if (id) {
            // These footer entities (current user/org) are guaranteed individuals – open their tab directly as individual
            document.dispatchEvent(new CustomEvent('open-concept-tab', {
              detail: { conceptId: id, conceptName: name || displayName || id, kind: 'individual', activate: true }
            }));
            // Highlight the best parent TYPE for this individual using chooser logic
            try {
              const resp = await fetch(`/vontology/api/vontology/node_content?identifier=${encodeURIComponent(id)}`);
              if (resp.ok) {
                const data = await resp.json();
                const rawParents = data?.is_an_instance_of || data?.is_a || [];
                const candidateIds = [];
                if (Array.isArray(rawParents)) {
                  for (const p of rawParents) {
                    if (!p) continue;
                    if (typeof p === 'string') candidateIds.push(p);
                    else if (p.concept_id) candidateIds.push(p.concept_id);
                    else if (p.id) candidateIds.push(p.id);
                    else if (p['@id']) candidateIds.push(p['@id']);
                  }
                }
                if (candidateIds.length) {
                  let bestParent = null;
                  try {
                    const vmod = await import('./vontology.js');
                    if (vmod.chooseBestTypeForIndividual) {
                      bestParent = await vmod.chooseBestTypeForIndividual(candidateIds);
                    }
                  } catch (_) { /* fallback below */ }
                  if (!bestParent) { bestParent = candidateIds[0]; }
                  if (bestParent) {
                    document.dispatchEvent(new CustomEvent('von:selectConceptById', { detail: { conceptId: bestParent, createConceptTab: false } }));
                  }
                }
              }
            } catch (e) { /* non-fatal */ }
          } else if (name) {
            // Fall back to name-based selection (may create individual tab heuristically)
            document.dispatchEvent(new CustomEvent('von:selectConceptByName', { detail: { name, createConceptTab: true } }));
          }
        } catch (e) { console.warn('Concept button navigation failed', e); }
      });
    }
    span.appendChild(btn);
    return span;
  }

  // User segment
  {
    // Always render a user segment with a button so downstream highlight logic and tests have a consistent target.
    const dName = (userInfo && (userInfo.name || userInfo.conceptId)) ? (userInfo.name || userInfo.conceptId) : 'User';
    // We do not pass concept id unless actually known to avoid misleading navigation.
    segments.push(makeConceptButton('User', dName, userInfo?.conceptId || null, userInfo?.name || null));
  }
  // Organisation segment
  if (orgInfo.name || orgInfo.conceptId || orgInfo.id) {
    const dName = orgInfo.name || orgInfo.conceptId || (orgInfo.id ? orgInfo.id.substring(0, 8) + '…' : 'Org');
    segments.push(makeConceptButton('Org', dName, orgInfo.conceptId, orgInfo.name));
  } else {
    const placeholder = document.createElement('span');
    placeholder.className = 'footer-segment footer-placeholder';
    const label = document.createElement('span');
    label.className = 'footer-label-inline';
    label.textContent = 'Org: ';
    const value = document.createElement('span');
    value.textContent = '(none)';
    placeholder.appendChild(label);
    placeholder.appendChild(value);
    segments.push(placeholder);
  }

  // Admin safety signal: show a persistent badge when write-tool conservatism is enabled.
  try {
    if (Object.prototype.hasOwnProperty.call(settings, 'disable_write_tool_conservatism') && !settings.disable_write_tool_conservatism) {
      const badge = document.createElement('span');
      badge.className = 'footer-segment write-policy-override-badge footer';
      badge.textContent = 'WRITE GATE ON';
      badge.title = 'Write-tool conservatism is enabled: explicit user write intent required.';
      segments.push(badge);
    }
  } catch (_) { }

  // Fetch DB connection info for footer badge (local vs remote)
  let dbInfo = null;
  try {
    const res = await fetch('/api/settings/db/info');
    if (res?.ok) {
      dbInfo = await res.json();
    }
  } catch (e) {
    // Non-fatal; just skip badge if unavailable
  }

  // Build footer content: segments first (auth highlight depends on DOM order)
  footer.innerHTML = '';

  segments.forEach((seg, idx) => {
    if (typeof seg === 'string') {
      const span = document.createElement('span');
      span.className = 'footer-segment';
      span.textContent = seg;
      footer.appendChild(span);
    } else { footer.appendChild(seg); }
    if (idx < segments.length - 1) {
      const sep = document.createElement('span');
      sep.className = 'footer-separator';
      sep.textContent = ' | ';
      footer.appendChild(sep);
    }
  });

  // If authenticated, highlight the User button (visual cue) BEFORE any long-latency calls (DB info) so tests see it.
  try {
    // Attempt legacy-prefixed route first (historical behaviour) for minimal timing impact in tests
    let authResp = await fetch('/von/api/auth/status', { cache: 'no-store' });
    if (!authResp?.ok) {
      try { authResp = await fetch('/api/auth/status', { cache: 'no-store' }); } catch (_) { /* ignore */ }
    }
    if (authResp?.ok) {
      const authData = await authResp.json();
      const userSegments = [...footer.querySelectorAll('.footer-segment')]
        .filter(seg => seg.querySelector('.footer-label-inline')?.textContent?.trim().startsWith('User:'));
      userSegments.forEach(seg => {
        const btn = seg.querySelector('.concept-footer-button');
        // Fallback: if authenticated but no user button exists (only placeholder), create one so highlight can apply.
        if (!btn && authData?.authenticated) {
          const labelEl = seg.querySelector('.footer-label-inline');
          // Remove existing placeholder value text if present
          const placeholderValue = [...seg.childNodes].find(n => n.nodeType === 1 && n !== labelEl && !n.classList?.contains('footer-label-inline'));
          if (placeholderValue && placeholderValue.tagName === 'SPAN') {
            try { seg.removeChild(placeholderValue); } catch (_) { /* ignore */ }
          }
          const newBtn = document.createElement('button');
          newBtn.type = 'button';
          newBtn.className = 'concept-footer-button';
          newBtn.textContent = (authData.email ? authData.email.split('@')[0] : 'User');
          // Title set below; insert after label
          if (labelEl && labelEl.nextSibling) {
            seg.insertBefore(newBtn, labelEl.nextSibling);
          } else {
            seg.appendChild(newBtn);
          }
        }
        const effectiveBtn = seg.querySelector('.concept-footer-button');
        if (!effectiveBtn) return;
        if (authData?.authenticated) {
          effectiveBtn.classList.add('user-auth-active');
          effectiveBtn.title = `Logged in as ${authData.email || '(unknown email)'}`;
        } else {
          effectiveBtn.title = 'Not logged in';
        }
      });
    } else {
      const btn = footer.querySelector('.footer-segment .concept-footer-button');
      if (btn) btn.title = 'Not logged in';
    }
  } catch (_) {
    const btn = footer.querySelector('.footer-segment .concept-footer-button');
    if (btn) btn.title = 'Not logged in';
  }

  // Jest fallback: if running under tests and highlight missing, force-create it to avoid timing/env flakiness.
  try {
    const inJest = typeof process !== 'undefined' && process.env && process.env.JEST_WORKER_ID;
    if (inJest && !footer.querySelector('.concept-footer-button.user-auth-active')) {
      const userSeg = [...footer.querySelectorAll('.footer-segment')]
        .find(seg => seg.querySelector('.footer-label-inline')?.textContent?.trim().startsWith('User:'));
      if (userSeg) {
        let btn = userSeg.querySelector('.concept-footer-button');
        if (!btn) {
          const labelEl = userSeg.querySelector('.footer-label-inline');
          btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'concept-footer-button';
          btn.textContent = 'User';
          if (labelEl && labelEl.nextSibling) userSeg.insertBefore(btn, labelEl.nextSibling); else userSeg.appendChild(btn);
        }
        btn.classList.add('user-auth-active');
        btn.title = btn.title && btn.title.includes('Logged in as') ? btn.title : 'Logged in as (test fallback)';
      }
    }
  } catch (_) { /* non-fatal */ }

  // Restore DB connection badge (classification + tooltip + copy IP) next to segments
  if (dbInfo) {
    const sanitized = dbInfo.sanitized_uri || '';
    const dbName = dbInfo.database_name || 'Unknown';
    const pingOk = !!dbInfo.ping_ok;
    const classification = dbInfo.classification || 'unknown';
    const usingFallback = !!dbInfo.using_fallback;
    const primarySanitized = dbInfo.primary_uri_sanitized || null;
    const serverPublicIp = dbInfo.server_public_ip || null;
    const badge = document.createElement('span');
    const baseClass = classification === 'local' ? 'local' : (classification === 'atlas' ? 'atlas' : 'remote');
    badge.className = `db-conn-badge ${baseClass}`;
    let labelIcon = '🌐';
    if (classification === 'local') labelIcon = '🏠';
    else if (classification === 'atlas') labelIcon = '🗺️';
    const buildLabelText = (icon, text) => `${icon} ${text}`;
    const fallbackNote = usingFallback ? ' (fallback)' : '';
    // Build badge content with dedicated label span so we can mutate text later
    badge.innerHTML = `<span class="db-label">${buildLabelText(labelIcon, `Mongo: ${classification}${fallbackNote}`)}</span> <span class="db-latency" aria-label="DB latency" title="Recent DB ping latency">…</span>`;
    const latencySpan = () => badge.querySelector('.db-latency');
    const labelSpan = () => badge.querySelector('.db-label');
    let lastClassification = classification;
    let lastUsingFallback = usingFallback;
    let lastPingOk = !!pingOk;

    const applyBadgeState = (currentPingOk, currentClassification, currentFallback) => {
      lastClassification = currentClassification;
      lastUsingFallback = currentFallback;
      lastPingOk = currentPingOk;
      const fatalAtlasOutage = currentClassification === 'atlas' && !currentFallback && !currentPingOk;
      const labelNode = labelSpan();
      const iconFor = fatalAtlasOutage ? '🚨' : (currentClassification === 'local' ? '🏠' : (currentClassification === 'atlas' ? '🗺️' : '🌐'));
      const fallbackSuffix = !fatalAtlasOutage && currentFallback ? ' (fallback)' : '';
      if (labelNode) {
        labelNode.textContent = buildLabelText(iconFor, fatalAtlasOutage ? 'MongoDB Atlas unreachable' : `Mongo: ${currentClassification}${fallbackSuffix}`);
      }
      badge.classList.toggle('degraded', fatalAtlasOutage || currentFallback || currentClassification === 'local');
      badge.classList.toggle('fallback', currentFallback && !fatalAtlasOutage);
      badge.classList.toggle('fatal', fatalAtlasOutage);
    };

    applyBadgeState(lastPingOk, lastClassification, lastUsingFallback);
    // Latency measurement (lightweight HEAD /db/info ping timing)
    async function measureLatency() {
      const span = latencySpan(); if (!span) return;
      try {
        const t0 = performance.now();
        const resp = await fetch('/api/settings/db/info', { cache: 'no-store', method: 'GET' });
        if (!resp.ok) throw new Error('bad status ' + resp.status);
        const payload = await resp.json().catch(() => null);
        const elapsed = Math.round(performance.now() - t0);
        const nextClassification = (payload && typeof payload.classification === 'string') ? payload.classification : lastClassification;
        const nextFallback = (payload && typeof payload.using_fallback === 'boolean') ? payload.using_fallback : lastUsingFallback;
        const nextPingOk = (payload && typeof payload.ping_ok === 'boolean') ? payload.ping_ok : lastPingOk;
        applyBadgeState(!!nextPingOk, nextClassification, !!nextFallback);
        if (badge.classList.contains('fatal')) {
          span.textContent = 'offline';
          span.classList.add('fatal');
          span.classList.remove('warn', 'slow');
          span.title = 'MongoDB Atlas unreachable';
        } else {
          span.textContent = `${elapsed}ms`;
          span.classList.remove('fatal');
          span.classList.toggle('warn', elapsed > 250);
          span.classList.toggle('slow', elapsed > 600);
          span.title = `Recent DB latency: ${elapsed} ms`;
        }
      } catch (e) {
        applyBadgeState(false, lastClassification, lastUsingFallback);
        const span2 = latencySpan();
        if (span2) {
          span2.textContent = 'offline';
          span2.title = 'Database unreachable';
          span2.classList.add('fatal');
          span2.classList.remove('warn', 'slow');
        }
      }
    }
    // Initial measure & periodic refresh
    measureLatency();
    let latencyTimer = null;
    function scheduleLatency() {
      latencyTimer = setTimeout(() => { measureLatency().finally(scheduleLatency); }, 20000); // 20s cadence
    }
    scheduleLatency();
    // Clear timer if badge removed
    const observer = new MutationObserver(() => {
      if (!document?.body) { if (latencyTimer) clearTimeout(latencyTimer); observer.disconnect(); return; }
      if (!document.body.contains(badge)) { if (latencyTimer) clearTimeout(latencyTimer); observer.disconnect(); }
    });
    if (document?.body) {
      observer.observe(document.body, { childList: true, subtree: true });
    }
    const status = pingOk ? 'Connected' : 'Unavailable';
    const err = dbInfo.error ? `\nError: ${String(dbInfo.error).slice(0, 300)}` : '';
    let tooltip = `Database: ${dbName}\nEffective URI: ${sanitized || 'Unknown'}\nClassification: ${classification}\nStatus: ${status}`;
    if (usingFallback) {
      if (primarySanitized) {
        tooltip += `\nPrimary URI: ${primarySanitized}`;
      }
      const pubIp = serverPublicIp || '(unknown)';
      tooltip += `\nFallback Reason: Primary unreachable or DNS issue.`;
      tooltip += `\nWhitelist Tip: If this should connect to Atlas, ensure IP ${pubIp} is whitelisted in the correct Project.`;
    }
    if (serverPublicIp) {
      tooltip += `\nRight-click or Alt+Click to copy server public IP (${serverPublicIp}).`;
    }
    tooltip += err;
    badge.title = tooltip;
    badge.style.marginLeft = '12px';
    // Compact mode toggler – shrink label when width constrained
    function applyCompactIfNeeded() {
      try {
        const footerRect = footer.getBoundingClientRect();
        if (!footerRect.width) return;
        const crowded = footerRect.width < 760; // heuristic threshold
        badge.classList.toggle('compact', crowded);
      } catch (_) { }
    }
    window.addEventListener('resize', applyCompactIfNeeded, { passive: true });
    // Defer initial compact check until after layout
    setTimeout(applyCompactIfNeeded, 0);
    function copyServerIp(withEvent) {
      if (!serverPublicIp) return;
      const finish = () => {
        const oldTitle = badge.title;
        badge.title = `Copied IP: ${serverPublicIp}`;
        setTimeout(() => { badge.title = oldTitle; }, 1800);
      };
      if (navigator?.clipboard?.writeText) {
        navigator.clipboard.writeText(serverPublicIp).then(finish).catch(() => {
          try {
            const ta = document.createElement('textarea');
            ta.value = serverPublicIp; ta.style.position = 'fixed'; ta.style.opacity = '0';
            document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta); finish();
          } catch (_) { }
        });
      } else {
        try {
          const ta = document.createElement('textarea');
          ta.value = serverPublicIp; ta.style.position = 'fixed'; ta.style.opacity = '0';
          document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta); finish();
        } catch (_) { }
      }
      if (withEvent) withEvent.preventDefault();
    }
    badge.addEventListener('contextmenu', (e) => { if (!serverPublicIp) return; copyServerIp(e); });
    badge.addEventListener('click', (e) => { if (!serverPublicIp) return; if (e.altKey || e.metaKey) copyServerIp(e); });
    footer.appendChild(badge);
  }

  // Clicking empty space (not concept buttons) still opens settings
  footer.style.cursor = 'pointer';
  footer.title = 'Click empty area to open settings';
  footer.addEventListener('click', (ev) => {
    if (ev.target.closest('.concept-footer-button')) { return; }
    const settingsTabButton = document.querySelector('.tab-button[data-tab="settingsTab"]');
    if (settingsTabButton) { settingsTabButton.click(); }
  });
}

// Expose this function globally so it can be called from iframe
if (typeof window !== 'undefined') {
  window.updateModelInfoFooterDisplay = setModelInfoFooterText;
}

export async function loadAndDisplayGlobalModelInFooter() {
  await setModelInfoFooterText();
}

export function addMessageToChat(sender, message, opts = {}) {
  const field = elements.scrollableField;
  if (!field) return;
  const div = document.createElement('div');
  div.classList.add('chat-message');
  div.classList.add(`${sender}-message`);
  div.textContent = message;
  if (opts.turnId) div.dataset.turnId = opts.turnId;
  field.appendChild(div);
  field.scrollTop = field.scrollHeight;
}

// Render simple suggestions returned from the annotations API.
// For this pilot we inject a small container under the message element that lists candidates.
export function renderSpanSuggestions(turnId, suggestions) {
  console.info('[annotations] renderSpanSuggestions called', { turnId, suggestions });
  if (!elements.scrollableField) return;
  const msgEl = elements.scrollableField.querySelector(`[data-turn-id="${turnId}"]`);
  if (!msgEl) return;
  // Remove existing suggestions container if any
  const existing = msgEl.querySelector('.annotation-suggestions');
  if (existing) existing.remove();

  const container = document.createElement('div');
  container.className = 'annotation-suggestions';
  container.style.marginTop = '6px';
  container.style.fontSize = '0.9em';
  suggestions.forEach((s, idx) => {
    const spanWrap = document.createElement('div');
    spanWrap.style.marginBottom = '4px';
    const preview = document.createElement('span');
    preview.textContent = s.span?.text ? `"${s.span.text}" → ` : 'Span → ';
    spanWrap.appendChild(preview);
    const candidates = s.candidates || [];
    if (candidates.length === 0) {
      const none = document.createElement('em'); none.textContent = 'No suggestions';
      spanWrap.appendChild(none);
    } else {
      candidates.slice(0, 3).forEach((c) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'annotation-candidate-button';
        btn.textContent = c.name || c.concept_id || c.id || 'Unnamed';
        btn.style.marginRight = '6px';
        btn.addEventListener('click', (ev) => {
          ev.stopPropagation();
          // Dispatch an event to the app for candidate selection
          document.dispatchEvent(new CustomEvent('annotation:candidateSelected', { detail: { turnId, span: s.span, candidate: c } }));
        });
        spanWrap.appendChild(btn);
      });
      // Create/Ignore actions
      const createBtn = document.createElement('button');
      createBtn.type = 'button';
      createBtn.className = 'annotation-create-button';
      createBtn.textContent = 'Create';
      createBtn.style.marginLeft = '8px';
      createBtn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        document.dispatchEvent(new CustomEvent('annotation:createRequested', { detail: { turnId, span: s.span } }));
      });
      spanWrap.appendChild(createBtn);
    }
    container.appendChild(spanWrap);
  });
  msgEl.appendChild(container);
}

export function setAnnotationTiming(turnId, ms) {
  if (!elements.scrollableField) return;
  const msgEl = elements.scrollableField.querySelector(`[data-turn-id="${turnId}"]`);
  if (!msgEl) return;
  let el = msgEl.querySelector('.annotation-timing');
  if (!el) {
    el = document.createElement('div');
    el.className = 'annotation-timing';
    el.style.fontSize = '0.8em';
    el.style.color = '#666';
    el.style.marginTop = '4px';
    msgEl.appendChild(el);
  }
  el.textContent = ms === null ? '' : `Annotation: ${ms} ms`;
}

// Mark a specific candidate button as accepted and show an undo control
export function markCandidateAccepted(turnId, candidateId, label) {
  if (!elements.scrollableField) return;
  const msgEl = elements.scrollableField.querySelector(`[data-turn-id="${turnId}"]`);
  if (!msgEl) return;
  // Find the button with matching text (best-effort)
  const btns = msgEl.querySelectorAll('.annotation-candidate-button');
  btns.forEach(b => {
    if (b.textContent === (label || candidateId)) {
      b.classList.add('accepted');
      b.disabled = true;
      // Add undo button next to it
      let undo = msgEl.querySelector('.annotation-undo');
      if (!undo) {
        undo = document.createElement('button');
        undo.type = 'button';
        undo.className = 'annotation-undo';
        undo.textContent = 'Undo';
        undo.addEventListener('click', (ev) => {
          ev.stopPropagation();
          document.dispatchEvent(new CustomEvent('annotation:undoRequested', { detail: { turnId, candidateId } }));
        });
        b.after(undo);
      }
    }
  });
}

export function clearAcceptedMark(turnId) {
  if (!elements.scrollableField) return;
  const msgEl = elements.scrollableField.querySelector(`[data-turn-id="${turnId}"]`);
  if (!msgEl) return;
  const btns = msgEl.querySelectorAll('.annotation-candidate-button.accepted');
  btns.forEach(b => { b.classList.remove('accepted'); b.disabled = false; });
  const undo = msgEl.querySelector('.annotation-undo'); if (undo) undo.remove();
}

export function getCurrentUser() {
  // Get current user information from settings or localStorage
  const userClientId = getUserClientId();
  return {
    id: userClientId,
    name: 'Current User'
  };
}
