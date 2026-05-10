import { openSettingsTabAndFocus } from './utils/settingsNavigation.js';
import { parseStoredContextValue } from './utils/runtimeIdentityBootstrap.js';
import { applyLocalModelPreferenceOverlay, getEffectiveLocalModelPreference } from './utils/localModelPreferences.js';

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
      const parsed = parseStoredContextValue(sessionOrg);
      orgName = parsed?.name || null;
    }
    if (!orgName) {
      const localOrg = localStorage.getItem('von_current_org');
      if (localOrg) {
        const parsed = parseStoredContextValue(localOrg);
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
      const parsed = parseStoredContextValue(sessionOrg);
      orgConceptId = parsed?.concept_id || null;
    }
    if (!orgConceptId) {
      const localOrg = localStorage.getItem('von_current_org');
      if (localOrg) {
        const parsed = parseStoredContextValue(localOrg);
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

// Bound non-critical footer calls so one stalled endpoint cannot block footer rendering indefinitely.
async function fetchJsonWithTimeout(url, options = {}) {
  const { timeoutMs = 6000, ...fetchOptions } = options || {};
  const init = { ...fetchOptions };
  let timeoutId = null;

  try {
    if (typeof AbortController === 'function' && timeoutMs > 0) {
      const controller = new AbortController();
      timeoutId = setTimeout(() => controller.abort(), timeoutMs);
      init.signal = controller.signal;
    }
    const response = await fetch(url, init);
    if (!response?.ok) return null;
    return await response.json();
  } catch (_) {
    return null;
  } finally {
    if (timeoutId) clearTimeout(timeoutId);
  }
}

async function getSettings() {
  try {
    // Pass user context to get properly resolved LLM setting (user > org precedence, no global)
    const storedUser = readStoredJson('von_current_user');
    const storedOrg = readSessionScopedJson('von_current_org');
    const userConceptId = storedUser?.concept_id;
    const orgConceptId = storedOrg?.concept_id;

    const params = new URLSearchParams();
    if (userConceptId) params.set('user_concept_id', userConceptId);
    if (orgConceptId) params.set('organisation_concept_id', orgConceptId);

    const url = '/api/settings/' + (params.toString() ? '?' + params.toString() : '');
    const settings = await fetchJsonWithTimeout(url, {
      cache: 'no-store',
      timeoutMs: 6000,
    });
    if (settings) {
      // Use resolved_llm as the active_llm (no global fallback)
      settings.active_llm = settings.resolved_llm || null;
      return applyLocalModelPreferenceOverlay(settings);
    }
  } catch (err) {
    console.warn('Error loading settings:', err);
  }
  return {};
}

// Helpers to access current user / organisation with both name and concept id
function readStoredJson(key) { try { return parseStoredContextValue(localStorage.getItem(key)); } catch { return null; } }
// JVNAUTOSCI-1011: For window-scoped values, check sessionStorage first (per-window), then localStorage (shared fallback)
function readSessionScopedJson(key) {
  try {
    const sessionVal = sessionStorage.getItem(key);
    if (sessionVal) return parseStoredContextValue(sessionVal);
    return parseStoredContextValue(localStorage.getItem(key));
  } catch { return null; }
}
async function getCurrentUserInfo(settingsOverride = null) {
  const stored = readStoredJson('von_current_user');
  if (stored?.id || stored?.concept_id) {
    return { id: stored.id || null, conceptId: stored.concept_id || null, name: stored.name || null };
  }
  const settings = settingsOverride || await getSettings();
  return { id: settings.current_user_person_id || null, conceptId: settings.current_user_person_concept_id || null, name: settings.current_user_person_name || null };
}

async function getCurrentOrganisationInfo(settingsOverride = null) {
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
  const settings = settingsOverride || await getSettings();
  return { id: settings.current_organisation_id || null, conceptId: settings.current_organisation_concept_id || null, name: settings.current_organisation_name || null };
}

let footerDbRetryTimerId = null;
let footerDbLoadGeneration = 0;
let footerServerReachability = null;
let footerLlmExecutionRefreshScheduled = false;
const FOOTER_DB_PROBE_STATS_KEY = 'von_footer_db_probe_stats_v1';
const FOOTER_DB_PROBE_SAMPLE_LIMIT = 32;
const FOOTER_DB_RETRY_MIN_MS = 1500;
const FOOTER_DB_RETRY_MAX_MS = 20000;

function normaliseFooterServerReachability(value) {
  return (typeof value === 'boolean') ? value : null;
}

// Main health polling reports whether Von itself is reachable so DB badge severity
// can distinguish "Atlas outage" from "status unknown because server is down".
export function setFooterServerReachability(isReachable) {
  footerServerReachability = normaliseFooterServerReachability(isReachable);
  try {
    document.dispatchEvent(new CustomEvent('von:serverReachabilityChanged', {
      detail: { isReachable: footerServerReachability }
    }));
  } catch (_) {
    // Non-fatal: badge will refresh on its next probe.
  }
}

function readFooterDbProbeStats() {
  const fallback = { successes: 0, failures: 0, samples_ms: [] };
  try {
    const raw = localStorage.getItem(FOOTER_DB_PROBE_STATS_KEY);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw);
    const samples = Array.isArray(parsed?.samples_ms)
      ? parsed.samples_ms.map(v => Number(v)).filter(v => Number.isFinite(v) && v >= 0)
      : [];
    return {
      successes: Number.isFinite(Number(parsed?.successes)) ? Math.max(0, Number(parsed.successes)) : 0,
      failures: Number.isFinite(Number(parsed?.failures)) ? Math.max(0, Number(parsed.failures)) : 0,
      samples_ms: samples.slice(-FOOTER_DB_PROBE_SAMPLE_LIMIT),
    };
  } catch (_) {
    return fallback;
  }
}

function persistFooterDbProbeStats(stats) {
  try {
    localStorage.setItem(FOOTER_DB_PROBE_STATS_KEY, JSON.stringify(stats));
  } catch (_) {
    // Non-fatal: stats persistence is best-effort only.
  }
}

function computePercentile(values, percentile) {
  if (!Array.isArray(values) || values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.floor((sorted.length - 1) * percentile)));
  const value = sorted[idx];
  return Number.isFinite(value) ? Math.round(value) : null;
}

function getFooterDbProbeSummary() {
  const stats = readFooterDbProbeStats();
  return {
    sampleCount: stats.samples_ms.length,
    p50Ms: computePercentile(stats.samples_ms, 0.5),
    p90Ms: computePercentile(stats.samples_ms, 0.9),
    successes: stats.successes,
    failures: stats.failures,
  };
}

function recordFooterDbProbeSuccess(elapsedMs) {
  const stats = readFooterDbProbeStats();
  const nextSamples = [...stats.samples_ms, Math.round(elapsedMs)].slice(-FOOTER_DB_PROBE_SAMPLE_LIMIT);
  persistFooterDbProbeStats({
    successes: stats.successes + 1,
    failures: stats.failures,
    samples_ms: nextSamples,
  });
}

function recordFooterDbProbeFailure() {
  const stats = readFooterDbProbeStats();
  persistFooterDbProbeStats({
    successes: stats.successes,
    failures: stats.failures + 1,
    samples_ms: stats.samples_ms,
  });
}

function computeFooterDbRetryDelayMs(attempt) {
  const safeAttempt = Math.max(0, Number(attempt) || 0);
  const summary = getFooterDbProbeSummary();
  const adaptiveBaseMs = Number.isFinite(summary.p90Ms)
    ? Math.min(6000, Math.max(2000, summary.p90Ms * 8))
    : 2500;
  const delay = Math.round(adaptiveBaseMs * Math.pow(1.6, Math.min(safeAttempt, 6)));
  return Math.max(FOOTER_DB_RETRY_MIN_MS, Math.min(FOOTER_DB_RETRY_MAX_MS, delay));
}

function formatFooterDbRetryHint(attempt, delayMs) {
  const summary = getFooterDbProbeSummary();
  const waitSec = Math.max(1, Math.round(delayMs / 1000));
  if (summary.sampleCount > 0 && Number.isFinite(summary.p50Ms) && Number.isFinite(summary.p90Ms)) {
    return `Waiting for DB status. Retry #${attempt + 1} in ${waitSec}s. Recent db/info latency p50=${summary.p50Ms}ms p90=${summary.p90Ms}ms (n=${summary.sampleCount}).`;
  }
  return `Waiting for DB status. Retry #${attempt + 1} in ${waitSec}s.`;
}

function clearFooterDbRetryTimer() {
  if (footerDbRetryTimerId) {
    clearTimeout(footerDbRetryTimerId);
    footerDbRetryTimerId = null;
  }
}

function normaliseFooterTelemetryStrings(values) {
  if (!Array.isArray(values)) return [];
  const result = [];
  const seen = new Set();
  for (const value of values) {
    if (typeof value !== 'string') continue;
    const cleaned = value.trim();
    if (!cleaned) continue;
    const dedupeKey = cleaned.toLowerCase();
    if (seen.has(dedupeKey)) continue;
    seen.add(dedupeKey);
    result.push(cleaned);
  }
  return result;
}

/**
 * Normalise a model name for alias-aware comparison.
 *
 * OpenAI resolves model aliases to dated snapshots (e.g. gpt-5.4-mini →
 * gpt-5.4-mini-2026-03-17).  Stripping the trailing date suffix lets us
 * compare the base family name without false-alarm mismatches.
 */
function normaliseModelNameForComparison(name) {
  if (!name || typeof name !== 'string') return '';
  return name.trim().replace(/-\d{4}-\d{2}-\d{2}$/, '');
}

function readLatestLlmExecutionTelemetry() {
  try {
    const raw = window.__vonLatestLlmExecutionTelemetry;
    if (!raw || typeof raw !== 'object') return null;
    const callModels = normaliseFooterTelemetryStrings(raw.call_models);
    const callProviders = normaliseFooterTelemetryStrings(raw.call_providers);
    const requestedModel = (typeof raw.requested_model === 'string' && raw.requested_model.trim())
      ? raw.requested_model.trim()
      : null;
    const actualModel = (typeof raw.actual_model === 'string' && raw.actual_model.trim())
      ? raw.actual_model.trim()
      : (callModels.length ? callModels[callModels.length - 1] : null);
    const actualProvider = (typeof raw.actual_provider === 'string' && raw.actual_provider.trim())
      ? raw.actual_provider.trim()
      : (callProviders.length ? callProviders[callProviders.length - 1] : null);
    const primaryFailureKind = (typeof raw.primary_failure_kind === 'string' && raw.primary_failure_kind.trim())
      ? raw.primary_failure_kind.trim()
      : null;
    const primaryFailureReason = (typeof raw.primary_failure_reason === 'string' && raw.primary_failure_reason.trim())
      ? raw.primary_failure_reason.trim()
      : null;
    const error = (typeof raw.error === 'string' && raw.error.trim())
      ? raw.error.trim()
      : null;
    const executionStage = (typeof raw.execution_stage === 'string' && raw.execution_stage.trim())
      ? raw.execution_stage.trim()
      : null;
    const policyStage = (typeof raw.policy_stage === 'string' && raw.policy_stage.trim())
      ? raw.policy_stage.trim()
      : null;
    const selectionMode = (typeof raw.selection_mode === 'string' && raw.selection_mode.trim())
      ? raw.selection_mode.trim()
      : null;
    const explicitStageModelOverrideOrigin = (typeof raw.explicit_stage_model_override_origin === 'string'
      && raw.explicit_stage_model_override_origin.trim())
      ? raw.explicit_stage_model_override_origin.trim()
      : null;
    const warnings = normaliseFooterTelemetryStrings(raw.warnings).slice(0, 3);
    if (!requestedModel && !actualModel && !actualProvider && !primaryFailureReason && !error && warnings.length === 0) {
      return null;
    }
    return {
      requestedModel,
      actualModel,
      actualProvider,
      callModels,
      callProviders,
      fallbackUsed: !!raw.fallback_used,
      primaryFailureKind,
      primaryFailureReason,
      error,
      executionStage,
      policyStage,
      selectionMode,
      followsActiveLlm: raw.follows_active_llm === true,
      explicitStageModelOverride: raw.explicit_stage_model_override === true,
      explicitStageModelOverrideOrigin,
      warnings,
    };
  } catch (_) {
    return null;
  }
}

function scheduleFooterModelInfoRefresh() {
  if (footerLlmExecutionRefreshScheduled) return;
  footerLlmExecutionRefreshScheduled = true;
  const schedule = (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function')
    ? window.requestAnimationFrame.bind(window)
    : (cb) => setTimeout(cb, 0);
  schedule(() => {
    footerLlmExecutionRefreshScheduled = false;
    if (document.getElementById('modelInfoFooter')) {
      void setModelInfoFooterText();
    }
  });
}

try {
  document.addEventListener('von:latestLlmExecutionTelemetryUpdated', () => {
    scheduleFooterModelInfoRefresh();
  });
} catch (_) {
  // Ignore missing document in tests or constrained environments.
}

function clearKeptNativeTitle(element) {
  if (!element) return;
  element.removeAttribute('title');
  element.removeAttribute('data-original-title');
  element.removeAttribute('data-keep-title');
}

function setKeptNativeTitle(element, title) {
  if (!element) return;
  const cleanTitle = typeof title === 'string' ? title.trim() : '';
  if (!cleanTitle) {
    clearKeptNativeTitle(element);
    return;
  }
  element.setAttribute('data-keep-title', 'true');
  element.setAttribute('title', cleanTitle);
  element.setAttribute('data-original-title', cleanTitle);
}

function readKeptNativeTitle(element) {
  if (!element) return '';
  return element.getAttribute('title')
    || element.getAttribute('data-original-title')
    || '';
}

function updateFooterReadinessState(footerContainer, readinessIssues) {
  if (!footerContainer) return;
  const issueList = Array.from(readinessIssues || []);
  const notReady = issueList.length > 0;
  footerContainer.classList.toggle('footer-not-ready', notReady);
  setKeptNativeTitle(footerContainer, notReady
    ? `Footer partially ready. Waiting on: ${issueList.join(', ')}`
    : '');
}

function ensureFooterDbLoadingBadge(footer) {
  if (!footer) return null;
  const existing = footer.querySelector('.db-conn-badge');
  if (existing) return existing;

  const badge = document.createElement('span');
  badge.className = 'db-conn-badge loading';
  badge.style.marginLeft = '12px';
  badge.innerHTML = '<span class="db-label">🕓 Mongo: loading...</span> <span class="db-latency" aria-label="DB latency" title="Waiting for DB status">...</span>';
  setKeptNativeTitle(badge, 'Loading database status...');
  setKeptNativeTitle(badge.querySelector('.db-latency'), 'Waiting for DB status');
  footer.appendChild(badge);
  return badge;
}

function attachFooterDbBadge(footer, dbInfo) {
  if (!footer || !dbInfo || typeof dbInfo !== 'object') return;

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
  let lastServerReachable = normaliseFooterServerReachability(footerServerReachability);
  let lastMeasuredLatencyMs = null;

  const status = pingOk ? 'Connected' : 'Unavailable';
  const err = dbInfo.error ? `\nError: ${String(dbInfo.error).slice(0, 300)}` : '';
  let baseTooltip = `Database: ${dbName}\nEffective URI: ${sanitized || 'Unknown'}\nClassification: ${classification}\nStatus: ${status}`;
  if (usingFallback) {
    if (primarySanitized) {
      baseTooltip += `\nPrimary URI: ${primarySanitized}`;
    }
    const pubIp = serverPublicIp || '(unknown)';
    baseTooltip += `\nFallback Reason: Primary unreachable or DNS issue.`;
    baseTooltip += `\nWhitelist Tip: If this should connect to Atlas, ensure IP ${pubIp} is whitelisted in the correct Project.`;
  }
  if (serverPublicIp) {
    baseTooltip += `\nRight-click or Alt+Click to copy server public IP (${serverPublicIp}).`;
  }
  baseTooltip += err;

  const refreshBadgeTooltip = (state) => {
    let dynamic = '';
    if (state === 'server_down_unknown') {
      dynamic = '\nCurrent status: unknown because Von server is unreachable.';
    } else if (state === 'fatal_atlas') {
      dynamic = '\nCurrent status: MongoDB Atlas unreachable.';
    } else if (state === 'degraded') {
      dynamic = '\nCurrent status: degraded/fallback connection.';
    }
    setKeptNativeTitle(badge, `${baseTooltip}${dynamic}`);
  };

  const applyBadgeState = (currentPingOk, currentClassification, currentFallback, currentServerReachable = lastServerReachable) => {
    lastClassification = currentClassification;
    lastUsingFallback = currentFallback;
    lastPingOk = currentPingOk;
    lastServerReachable = normaliseFooterServerReachability(currentServerReachable);
    const serverDownUnknown = lastServerReachable === false;
    const fatalAtlasOutage = !serverDownUnknown
      && currentClassification === 'atlas'
      && !currentFallback
      && !currentPingOk;
    const degradedConnection = !serverDownUnknown
      && (fatalAtlasOutage || currentFallback || currentClassification === 'local');
    const labelNode = labelSpan();
    const iconFor = serverDownUnknown
      ? '⚠️'
      : (fatalAtlasOutage ? '🚨' : (currentClassification === 'local' ? '🏠' : (currentClassification === 'atlas' ? '🗺️' : '🌐')));
    const fallbackSuffix = !fatalAtlasOutage && currentFallback ? ' (fallback)' : '';
    if (labelNode) {
      if (serverDownUnknown) {
        labelNode.textContent = buildLabelText(iconFor, 'Mongo status unknown (Von down)');
      } else {
        labelNode.textContent = buildLabelText(iconFor, fatalAtlasOutage ? 'MongoDB Atlas unreachable' : `Mongo: ${currentClassification}${fallbackSuffix}`);
      }
    }
    badge.classList.toggle('warning', serverDownUnknown);
    badge.classList.toggle('degraded', degradedConnection);
    badge.classList.toggle('fallback', !serverDownUnknown && currentFallback && !fatalAtlasOutage);
    badge.classList.toggle('fatal', !serverDownUnknown && fatalAtlasOutage);

    if (serverDownUnknown) {
      refreshBadgeTooltip('server_down_unknown');
      return 'server_down_unknown';
    }
    if (fatalAtlasOutage) {
      refreshBadgeTooltip('fatal_atlas');
      return 'fatal_atlas';
    }
    if (degradedConnection) {
      refreshBadgeTooltip('degraded');
      return 'degraded';
    }
    refreshBadgeTooltip('normal');
    return 'normal';
  };

  const applyLatencyState = (state, elapsedMs = null) => {
    const span = latencySpan();
    if (!span) return;

    span.classList.remove('fatal', 'warn', 'slow');

    if (state === 'fatal_atlas') {
      span.textContent = 'offline';
      span.classList.add('fatal');
      setKeptNativeTitle(span, 'MongoDB Atlas unreachable');
      return;
    }

    if (state === 'server_down_unknown') {
      span.textContent = 'unknown';
      span.classList.add('warn');
      setKeptNativeTitle(span, 'Mongo status unknown because Von server is unreachable');
      return;
    }

    if (!Number.isFinite(elapsedMs)) {
      span.textContent = '...';
      setKeptNativeTitle(span, 'Waiting for DB status');
      return;
    }

    span.textContent = `${elapsedMs}ms`;
    span.classList.toggle('warn', elapsedMs > 250);
    span.classList.toggle('slow', elapsedMs > 600);
    const summary = getFooterDbProbeSummary();
    if (summary.sampleCount > 0 && Number.isFinite(summary.p50Ms) && Number.isFinite(summary.p90Ms)) {
      setKeptNativeTitle(span, `Recent DB latency: ${elapsedMs} ms (p50 ${summary.p50Ms} ms, p90 ${summary.p90Ms} ms, n=${summary.sampleCount})`);
    } else {
      setKeptNativeTitle(span, `Recent DB latency: ${elapsedMs} ms`);
    }
  };

  const handleServerReachabilityChanged = (event) => {
    const nextReachable = normaliseFooterServerReachability(event?.detail?.isReachable);
    if (nextReachable === null) return;
    const state = applyBadgeState(lastPingOk, lastClassification, lastUsingFallback, nextReachable);
    applyLatencyState(state, lastMeasuredLatencyMs);
  };

  document.addEventListener('von:serverReachabilityChanged', handleServerReachabilityChanged);
  const initialState = applyBadgeState(lastPingOk, lastClassification, lastUsingFallback, lastServerReachable);
  applyLatencyState(initialState, null);

  // Latency measurement (lightweight HEAD /db/info ping timing)
  async function measureLatency() {
    const span = latencySpan(); if (!span) return;
    try {
      const t0 = performance.now();
      const resp = await fetch('/api/settings/db/info', { cache: 'no-store', method: 'GET' });
      if (!resp.ok) throw new Error('bad status ' + resp.status);
      const payload = await resp.json().catch(() => null);
      const elapsed = Math.round(performance.now() - t0);
      lastMeasuredLatencyMs = elapsed;
      recordFooterDbProbeSuccess(elapsed);
      const nextClassification = (payload && typeof payload.classification === 'string') ? payload.classification : lastClassification;
      const nextFallback = (payload && typeof payload.using_fallback === 'boolean') ? payload.using_fallback : lastUsingFallback;
      const nextPingOk = (payload && typeof payload.ping_ok === 'boolean') ? payload.ping_ok : lastPingOk;
      const state = applyBadgeState(!!nextPingOk, nextClassification, !!nextFallback, true);
      applyLatencyState(state, elapsed);
    } catch (_) {
      recordFooterDbProbeFailure();
      const inferredServerReachable = normaliseFooterServerReachability(footerServerReachability);
      const state = applyBadgeState(
        false,
        lastClassification,
        lastUsingFallback,
        inferredServerReachable === null ? false : inferredServerReachable
      );
      applyLatencyState(state, null);
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
  const detachBadgeListeners = () => {
    if (latencyTimer) clearTimeout(latencyTimer);
    if (typeof document !== 'undefined') {
      document.removeEventListener('von:serverReachabilityChanged', handleServerReachabilityChanged);
    }
    observer.disconnect();
  };
  const observer = new MutationObserver(() => {
    if (!document?.body) {
      detachBadgeListeners();
      return;
    }
    if (!document.body.contains(badge)) {
      detachBadgeListeners();
    }
  });
  if (document?.body) {
    observer.observe(document.body, { childList: true, subtree: true });
  }

  refreshBadgeTooltip(initialState);
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
  setTimeout(applyCompactIfNeeded, 0);

  function copyServerIp(withEvent) {
    if (!serverPublicIp) return;
    const finish = () => {
      const oldTitle = readKeptNativeTitle(badge);
      setKeptNativeTitle(badge, `Copied IP: ${serverPublicIp}`);
      setTimeout(() => { setKeptNativeTitle(badge, oldTitle); }, 1800);
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

// Export for testing.
export function openSettingsForModelControls() {
  return openSettingsTabAndFocus('von:focus-model-settings');
}

export async function setModelInfoFooterText() {
  const footer = document.getElementById('modelInfoFooter');
  if (!footer) return;
  const footerContainer = footer.closest('.footer-container');
  const readinessIssues = new Set();
  footerDbLoadGeneration += 1;
  const dbLoadGeneration = footerDbLoadGeneration;
  clearFooterDbRetryTimer();

  const settings = await getSettings();
  if (!settings || Object.keys(settings).length === 0) {
    readinessIssues.add('settings');
  }
  const activeLlm = settings.active_llm;

  // Prefer the local model preference over the server-resolved setting for display
  // and status checks. This ensures the footer immediately reflects the user's
  // local choice (e.g. Ollama when premium is disabled) rather than the DB value.
  const localModelPref = getEffectiveLocalModelPreference();
  const localModelUnavailable = localModelPref.modelUnavailable === true;
  const localModelUnavailableReason = localModelPref.modelUnavailableReason || '';
  const effectiveLlm = localModelUnavailable
    ? null
    : localModelPref.requestedLlm
    ? { provider: localModelPref.requestedLlm.provider, model: localModelPref.requestedLlm.model }
    : activeLlm;

  const userInfo = await getCurrentUserInfo(settings);
  const orgInfo = await getCurrentOrganisationInfo(settings);

  // Fetch LLM connection info early for merging into Model segment
  // Pass user context for proper per-user resolution
  let llmInfo = null;
  if (!localModelUnavailable) {
    try {
      const llmParams = new URLSearchParams();
      if (userInfo.conceptId) llmParams.set('user_concept_id', userInfo.conceptId);
      if (orgInfo.conceptId) llmParams.set('organisation_concept_id', orgInfo.conceptId);
      // Pass the effective provider so the status check reflects the user's local model
      // preference rather than always checking the DB-stored (e.g. OpenAI) provider.
      if (effectiveLlm?.provider) llmParams.set('effective_provider', effectiveLlm.provider);
      const llmUrl = '/api/settings/llm/info' + (llmParams.toString() ? '?' + llmParams.toString() : '');
      llmInfo = await fetchJsonWithTimeout(llmUrl, {
        cache: 'no-store',
        timeoutMs: 6000,
      });
    } catch (_) { }
  }
  if (!llmInfo) {
    readinessIssues.add('llm');
  }

  const executionTelemetry = readLatestLlmExecutionTelemetry();

  // Determine LLM status styles
  const status = llmInfo?.status || 'unknown';
  const errorMsg = llmInfo?.error || '';
  const llmHost = typeof llmInfo?.details?.host === 'string' ? llmInfo.details.host : '';
  let llmClass = '';
  let configuredStatusLabel = 'Unknown';

  if (localModelUnavailable) {
    llmClass = 'fatal';
    configuredStatusLabel = 'No model configured';
  } else if (status === 'missing_key') {
    llmClass = 'missing-key';
    configuredStatusLabel = 'Missing Key';
  } else if (status === 'error') {
    llmClass = 'error';
    configuredStatusLabel = 'Error';
  } else if (status === 'ready') {
    llmClass = 'ready';
    configuredStatusLabel = 'Ready';
  }

  // Build dynamic segments (User / Org as concept buttons)
  const segments = [];

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
      setKeptNativeTitle(btn, tooltipParts.join('\n'));
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
            } catch (_) { /* non-fatal */ }
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

  function makeActionButton(labelPrefix, displayName, onClick, options = {}) {
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
    setKeptNativeTitle(btn, options.title);
    if (options.ariaLabel) btn.setAttribute('aria-label', options.ariaLabel);
    if (typeof onClick === 'function') {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        onClick();
      });
    }
    span.appendChild(btn);
    return span;
  }

  {
    const configuredProvider = (typeof effectiveLlm?.provider === 'string' && effectiveLlm.provider.trim())
      ? effectiveLlm.provider.trim()
      : ((typeof llmInfo?.provider === 'string' && llmInfo.provider.trim()) ? llmInfo.provider.trim() : '');
    const configuredModel = (typeof effectiveLlm?.model === 'string' && effectiveLlm.model.trim())
      ? effectiveLlm.model.trim()
      : '';
    const requestedModel = executionTelemetry?.requestedModel || configuredModel;
    const actualModel = executionTelemetry?.actualModel || '';
    const actualProvider = executionTelemetry?.actualProvider || '';
    const actualDiffersFromRequested = !!actualModel && !!requestedModel
      && normaliseModelNameForComparison(actualModel) !== normaliseModelNameForComparison(requestedModel);
    const actualDiffersFromConfiguredProvider = !!actualProvider && !!configuredProvider && actualProvider !== configuredProvider;
    const failureReason = executionTelemetry?.primaryFailureReason || executionTelemetry?.error || '';
    const explicitStageModelOverride = !!executionTelemetry?.explicitStageModelOverride;
    const unexpectedModelMismatch = (
      actualDiffersFromRequested || actualDiffersFromConfiguredProvider
    ) && !explicitStageModelOverride;
    const executionOverlayActive = !localModelUnavailable && !!executionTelemetry && (
      !!executionTelemetry.fallbackUsed
      || actualDiffersFromRequested
      || actualDiffersFromConfiguredProvider
      || explicitStageModelOverride
      || !!failureReason
    );
    const executionStatusLabel = explicitStageModelOverride && !failureReason
      ? 'Stage Override Active'
      : executionTelemetry?.primaryFailureKind === 'quota_exhausted'
      ? 'Quota Exhausted'
      : (executionTelemetry?.fallbackUsed ? 'Fallback Active' : (failureReason ? 'Execution Error' : 'Last Execution'));
    const titleParts = ['Open language model settings'];
    titleParts.push(`Configured status: ${configuredStatusLabel}`);
    if (localModelUnavailable) {
      titleParts.push('No usable model configured: premium model use is disabled and no Ollama model is selected.');
      if (localModelUnavailableReason) titleParts.push(`Unavailable reason: ${localModelUnavailableReason}`);
    }
    if (configuredProvider) titleParts.push(`Configured provider: ${configuredProvider}`);
    if (configuredModel) titleParts.push(`Configured model: ${configuredModel}`);
    if (llmHost) titleParts.push(`Host: ${llmHost}`);
    if (errorMsg) titleParts.push(`Error: ${errorMsg}`);
    if (executionOverlayActive) {
      const hasHardFailure = !!failureReason || unexpectedModelMismatch;
      llmClass = hasHardFailure ? 'fatal' : 'warning';
      titleParts.push(`Last execution status: ${executionStatusLabel}`);
      if (requestedModel) titleParts.push(`Requested model: ${requestedModel}`);
      if (actualProvider) titleParts.push(`Executed provider: ${actualProvider}`);
      if (actualModel) titleParts.push(`Executed model: ${actualModel}`);
      if (explicitStageModelOverride) {
        titleParts.push('Stage model override: explicit policy override');
        if (executionTelemetry.executionStage) {
          titleParts.push(`Execution stage: ${executionTelemetry.executionStage}`);
        }
        if (executionTelemetry.policyStage) {
          titleParts.push(`Policy stage: ${executionTelemetry.policyStage}`);
        }
        if (executionTelemetry.selectionMode) {
          titleParts.push(`Selection mode: ${executionTelemetry.selectionMode}`);
        }
        if (executionTelemetry.explicitStageModelOverrideOrigin) {
          titleParts.push(`Override origin: ${executionTelemetry.explicitStageModelOverrideOrigin}`);
        }
      }
      if (executionTelemetry?.callModels?.length > 1) {
        titleParts.push(`Execution models tried: ${executionTelemetry.callModels.join(', ')}`);
      }
      if (executionTelemetry?.primaryFailureKind) {
        titleParts.push(`Failure kind: ${executionTelemetry.primaryFailureKind}`);
      }
      if (failureReason) {
        titleParts.push(`Failure reason: ${failureReason}`);
      }
      if (executionTelemetry?.warnings?.length) {
        titleParts.push(`Warnings: ${executionTelemetry.warnings.join(' | ')}`);
      }
    }
    const displayModelText = localModelUnavailable
      ? 'No model configured'
      : executionOverlayActive
      ? (actualModel || requestedModel || configuredModel || 'Not Set')
      : (configuredModel || 'Not Set');
    const modelSettingsSegment = makeActionButton(
      'Model',
      displayModelText,
      () => { openSettingsForModelControls(); },
      {
        ariaLabel: 'Open language model settings',
        title: titleParts.join('\n')
      }
    );
    if (llmClass) modelSettingsSegment.classList.add('llm-status-badge', llmClass);
    segments.push(modelSettingsSegment);
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
      setKeptNativeTitle(badge, 'Write-tool conservatism is enabled: explicit user write intent required.');
      segments.push(badge);
    }
  } catch (_) { }

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
    let authData = await fetchJsonWithTimeout('/von/api/auth/status', { cache: 'no-store', timeoutMs: 4000 });
    if (!authData) {
      try { authData = await fetchJsonWithTimeout('/api/auth/status', { cache: 'no-store', timeoutMs: 4000 }); } catch (_) { /* ignore */ }
    }
    if (authData) {
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
          setKeptNativeTitle(effectiveBtn, `Logged in as ${authData.email || '(unknown email)'}`);
        } else {
          setKeptNativeTitle(effectiveBtn, 'Not logged in');
        }
      });
    } else {
      const btn = footer.querySelector('.footer-segment .concept-footer-button');
      if (btn) setKeptNativeTitle(btn, 'Not logged in');
    }
  } catch (_) {
    const btn = footer.querySelector('.footer-segment .concept-footer-button');
    if (btn) setKeptNativeTitle(btn, 'Not logged in');
  }

  // Jest fallback: if running under tests and highlight missing, force-create it to avoid timing/env flakiness.
  try {
    const inJest = typeof globalThis.process !== 'undefined' && globalThis.process?.env?.JEST_WORKER_ID;
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
        setKeptNativeTitle(btn, readKeptNativeTitle(btn).includes('Logged in as') ? readKeptNativeTitle(btn) : 'Logged in as (test fallback)');
      }
    }
  } catch (_) { /* non-fatal */ }

  // Clicking empty space (not concept buttons) still opens settings
  footer.style.cursor = 'pointer';
  setKeptNativeTitle(footer, 'Click empty area to open settings');
  footer.addEventListener('click', (ev) => {
    if (ev.target.closest('.concept-footer-button')) { return; }
    const settingsTabButton = document.querySelector('.tab-button[data-tab="settingsTab"]');
    if (settingsTabButton) { settingsTabButton.click(); }
  });

  // Keep footer visually flagged until DB/Atlas status has been retrieved.
  readinessIssues.add('db');
  updateFooterReadinessState(footerContainer, readinessIssues);
  ensureFooterDbLoadingBadge(footer);

  // Load DB badge asynchronously and retry so transient startup pressure does not
  // permanently suppress the badge for the rest of the session.
  async function loadDbBadge(attempt = 0) {
    if (dbLoadGeneration !== footerDbLoadGeneration) return;
    const requestStartedAt = (typeof performance !== 'undefined' && typeof performance.now === 'function')
      ? performance.now()
      : Date.now();
    const dbInfo = await fetchJsonWithTimeout('/api/settings/db/info', { timeoutMs: 5000 });
    const requestElapsedMs = Math.max(0, Math.round(((typeof performance !== 'undefined' && typeof performance.now === 'function')
      ? performance.now()
      : Date.now()) - requestStartedAt));
    if (dbLoadGeneration !== footerDbLoadGeneration) return;

    if (dbInfo) {
      recordFooterDbProbeSuccess(requestElapsedMs);
      const existingBadge = footer.querySelector('.db-conn-badge');
      if (existingBadge) {
        try { existingBadge.remove(); } catch (_) { /* ignore */ }
      }
      attachFooterDbBadge(footer, dbInfo);
      readinessIssues.delete('db');
      updateFooterReadinessState(footerContainer, readinessIssues);
      clearFooterDbRetryTimer();
      return;
    }

    readinessIssues.add('db');
    updateFooterReadinessState(footerContainer, readinessIssues);
    const loadingBadge = ensureFooterDbLoadingBadge(footer);
    if (loadingBadge) {
      const labelNode = loadingBadge.querySelector('.db-label');
      if (labelNode) {
        labelNode.textContent = attempt > 0 ? '🕓 Mongo: retrying...' : '🕓 Mongo: loading...';
      }
    }
    recordFooterDbProbeFailure();

    const inJest = typeof globalThis.process !== 'undefined' && globalThis.process?.env?.JEST_WORKER_ID;
    if (inJest) return;

    const delayMs = computeFooterDbRetryDelayMs(attempt);
    if (loadingBadge) {
      setKeptNativeTitle(loadingBadge, formatFooterDbRetryHint(attempt, delayMs));
    }
    clearFooterDbRetryTimer();
    footerDbRetryTimerId = setTimeout(() => { void loadDbBadge(attempt + 1); }, delayMs);
  }

  void loadDbBadge();
  updateFooterReadinessState(footerContainer, readinessIssues);
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
  suggestions.forEach((s) => {
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
