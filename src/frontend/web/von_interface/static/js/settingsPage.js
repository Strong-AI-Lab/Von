import { getWindowSessionId, postJson, WINDOW_SESSION_HEADER } from './apiService.js';
import {
  clearBackgroundTaskHistory,
  formatBackgroundTaskSummary,
  getBackgroundTaskState,
  subscribeBackgroundTaskUpdates
} from './backgroundTaskTracker.js';
import {
  normaliseOrganisationDisplayName,
  renderOrgSelector,
  setupOrgSwitchListener,
  switchOrganisation
} from './components/orgSelector.js';
import { populateLanguageSelect } from './languageConfig.js';
import {
  loadAndRenderOllamaHosts,
  loadOllamaHosts,
  populateModelDropdown,
  populateOpenAIModelDropdown,
  renderOpenAIModelOptions,
  populateOrganisationsDropdown,
  populatePeopleDropdown,
  saveOllamaHosts,
  showStatusMessage,
  verifyOllamaHost
} from './settings.js';
import {
  getSpeechSynthesisVoices,
  isSpeechRecognitionSupported,
  isTextToSpeechSupported,
  speakText
} from './speech.js';
import {
  CHAT_HISTORY_RECENT_LIMIT_STORAGE_KEY,
  CHAT_HISTORY_RECENT_WINDOW_DAYS_STORAGE_KEY,
  clampConversationHistoryRecentLimit,
  clampConversationHistoryRecentWindowDays,
  loadConversationHistorySettings
} from './utils/conversationHistoryPreferences.js';
import {
  getSessionScopedOrgContext,
  getSessionScopedNamespace,
  setSessionScopedNamespace,
} from './utils/sessionScopedStorage.js';
import {
  fetchUserPreferences,
  hydrateStoredSelectionsFromUserPreferences,
} from './utils/userPreferenceBootstrap.js';
import {
  clearStoredOllamaSelection,
  getLocalPremiumModelUseEnabled,
  getStoredOllamaSelection,
  getStoredOpenAiSelectedModel,
  hasLocalPremiumModelUsePreference,
  setLocalPremiumModelUseEnabled,
  setStoredOllamaSelection,
  setStoredOpenAiSelectedModel,
} from './utils/localModelPreferences.js';

// Helper to build fetch headers with window session context (JVNAUTOSCI-1011)
function buildSettingsFetchHeaders(extraHeaders = {}) {
  return {
    [WINDOW_SESSION_HEADER]: getWindowSessionId(),
    ...extraHeaders
  };
}

// LocalStorage keys for client-side persistence (no DB storage)
const LS_USER_KEY = 'von_current_user';
const LS_ORG_KEY = 'von_current_org';
const LS_LANG_KEY = 'von_preferred_language';
const LS_AUTO_RELOAD = 'von:autoReloadOnRestart';
const LS_GMAIL_PROFILE = 'von_gmail_profile';
const LS_TTS_VOICE_URI = 'chatTtsVoiceUri';
const LS_TTS_LANGUAGE = 'chatTtsLanguage';
const LS_TTS_RATE = 'chatTtsRate';
const LS_TTS_PITCH = 'chatTtsPitch';
const LS_TTS_VOLUME = 'chatTtsVolume';
const LS_TTS_MAX_SPEAKING_SECONDS = 'chatTtsMaxSpeakingSeconds';
const LS_TTS_PREFERRED_SPEAKING_SECONDS = 'chatTtsPreferredSpeakingSeconds';
const LS_STT_LANGUAGE = 'chatSttLanguage';
const LS_STT_CONTINUOUS = 'chatSttContinuous';
const LS_STT_INTERIM_RESULTS = 'chatSttInterimResults';
const LS_SHOW_CODE_NAMES = 'von_show_code_names';
const LS_FILTER_NL_NAMES_TO_PREFERRED_LANGUAGE = 'von_filter_nl_names_to_preferred_language';
const LS_CARTOUCHE_SHORTEST_NAME = 'von_cartouche_use_shortest_name';
const LS_CARTOUCHE_SHOW_NAME = 'von_cartouche_show_name';
const LS_CARTOUCHE_SHOW_ID = 'von_cartouche_show_id';
const LS_CARTOUCHE_SHOW_KIND = 'von_cartouche_show_kind';
const LS_CARTOUCHE_KIND_AS_BG = 'von_cartouche_kind_as_background';
const RUNTIME_REFRESH_MS = 12000;

let runtimeIntervalId = null;
let runtimeAbortController = null;
let runtimeStatusInFlight = false;
let backgroundTaskUnsubscribe = null;
let gmailProfileStatusInFlight = false;
let currentResolvedLlm = null;
let latestOpenAiModelProbe = null;

let __vonIsAdminOrOwner = false;
let __canPersistWriteConservatism = false;
let availableGmailProfiles = [];

function _focusCurrentUserSettingsSection() {
  try {
    const section = document.getElementById('current-user-settings');
    const authContainer = document.getElementById('authenticationStatus');
    const target = section || authContainer;
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    setTimeout(() => {
      try {
        const loginButton = document.querySelector('#authenticationStatus button');
        if (loginButton && typeof loginButton.focus === 'function') {
          loginButton.focus();
        }
      } catch { }
    }, 250);
  } catch { }
}

function _focusModelSettingsSection() {
  try {
    const section = document.getElementById('premium-model-settings') || document.getElementById('ollima-settings');
    if (section) {
      section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    setTimeout(() => {
      try {
        const focusTarget =
          document.getElementById('globalModelSelect')
          || document.getElementById('openaiModelSelect')
          || section?.querySelector('select, input, button');
        if (focusTarget && typeof focusTarget.focus === 'function') {
          focusTarget.focus();
        }
      } catch { }
    }, 250);
  } catch { }
}

// Allow parent (main app) to request focus on the login area from elsewhere (e.g. chat tabs placeholder).
try {
  window.addEventListener('message', (event) => {
    try {
      if (event.origin !== window.location.origin) return;
      const type = event?.data?.type;
      if (type === 'von:focus-current-user-settings') {
        _focusCurrentUserSettingsSection();
      } else if (type === 'von:focus-model-settings') {
        _focusModelSettingsSection();
      }
    } catch { }
  });
} catch { }

function _setWriteConservatismOverrideBadgeEnabled(enabled) {
  try {
    const badge = document.getElementById('disableWriteToolConservatismOnBadge');
    if (badge) {
      badge.classList.toggle('hidden', !enabled);
    }
  } catch { }

  // Keep the global footer in sync if this UI is inside an iframe.
  try {
    if (window.parent?.updateModelInfoFooterDisplay) {
      void window.parent.updateModelInfoFooterDisplay();
    }
  } catch { }
}

function resolveActiveLlmScopeContext() {
  const storedUser = getStoredJson(LS_USER_KEY);
  if (storedUser?.concept_id) {
    return { scope: 'user', conceptId: storedUser.concept_id };
  }

  const storedOrg = getStoredJson(LS_ORG_KEY);
  if (storedOrg?.concept_id) {
    return { scope: 'organisation', conceptId: storedOrg.concept_id };
  }

  return { scope: null, conceptId: null };
}

function readOptionText(option) {
  const text = String(option?.textContent || '').trim();
  return text || null;
}

function readOptionIdentity(option) {
  if (!option) return { id: null, conceptId: null };

  let id = option.dataset?.id || null;
  let conceptId = option.dataset?.conceptId || null;
  const rawValue = String(option.value || '').trim();

  if (!id && !conceptId && rawValue) {
    if (rawValue.startsWith('{')) {
      try {
        const parsed = JSON.parse(rawValue);
        id = parsed?.id || null;
        conceptId = parsed?.concept_id || null;
      } catch {
        // Ignore malformed legacy values and fall back below.
      }
    } else if (rawValue.startsWith('#V#')) {
      conceptId = rawValue;
    }
  }

  return { id, conceptId };
}

function buildStoredUserContextFromOption(option) {
  const { id, conceptId } = readOptionIdentity(option);
  if (!id && !conceptId) return null;
  return {
    id,
    concept_id: conceptId,
    name: readOptionText(option),
  };
}

function buildStoredOrganisationContextFromOption(option) {
  const { id, conceptId } = readOptionIdentity(option);
  if (!id && !conceptId) return null;
  return {
    id,
    concept_id: conceptId,
    name: normaliseOrganisationDisplayName(option.textContent) || readOptionText(option),
  };
}

function getSelectedUserContextFromUi() {
  const userSelect = document.getElementById('currentUserSelect');
  return buildStoredUserContextFromOption(userSelect?.selectedOptions?.[0]);
}

function getOrganisationSelectFromUi() {
  return (
    document.getElementById('currentOrganisationSelect')
    || document.getElementById('orgSelect')
  );
}

function getSelectedOrganisationContextFromUi() {
  const orgSelect = getOrganisationSelectFromUi();
  return buildStoredOrganisationContextFromOption(orgSelect?.selectedOptions?.[0]);
}

function resolveDisplayedProviderModels(settings) {
  const effectiveLlm = settings?.resolved_llm || settings?.active_llm || null;
  const enabledLlms = Array.isArray(settings?.enabled_llms) ? settings.enabled_llms : [];
  let currentOllamaModel = null;
  let currentOpenAIModel = null;

  for (const entry of enabledLlms) {
    if (!entry || typeof entry !== 'object') continue;
    if (entry.provider === 'ollama' && entry.model && !currentOllamaModel) {
      currentOllamaModel = entry.model;
    } else if (entry.provider === 'openai' && entry.model && !currentOpenAIModel) {
      currentOpenAIModel = entry.model;
    }
  }

  if (effectiveLlm?.provider === 'ollama' && effectiveLlm.model) {
    currentOllamaModel = effectiveLlm.model;
  } else if (effectiveLlm?.provider === 'openai' && effectiveLlm.model) {
    currentOpenAIModel = effectiveLlm.model;
  }

  return {
    effectiveLlm,
    currentOllamaModel,
    currentOpenAIModel,
  };
}

async function syncInitialScopedSelections({
  setUserConcept = async (userConceptId) =>
    postJson('/von/api/session/set_user_concept', { user_concept_id: userConceptId }),
  switchOrganisationFn = switchOrganisation,
  refreshRagStatus = () => loadRagStatus(null),
} = {}) {
  const userData = getSelectedUserContextFromUi() || getStoredJson(LS_USER_KEY);
  setStoredJson(LS_USER_KEY, userData);

  let fallbackNamespace = '';
  if (userData?.concept_id) {
    try {
      const resp = await setUserConcept(userData.concept_id);
      fallbackNamespace = resp?.namespace || '';
      setSessionScopedNamespace(fallbackNamespace);
    } catch (e) {
      console.warn('Failed to sync server session user concept (initial load)', e);
    }
  } else {
    setSessionScopedNamespace(null);
  }

  const orgSelect = getOrganisationSelectFromUi();
  const orgData = getSelectedOrganisationContextFromUi()
    || (!orgSelect ? (getStoredJson(LS_ORG_KEY) || getSessionScopedOrgContext()) : null);
  setStoredJson(LS_ORG_KEY, orgData);

  try {
    const resp = await switchOrganisationFn(orgData?.concept_id || null, orgData?.name || null);
    if (resp && Object.prototype.hasOwnProperty.call(resp, 'namespace')) {
      setSessionScopedNamespace(resp.namespace || null);
    } else {
      setSessionScopedNamespace(fallbackNamespace || null);
    }
  } catch (e) {
    console.warn('Failed to sync organisation via backend (initial load)', e);
    setSessionScopedNamespace(fallbackNamespace || null);
  }

  renderActiveNamespace();
  void refreshRagStatus();
}

function resolveOllamaSelection(includeFallback = false) {
  const ollamaModelSelect = document.getElementById('globalModelSelect');
  if (!ollamaModelSelect) return null;

  let ollamaOption = ollamaModelSelect.selectedOptions?.[0] || null;
  let ollamaModel = ollamaOption
    ? (ollamaOption.dataset.modelName || ollamaModelSelect.value)
    : ollamaModelSelect.value;

  if (!ollamaModel && includeFallback) {
    ollamaOption = Array.from(ollamaModelSelect.options || []).find((option) =>
      String(option?.dataset?.modelName || option?.value || '').trim(),
    ) || null;
    ollamaModel = ollamaOption
      ? (ollamaOption.dataset.modelName || ollamaOption.value)
      : '';

    if (ollamaOption && ollamaOption.value) {
      ollamaModelSelect.selectedIndex = Array.from(ollamaModelSelect.options).indexOf(ollamaOption);
      ollamaModelSelect.value = ollamaOption.value;
    }
  }

  if (!ollamaModel) return null;

  const selection = {
    provider: 'ollama',
    model: ollamaModel,
    value: ollamaOption?.value || ollamaModelSelect.value || null,
  };
  const hostUrl = ollamaOption?.dataset?.hostUrl || null;
  if (hostUrl) selection.host = hostUrl;
  return selection;
}

function buildPersistedLlmSelections() {
  const selections = [];
  const openaiModelSelect = document.getElementById('openaiModelSelect');

  const openaiModel = String(openaiModelSelect?.value || '').trim();
  if (openaiModel) {
    setStoredOpenAiSelectedModel(openaiModel);
    selections.push({ provider: 'openai', model: openaiModel });
  }

  return selections;
}

function setInlineStatusMessage(element, text, tone = null) {
  if (!element) return;
  element.textContent = String(text || '').trim();
  element.className = 'status-message';
  if (tone === 'success' || tone === 'error') {
    element.classList.add(tone);
  }
  element.style.display = element.textContent ? 'block' : 'none';
}

function updateOpenAiModelStatusMessage() {
  const statusEl = document.getElementById('openaiModelStatusMessage');
  const toggleEl = document.getElementById('enableOpenAiPremiumToggle');
  const selectEl = document.getElementById('openaiModelSelect');
  if (!statusEl) return;

  const premiumEnabled = !!toggleEl?.checked;
  const selectedModel = String(selectEl?.value || getStoredOpenAiSelectedModel() || '').trim();

  if (!selectedModel) {
    setInlineStatusMessage(
      statusEl,
      premiumEnabled
        ? 'Select a premium model, then test it before relying on it.'
        : 'Premium use is disabled on this machine. Ollama remains the active provider here.',
      null,
    );
    return;
  }

  if (latestOpenAiModelProbe && latestOpenAiModelProbe.model === selectedModel) {
    if (latestOpenAiModelProbe.usable) {
      const prefix = premiumEnabled
        ? `${selectedModel} is usable.`
        : `${selectedModel} tested successfully, but premium use is disabled.`;
      setInlineStatusMessage(
        statusEl,
        latestOpenAiModelProbe.reason ? `${prefix} ${latestOpenAiModelProbe.reason}` : prefix,
        premiumEnabled ? 'success' : null,
      );
      return;
    }
    const failurePrefix = premiumEnabled
      ? `${selectedModel} is not usable.`
      : `Premium use is disabled on this machine. ${selectedModel} also failed its last test.`;
    setInlineStatusMessage(
      statusEl,
      latestOpenAiModelProbe.reason
        ? `${failurePrefix} ${latestOpenAiModelProbe.reason}`
        : failurePrefix,
      'error',
    );
    return;
  }

  setInlineStatusMessage(
    statusEl,
    premiumEnabled
      ? `Selected premium model has not been tested yet.`
      : `Premium use is disabled on this machine. You can still test ${selectedModel} before enabling it.`,
    null,
  );
}

function notifyLocalModelPreferenceChanged() {
  if (window.parent?.updateModelInfoFooterDisplay) {
    window.parent.updateModelInfoFooterDisplay();
  }
  if (window.parent) {
    window.parent.document.dispatchEvent(new CustomEvent('von:settingsChanged'));
  }
}

async function testSelectedOpenAiModel() {
  const apiKeyEnvVar = document.getElementById('openaiApiKeyEnvVar')?.value;
  const selectedModel = String(document.getElementById('openaiModelSelect')?.value || '').trim();
  const statusEl = document.getElementById('openaiModelStatusMessage');

  if (!selectedModel) {
    latestOpenAiModelProbe = null;
    updateOpenAiModelStatusMessage();
    return { usable: false, model: null, reason: 'No premium model selected.' };
  }

  setStoredOpenAiSelectedModel(selectedModel);
  setInlineStatusMessage(statusEl, `Testing ${selectedModel}...`, null);

  try {
    const response = await postJson('/api/settings/openai/test_model', {
      api_key_env_var: apiKeyEnvVar,
      model: selectedModel,
    });
    latestOpenAiModelProbe = {
      usable: !!response?.usable,
      model: String(response?.model || selectedModel),
      reason: String(response?.reason || '').trim(),
      failure_kind: String(response?.failure_kind || '').trim() || null,
    };
  } catch (error) {
    const message = String(error?.message || '').trim();
    const restartHint = (
      /404/.test(message)
      || /unexpected token </i.test(message)
      || /failed to fetch/i.test(message)
    )
      ? 'The server needs a restart before the premium model test endpoint is available.'
      : null;
    latestOpenAiModelProbe = {
      usable: false,
      model: selectedModel,
      reason: restartHint || message || 'Premium model test failed.',
      failure_kind: restartHint ? 'server_restart_required' : 'request_failed',
    };
  }

  updateOpenAiModelStatusMessage();
  return latestOpenAiModelProbe;
}

function resolveActiveLlmFromSelections(
  enabledLlms,
  preferredProvider = null,
  currentResolved = currentResolvedLlm,
) {
  if (!Array.isArray(enabledLlms) || !enabledLlms.length) return null;

  if (preferredProvider) {
    const preferred = enabledLlms.find((entry) => entry?.provider === preferredProvider);
    if (preferred) return { ...preferred };
  }

  const matchesCurrent = enabledLlms.find((entry) =>
    currentResolved
    && entry?.provider === currentResolved.provider
    && entry?.model === currentResolved.model
    && (entry?.host || null) === (currentResolved.host || null)
  );
  if (matchesCurrent) return { ...matchesCurrent };

  return { ...enabledLlms[0] };
}

async function _fetchSessionContextForRole() {
  try {
    const resp = await fetch('/von/api/session/context', {
      cache: 'no-cache',
      headers: buildSettingsFetchHeaders()
    });
    if (!resp.ok) return null;
    return await resp.json();
  } catch {
    return null;
  }
}

function formatUptime(ms) {
  const totalSec = Math.floor(ms / 1000);
  const d = Math.floor(totalSec / 86400);
  const h = Math.floor((totalSec % 86400) / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function normaliseBackgroundTaskStatus(rawStatus) {
  const status = String(rawStatus || '').trim().toLowerCase();
  if (status === 'error' || status === 'failed' || status === 'failure') return 'error';
  if (status === 'cancelled' || status === 'canceled') return 'cancelled';
  return 'success';
}

function formatBackgroundTaskTimestamp(isoValue) {
  const value = String(isoValue || '').trim();
  if (!value) return 'unknown time';
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return value;
  return new Date(parsed).toLocaleString();
}

function formatBackgroundTaskDuration(durationMs) {
  const ms = Number(durationMs);
  if (!Number.isFinite(ms) || ms < 0) return 'duration unknown';
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  return `${Math.round(ms)}ms`;
}

export function __testOnly_formatBackgroundTaskActivity(snapshot) {
  const active = Array.isArray(snapshot?.active) ? snapshot.active.filter(Boolean) : [];
  const history = Array.isArray(snapshot?.history) ? snapshot.history.filter(Boolean) : [];
  const activeText = active.length
    ? `${formatBackgroundTaskSummary(active, { maxLabels: 2 })} (${active.length} running)`
    : 'No background tasks running.';
  const historyRows = history.slice(0, 20).map((entry) => {
    const status = normaliseBackgroundTaskStatus(entry.status);
    const label = String(entry.label || entry.taskType || 'Background task').trim();
    const detail = String(entry.detail || '').trim();
    const duration = formatBackgroundTaskDuration(entry.durationMs);
    const completedAt = formatBackgroundTaskTimestamp(entry.finishedAtIso);
    const parts = [`${label}`, `status=${status}`, `duration=${duration}`, `finished=${completedAt}`];
    if (detail) parts.push(`detail=${detail}`);
    if (status === 'error' && entry.errorMessage) parts.push(`error=${String(entry.errorMessage).trim()}`);
    return parts.join(' | ');
  });
  return { activeText, historyRows };
}

function renderBackgroundTaskActivity(snapshot = getBackgroundTaskState({ historyLimit: 20 })) {
  const statusEl = document.getElementById('settingsBackgroundTaskStatus');
  const historyEl = document.getElementById('settingsBackgroundTaskHistory');
  if (!statusEl && !historyEl) return;

  const active = Array.isArray(snapshot?.active) ? snapshot.active.filter(Boolean) : [];
  const history = Array.isArray(snapshot?.history) ? snapshot.history.filter(Boolean) : [];
  const { activeText } = __testOnly_formatBackgroundTaskActivity({ active, history });

  if (statusEl) {
    statusEl.textContent = activeText;
    statusEl.classList.toggle('is-active', active.length > 0);
  }

  if (!historyEl) return;
  historyEl.textContent = '';
  if (!history.length) {
    const empty = document.createElement('p');
    empty.className = 'background-task-history-empty';
    empty.textContent = 'No background tasks recorded yet.';
    historyEl.appendChild(empty);
    return;
  }

  const list = document.createElement('ul');
  list.className = 'background-task-history-list';
  history.slice(0, 20).forEach((entry) => {
    const status = normaliseBackgroundTaskStatus(entry.status);
    const item = document.createElement('li');
    item.className = `background-task-history-item status-${status}`;

    const title = document.createElement('div');
    title.className = 'background-task-history-title';
    const label = String(entry.label || entry.taskType || 'Background task').trim();
    const detail = String(entry.detail || '').trim();
    title.textContent = detail ? `${label}: ${detail}` : label;
    item.appendChild(title);

    const meta = document.createElement('div');
    meta.className = 'background-task-history-meta';
    const finishedAt = formatBackgroundTaskTimestamp(entry.finishedAtIso);
    const duration = formatBackgroundTaskDuration(entry.durationMs);
    const metaParts = [`Completed ${finishedAt}`, `status ${status}`, duration];
    if (status === 'error' && entry.errorMessage) {
      metaParts.push(`error ${String(entry.errorMessage).trim()}`);
    }
    meta.textContent = metaParts.join(' | ');
    item.appendChild(meta);

    list.appendChild(item);
  });
  historyEl.appendChild(list);
}

function setupBackgroundTaskSection() {
  const statusEl = document.getElementById('settingsBackgroundTaskStatus');
  const historyEl = document.getElementById('settingsBackgroundTaskHistory');
  const clearBtn = document.getElementById('settingsClearBackgroundTaskHistoryButton');

  if (!statusEl && !historyEl && !clearBtn) return;

  try {
    if (backgroundTaskUnsubscribe) {
      backgroundTaskUnsubscribe();
      backgroundTaskUnsubscribe = null;
    }
  } catch (_) { }

  try {
    backgroundTaskUnsubscribe = subscribeBackgroundTaskUpdates((snapshot) => {
      renderBackgroundTaskActivity(snapshot);
    });
  } catch (_) {
    renderBackgroundTaskActivity();
  }

  if (clearBtn && !clearBtn.dataset.backgroundTaskBound) {
    clearBtn.dataset.backgroundTaskBound = '1';
    clearBtn.addEventListener('click', () => {
      clearBackgroundTaskHistory();
      renderBackgroundTaskActivity(getBackgroundTaskState({ historyLimit: 20 }));
      showStatusMessage('vontologyPerformanceStatus', 'Background task history cleared.', false);
    });
  }
}

function wireCopyButton(btn) {
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const value = btn.textContent.trim();
    if (!value || value === '—') return;
    try {
      await navigator.clipboard.writeText(value);
      const oldText = btn.textContent;
      btn.textContent = 'Copied';
      btn.classList.add('copied');
      setTimeout(() => {
        btn.textContent = oldText;
        btn.classList.remove('copied');
      }, 1000);
    } catch (err) {
      console.warn('Copy failed', err);
    }
  });
}

function safeLocalStorageGet(key) {
  runtimeStatusInFlight = true;
  try {
    if (typeof localStorage === 'undefined') return null;
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeLocalStorageSet(key, value) {
  try {
    if (typeof localStorage === 'undefined') return;
    localStorage.setItem(key, value);
  } catch {
    // Ignore.
  }
}

function getStoredGmailProfile() {
  try {
    return String(localStorage.getItem(LS_GMAIL_PROFILE) || '').trim();
  } catch {
    return '';
  }
}

function setStoredGmailProfile(profileId) {
  try {
    const trimmed = String(profileId || '').trim();
    if (trimmed) {
      localStorage.setItem(LS_GMAIL_PROFILE, trimmed);
    } else {
      localStorage.removeItem(LS_GMAIL_PROFILE);
    }
  } catch {
    // Ignore localStorage errors.
  }
}

function normaliseGmailProfileList(value) {
  if (Array.isArray(value)) {
    return value.map((item) => String(item ?? '').trim()).filter(Boolean);
  }
  if (value && typeof value === 'object') {
    return Object.keys(value).map((item) => String(item ?? '').trim()).filter(Boolean);
  }
  return [];
}

function getProfilesFromSelect() {
  const select = document.getElementById('gmailProfileSelect');
  if (!select) return [];
  return [...select.options]
    .map((opt) => String(opt.value || '').trim())
    .filter((value) => value);
}

function renderGmailProfileOptions(profiles, defaultProfile) {
  const select = document.getElementById('gmailProfileSelect');
  if (!select) return;

  const sortedProfiles = [...profiles];
  sortedProfiles.sort((a, b) => a.localeCompare(b));
  availableGmailProfiles = sortedProfiles;

  const storedProfile = getStoredGmailProfile();
  const defaultCandidate = String(defaultProfile || '').trim();
  const selectedProfile =
    (storedProfile && sortedProfiles.includes(storedProfile) && storedProfile) ||
    (defaultCandidate && sortedProfiles.includes(defaultCandidate) && defaultCandidate) ||
    '';

  select.replaceChildren();
  const noneOption = document.createElement('option');
  noneOption.value = '';
  noneOption.textContent = 'None (disable Gmail calls)';
  select.append(noneOption);

  for (const profileId of sortedProfiles) {
    const option = document.createElement('option');
    option.value = profileId;
    option.textContent = profileId;
    select.append(option);
  }

  select.value = selectedProfile;
  setStoredGmailProfile(selectedProfile);
}

async function refreshGmailProfileStatusList() {
  const container = document.getElementById('gmailProfilesStatus');
  if (!container) return;
  if (gmailProfileStatusInFlight) return;
  gmailProfileStatusInFlight = true;

  try {
    const profiles = availableGmailProfiles.length
      ? availableGmailProfiles
      : getProfilesFromSelect();

    if (!profiles.length) {
      container.textContent = 'Gmail profiles: none configured';
      return;
    }

    container.textContent = 'Gmail profiles: loading...';

    const results = await Promise.all(
      profiles.map(async (profileId) => {
        try {
          const response = await fetch(
            `/von/api/agent/gmail/oauth/status?profile_id=${encodeURIComponent(profileId)}`,
            { cache: 'no-cache' }
          );
          const data = await response.json();
          if (!response.ok) {
            return { profileId, status: `error (${data?.error || response.status})` };
          }
          if (data?.has_tokens) {
            const email = data.authorised_email || '(unknown email)';
            const expiry = data.expires_at ? ` (expires ${data.expires_at})` : '';
            return { profileId, status: `authorised as ${email}${expiry}` };
          }
          return { profileId, status: 'not authorised' };
        } catch (_) {
          return { profileId, status: 'error (failed to fetch)' };
        }
      })
    );

    container.replaceChildren();
    for (const result of results) {
      const row = document.createElement('div');
      row.textContent = `${result.profileId}: ${result.status}`;
      container.append(row);
    }
  } finally {
    gmailProfileStatusInFlight = false;
    if (typeof updateGmailOauthLastRefreshed === 'function') {
      updateGmailOauthLastRefreshed();
    }
  }
}

window.refreshGmailProfileStatusList = refreshGmailProfileStatusList;

function updateGmailOauthLastRefreshed() {
  const el = document.getElementById('agentGmailOauthLastRefreshed');
  if (!el) return;
  try {
    const stamp = new Date().toLocaleString('en-NZ');
    el.textContent = `Last refreshed: ${stamp}`;
  } catch {
    el.textContent = 'Last refreshed: just now';
  }
}

window.updateGmailOauthLastRefreshed = updateGmailOauthLastRefreshed;

function updateGmailProfileStatus() {
  void refreshGmailProfileStatusList();
}

window.updateGmailProfileStatus = updateGmailProfileStatus;

function clampNumber(value, minValue, maxValue, fallbackValue) {
  const num = Number(value);
  if (!Number.isFinite(num)) {
    return fallbackValue;
  }
  return Math.min(Math.max(num, minValue), maxValue);
}

function normaliseLanguageSetting(value) {
  return String(value ?? '').trim();
}

function parseBoolSetting(value, fallbackValue) {
  if (value === null || value === undefined) {
    return fallbackValue;
  }
  return String(value) === 'true';
}

function notifyPreferenceChanged(key, value) {
  try {
    window.dispatchEvent(new CustomEvent('von-preferences-changed', {
      detail: { key: String(key || ''), value }
    }));
  } catch (_) {
    // ignore
  }

  try {
    window.parent?.dispatchEvent?.(new CustomEvent('von-preferences-changed', {
      detail: { key: String(key || ''), value }
    }));
  } catch (_) {
    // ignore
  }

  try {
    window.parent?.document?.dispatchEvent?.(new CustomEvent('von-preferences-changed', {
      detail: { key: String(key || ''), value }
    }));
  } catch (_) {
    // ignore
  }
}

function setShowCodeNamesSetting(value) {
  safeLocalStorageSet(LS_SHOW_CODE_NAMES, value ? 'true' : 'false');
  notifyPreferenceChanged(LS_SHOW_CODE_NAMES, !!value);
}

function getShowCodeNamesSetting() {
  return parseBoolSetting(safeLocalStorageGet(LS_SHOW_CODE_NAMES), true);
}

function setCartoucheShowNameSetting(value) {
  safeLocalStorageSet(LS_CARTOUCHE_SHOW_NAME, value ? 'true' : 'false');
  notifyPreferenceChanged(LS_CARTOUCHE_SHOW_NAME, !!value);
}

function getCartoucheShowNameSetting() {
  return parseBoolSetting(safeLocalStorageGet(LS_CARTOUCHE_SHOW_NAME), true);
}

function setCartoucheShortestNameSetting(value) {
  safeLocalStorageSet(LS_CARTOUCHE_SHORTEST_NAME, value ? 'true' : 'false');
  notifyPreferenceChanged(LS_CARTOUCHE_SHORTEST_NAME, !!value);
}

function getCartoucheShortestNameSetting() {
  return parseBoolSetting(safeLocalStorageGet(LS_CARTOUCHE_SHORTEST_NAME), false);
}

function setCartoucheShowIdSetting(value) {
  safeLocalStorageSet(LS_CARTOUCHE_SHOW_ID, value ? 'true' : 'false');
  notifyPreferenceChanged(LS_CARTOUCHE_SHOW_ID, !!value);
}

function getCartoucheShowIdSetting() {
  return parseBoolSetting(safeLocalStorageGet(LS_CARTOUCHE_SHOW_ID), false);
}

function setCartoucheShowKindSetting(value) {
  safeLocalStorageSet(LS_CARTOUCHE_SHOW_KIND, value ? 'true' : 'false');
  notifyPreferenceChanged(LS_CARTOUCHE_SHOW_KIND, !!value);
}

function getCartoucheShowKindSetting() {
  return parseBoolSetting(safeLocalStorageGet(LS_CARTOUCHE_SHOW_KIND), true);
}

function setCartoucheKindAsBackgroundSetting(value) {
  safeLocalStorageSet(LS_CARTOUCHE_KIND_AS_BG, value ? 'true' : 'false');
  notifyPreferenceChanged(LS_CARTOUCHE_KIND_AS_BG, !!value);
}

function getCartoucheKindAsBackgroundSetting() {
  return parseBoolSetting(safeLocalStorageGet(LS_CARTOUCHE_KIND_AS_BG), false);
}

function enforceCartoucheAppearanceConstraints(toggles = {}) {
  const { showNameToggle, showIdToggle, showKindToggle, kindBgToggle } = toggles;
  const showName = showNameToggle ? !!showNameToggle.checked : getCartoucheShowNameSetting();
  const showId = showIdToggle ? !!showIdToggle.checked : getCartoucheShowIdSetting();
  const showKind = showKindToggle ? !!showKindToggle.checked : getCartoucheShowKindSetting();
  const kindAsBg = kindBgToggle ? !!kindBgToggle.checked : getCartoucheKindAsBackgroundSetting();

  if (!showName && !showId) {
    if (showIdToggle) {
      showIdToggle.checked = true;
    }
    setCartoucheShowIdSetting(true);
  }

  if (kindAsBg && showKind) {
    if (showKindToggle) {
      showKindToggle.checked = false;
    }
    setCartoucheShowKindSetting(false);
  }
}

function setFilterNlNamesToPreferredLanguageSetting(value) {
  safeLocalStorageSet(LS_FILTER_NL_NAMES_TO_PREFERRED_LANGUAGE, value ? 'true' : 'false');
  notifyPreferenceChanged(LS_FILTER_NL_NAMES_TO_PREFERRED_LANGUAGE, !!value);
}

function getFilterNlNamesToPreferredLanguageSetting() {
  return parseBoolSetting(safeLocalStorageGet(LS_FILTER_NL_NAMES_TO_PREFERRED_LANGUAGE), false);
}

function getPreferredLanguage() {
  const lang = String(safeLocalStorageGet(LS_LANG_KEY) || '').trim();
  return lang || 'en-NZ';
}

function getSpeechSettingsFromStorage() {
  const preferredLanguage = getPreferredLanguage();
  const ttsVoiceUri = String(safeLocalStorageGet(LS_TTS_VOICE_URI) || '').trim();
  const ttsLanguage = normaliseLanguageSetting(safeLocalStorageGet(LS_TTS_LANGUAGE)) || preferredLanguage;
  const ttsRate = clampNumber(safeLocalStorageGet(LS_TTS_RATE), 0.5, 2, 1);
  const ttsPitch = clampNumber(safeLocalStorageGet(LS_TTS_PITCH), 0, 2, 1);
  const ttsVolume = clampNumber(safeLocalStorageGet(LS_TTS_VOLUME), 0, 1, 1);
  const ttsMaxSeconds = clampNumber(safeLocalStorageGet(LS_TTS_MAX_SPEAKING_SECONDS), 10, 600, 40);
  const ttsPreferredSecondsRaw = clampNumber(safeLocalStorageGet(LS_TTS_PREFERRED_SPEAKING_SECONDS), 5, 600, 20);
  const ttsPreferredSeconds = Math.min(ttsPreferredSecondsRaw, ttsMaxSeconds);

  const sttLanguage = normaliseLanguageSetting(safeLocalStorageGet(LS_STT_LANGUAGE)) || preferredLanguage;
  const sttContinuous = parseBoolSetting(safeLocalStorageGet(LS_STT_CONTINUOUS), true);
  const sttInterimResults = parseBoolSetting(safeLocalStorageGet(LS_STT_INTERIM_RESULTS), true);

  return {
    tts: {
      voiceUri: ttsVoiceUri || null,
      language: ttsLanguage,
      rate: ttsRate,
      pitch: ttsPitch,
      volume: ttsVolume,
      maxSpeakingSeconds: ttsMaxSeconds,
      preferredSpeakingSeconds: ttsPreferredSeconds
    },
    stt: {
      language: sttLanguage,
      continuous: sttContinuous,
      interimResults: sttInterimResults
    }
  };
}

function setupConversationHistorySettingsSection() {
  const recentLimitInput = document.getElementById('settingsConversationRecentLimitInput');
  const recentWindowDaysInput = document.getElementById('settingsConversationRecentWindowDaysInput');
  if (!recentLimitInput && !recentWindowDaysInput) {
    return;
  }

  const refreshUiFromSettings = () => {
    const settings = loadConversationHistorySettings((key) => safeLocalStorageGet(key));
    if (recentLimitInput) {
      recentLimitInput.value = String(settings.recentLimit);
    }
    if (recentWindowDaysInput) {
      recentWindowDaysInput.value = String(settings.recentWindowDays);
    }
  };

  refreshUiFromSettings();

  if (recentLimitInput) {
    recentLimitInput.addEventListener('change', (event) => {
      const nextValue = clampConversationHistoryRecentLimit(event?.target?.value);
      safeLocalStorageSet(CHAT_HISTORY_RECENT_LIMIT_STORAGE_KEY, String(nextValue));
      recentLimitInput.value = String(nextValue);
      notifyPreferenceChanged(CHAT_HISTORY_RECENT_LIMIT_STORAGE_KEY, nextValue);
    });
  }

  if (recentWindowDaysInput) {
    recentWindowDaysInput.addEventListener('change', (event) => {
      const nextValue = clampConversationHistoryRecentWindowDays(event?.target?.value);
      safeLocalStorageSet(CHAT_HISTORY_RECENT_WINDOW_DAYS_STORAGE_KEY, String(nextValue));
      recentWindowDaysInput.value = String(nextValue);
      notifyPreferenceChanged(CHAT_HISTORY_RECENT_WINDOW_DAYS_STORAGE_KEY, nextValue);
    });
  }
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
  if (!selectEl) return;

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

function setupSpeechSettingsSection() {
  const ttsVoiceSelect = document.getElementById('settingsTtsVoiceSelect');
  const ttsLanguageInput = document.getElementById('settingsTtsLanguageInput');
  const ttsRateRange = document.getElementById('settingsTtsRateRange');
  const ttsPitchRange = document.getElementById('settingsTtsPitchRange');
  const ttsVolumeRange = document.getElementById('settingsTtsVolumeRange');
  const ttsMaxSecondsInput = document.getElementById('settingsTtsMaxSecondsInput');
  const ttsPreferredSecondsInput = document.getElementById('settingsTtsPreferredSecondsInput');
  const ttsRateValue = document.getElementById('settingsTtsRateValue');
  const ttsPitchValue = document.getElementById('settingsTtsPitchValue');
  const ttsVolumeValue = document.getElementById('settingsTtsVolumeValue');
  const ttsPreviewButton = document.getElementById('settingsTtsPreviewButton');

  const sttLanguageInput = document.getElementById('settingsSttLanguageInput');
  const sttContinuousToggle = document.getElementById('settingsSttContinuousToggle');
  const sttInterimToggle = document.getElementById('settingsSttInterimToggle');
  const supportNote = document.getElementById('settingsSpeechSupportNote');

  const anyUiExists = !!(
    ttsVoiceSelect || ttsLanguageInput || ttsRateRange || ttsPitchRange || ttsVolumeRange ||
    sttLanguageInput || sttContinuousToggle || sttInterimToggle
  );
  if (!anyUiExists) {
    return;
  }

  const refreshUiFromSettings = () => {
    const settings = getSpeechSettingsFromStorage();

    if (ttsLanguageInput) {
      ttsLanguageInput.value = settings.tts.language || '';
    }
    if (ttsRateRange) {
      ttsRateRange.value = String(settings.tts.rate);
    }
    if (ttsPitchRange) {
      ttsPitchRange.value = String(settings.tts.pitch);
    }
    if (ttsVolumeRange) {
      ttsVolumeRange.value = String(settings.tts.volume);
    }
    if (ttsMaxSecondsInput) {
      const existingRaw = safeLocalStorageGet(LS_TTS_MAX_SPEAKING_SECONDS);
      const currentValue = settings.tts.maxSpeakingSeconds ?? 40;
      ttsMaxSecondsInput.value = String(currentValue);
      if (existingRaw === null || existingRaw === undefined || String(existingRaw).trim() === '') {
        safeLocalStorageSet(LS_TTS_MAX_SPEAKING_SECONDS, String(currentValue));
      }
    }

    if (ttsPreferredSecondsInput) {
      const existingRaw = safeLocalStorageGet(LS_TTS_PREFERRED_SPEAKING_SECONDS);
      const currentValue = settings.tts.preferredSpeakingSeconds ?? 20;
      ttsPreferredSecondsInput.value = String(currentValue);
      if (existingRaw === null || existingRaw === undefined || String(existingRaw).trim() === '') {
        safeLocalStorageSet(LS_TTS_PREFERRED_SPEAKING_SECONDS, String(currentValue));
      }
    }
    if (ttsRateValue) {
      ttsRateValue.textContent = String(settings.tts.rate.toFixed(1));
    }
    if (ttsPitchValue) {
      ttsPitchValue.textContent = String(settings.tts.pitch.toFixed(1));
    }
    if (ttsVolumeValue) {
      ttsVolumeValue.textContent = String(settings.tts.volume.toFixed(2));
    }
    if (ttsVoiceSelect) {
      populateTtsVoiceSelect(ttsVoiceSelect, settings.tts.voiceUri);
    }

    if (sttLanguageInput) {
      sttLanguageInput.value = settings.stt.language || '';
    }
    if (sttContinuousToggle) {
      sttContinuousToggle.checked = !!settings.stt.continuous;
    }
    if (sttInterimToggle) {
      sttInterimToggle.checked = !!settings.stt.interimResults;
    }
  };

  refreshUiFromSettings();

  const ttsSupported = isTextToSpeechSupported();
  const sttSupported = isSpeechRecognitionSupported();

  if (supportNote) {
    if (ttsSupported && sttSupported) {
      supportNote.textContent = 'Text-to-speech and dictation are available in this browser.';
    } else if (ttsSupported) {
      supportNote.textContent = 'Dictation is not supported in this browser.';
    } else if (sttSupported) {
      supportNote.textContent = 'Text-to-speech is not supported in this browser.';
    } else {
      supportNote.textContent = 'Speech is not supported in this browser.';
    }
  }

  if (!ttsSupported) {
    if (ttsVoiceSelect) ttsVoiceSelect.disabled = true;
    if (ttsLanguageInput) ttsLanguageInput.disabled = true;
    if (ttsRateRange) ttsRateRange.disabled = true;
    if (ttsPitchRange) ttsPitchRange.disabled = true;
    if (ttsVolumeRange) ttsVolumeRange.disabled = true;
    if (ttsMaxSecondsInput) ttsMaxSecondsInput.disabled = true;
    if (ttsPreferredSecondsInput) ttsPreferredSecondsInput.disabled = true;
    if (ttsPreviewButton) {
      ttsPreviewButton.disabled = true;
      ttsPreviewButton.title = 'Text-to-speech is not supported in this browser.';
    }
  }

  const notifySpeechSettingsChanged = (changedKey, value) => {
    try {
      const detail = { key: String(changedKey || ''), value };
      window.dispatchEvent(new CustomEvent('von-preferences-changed', { detail }));
    } catch { }

    // Also notify parent (Settings is typically loaded in an iframe).
    try {
      window.parent?.dispatchEvent?.(new CustomEvent('von-preferences-changed', {
        detail: { key: String(changedKey || ''), value }
      }));
    } catch { }
  };

  if (!sttSupported) {
    if (sttLanguageInput) sttLanguageInput.disabled = true;
    if (sttContinuousToggle) sttContinuousToggle.disabled = true;
    if (sttInterimToggle) sttInterimToggle.disabled = true;
  }

  if (ttsVoiceSelect) {
    ttsVoiceSelect.addEventListener('change', (e) => {
      safeLocalStorageSet(LS_TTS_VOICE_URI, String(e.target.value || ''));
      notifySpeechSettingsChanged(LS_TTS_VOICE_URI, String(e.target.value || ''));
    });

    // Voices can load asynchronously.
    try {
      const root = typeof globalThis !== 'undefined' ? globalThis : null;
      if (root && root.speechSynthesis) {
        const previousHandler = root.speechSynthesis.onvoiceschanged;
        root.speechSynthesis.onvoiceschanged = () => {
          try { if (typeof previousHandler === 'function') previousHandler(); } catch { }
          refreshUiFromSettings();
        };
      }
    } catch {
      // Ignore.
    }
  }

  if (ttsLanguageInput) {
    ttsLanguageInput.addEventListener('change', (e) => {
      const value = normaliseLanguageSetting(e.target.value);
      safeLocalStorageSet(LS_TTS_LANGUAGE, value);
      notifySpeechSettingsChanged(LS_TTS_LANGUAGE, value);
    });
  }
  if (ttsRateRange) {
    ttsRateRange.addEventListener('input', (e) => {
      const value = clampNumber(e.target.value, 0.5, 2, 1);
      safeLocalStorageSet(LS_TTS_RATE, String(value));
      if (ttsRateValue) ttsRateValue.textContent = String(value.toFixed(1));
      notifySpeechSettingsChanged(LS_TTS_RATE, value);
    });
  }
  if (ttsPitchRange) {
    ttsPitchRange.addEventListener('input', (e) => {
      const value = clampNumber(e.target.value, 0, 2, 1);
      safeLocalStorageSet(LS_TTS_PITCH, String(value));
      if (ttsPitchValue) ttsPitchValue.textContent = String(value.toFixed(1));
      notifySpeechSettingsChanged(LS_TTS_PITCH, value);
    });
  }
  if (ttsVolumeRange) {
    ttsVolumeRange.addEventListener('input', (e) => {
      const value = clampNumber(e.target.value, 0, 1, 1);
      safeLocalStorageSet(LS_TTS_VOLUME, String(value));
      if (ttsVolumeValue) ttsVolumeValue.textContent = String(value.toFixed(2));
      notifySpeechSettingsChanged(LS_TTS_VOLUME, value);
    });
  }

  if (ttsMaxSecondsInput) {
    ttsMaxSecondsInput.addEventListener('change', (e) => {
      const value = clampNumber(e.target.value, 10, 600, 40);
      safeLocalStorageSet(LS_TTS_MAX_SPEAKING_SECONDS, String(value));
      ttsMaxSecondsInput.value = String(value);

      // Ensure preferred stays within max.
      if (ttsPreferredSecondsInput) {
        const preferred = clampNumber(ttsPreferredSecondsInput.value, 5, 600, 20);
        const nextPreferred = Math.min(preferred, value);
        safeLocalStorageSet(LS_TTS_PREFERRED_SPEAKING_SECONDS, String(nextPreferred));
        ttsPreferredSecondsInput.value = String(nextPreferred);
        notifySpeechSettingsChanged(LS_TTS_PREFERRED_SPEAKING_SECONDS, nextPreferred);
      }

      notifySpeechSettingsChanged(LS_TTS_MAX_SPEAKING_SECONDS, value);
    });
  }

  if (ttsPreferredSecondsInput) {
    ttsPreferredSecondsInput.addEventListener('change', (e) => {
      const maxSeconds = clampNumber(safeLocalStorageGet(LS_TTS_MAX_SPEAKING_SECONDS), 10, 600, 40);
      const value = clampNumber(e.target.value, 5, 600, 20);
      const nextValue = Math.min(value, maxSeconds);

      safeLocalStorageSet(LS_TTS_PREFERRED_SPEAKING_SECONDS, String(nextValue));
      ttsPreferredSecondsInput.value = String(nextValue);
      notifySpeechSettingsChanged(LS_TTS_PREFERRED_SPEAKING_SECONDS, nextValue);
    });
  }

  if (ttsPreviewButton) {
    ttsPreviewButton.addEventListener('click', () => {
      if (!ttsSupported) return;
      const settings = getSpeechSettingsFromStorage();
      try {
        speakText('Kia ora. This is Von speaking.', {
          language: settings.tts.language,
          rate: settings.tts.rate,
          pitch: settings.tts.pitch,
          volume: settings.tts.volume,
          voiceUri: settings.tts.voiceUri
        });
      } catch (err) {
        console.warn('[settingsPage] TTS preview failed:', err);
      }
    });
  }

  if (sttLanguageInput) {
    sttLanguageInput.addEventListener('change', (e) => {
      safeLocalStorageSet(LS_STT_LANGUAGE, normaliseLanguageSetting(e.target.value));
    });
  }
  if (sttContinuousToggle) {
    sttContinuousToggle.addEventListener('change', (e) => {
      safeLocalStorageSet(LS_STT_CONTINUOUS, e.target.checked ? 'true' : 'false');
    });
  }
  if (sttInterimToggle) {
    sttInterimToggle.addEventListener('change', (e) => {
      safeLocalStorageSet(LS_STT_INTERIM_RESULTS, e.target.checked ? 'true' : 'false');
    });
  }
}

function setupConceptUiSettings() {
  const showCodeToggle = document.getElementById('settingsShowCodeNamesToggle');
  if (showCodeToggle) {
    showCodeToggle.checked = getShowCodeNamesSetting();
    showCodeToggle.addEventListener('change', () => {
      setShowCodeNamesSetting(!!showCodeToggle.checked);
    });
  }

  const nlFilterToggle = document.getElementById('settingsFilterNlNamesToPreferredLanguageToggle');
  if (nlFilterToggle) {
    nlFilterToggle.checked = getFilterNlNamesToPreferredLanguageSetting();
    nlFilterToggle.addEventListener('change', () => {
      setFilterNlNamesToPreferredLanguageSetting(!!nlFilterToggle.checked);
    });
  }

  const langLabel = document.getElementById('settingsPreferredLanguageForNlFilter');
  if (langLabel) {
    langLabel.textContent = getPreferredLanguage();
  }
  setupCartoucheAppearanceSettings();
}

function setupCartoucheAppearanceSettings() {
  const showNameToggle = document.getElementById('cartoucheShowNameToggle');
  const shortestToggle = document.getElementById('cartoucheShortestNameToggle');
  const showIdToggle = document.getElementById('cartoucheShowIdToggle');
  const showKindToggle = document.getElementById('cartoucheShowKindToggle');
  const kindBgToggle = document.getElementById('cartoucheKindAsBackgroundToggle');

  if (showNameToggle) {
    showNameToggle.checked = getCartoucheShowNameSetting();
    showNameToggle.addEventListener('change', () => {
      setCartoucheShowNameSetting(!!showNameToggle.checked);
      enforceCartoucheAppearanceConstraints({ showNameToggle, showIdToggle, showKindToggle, kindBgToggle });
    });
  }

  if (shortestToggle) {
    shortestToggle.checked = getCartoucheShortestNameSetting();
    shortestToggle.addEventListener('change', () => {
      setCartoucheShortestNameSetting(!!shortestToggle.checked);
    });
  }

  if (showIdToggle) {
    showIdToggle.checked = getCartoucheShowIdSetting();
    showIdToggle.addEventListener('change', () => {
      setCartoucheShowIdSetting(!!showIdToggle.checked);
      enforceCartoucheAppearanceConstraints({ showNameToggle, showIdToggle, showKindToggle, kindBgToggle });
    });
  }

  if (showKindToggle) {
    showKindToggle.checked = getCartoucheShowKindSetting();
    showKindToggle.addEventListener('change', () => {
      setCartoucheShowKindSetting(!!showKindToggle.checked);
      enforceCartoucheAppearanceConstraints({ showNameToggle, showIdToggle, showKindToggle, kindBgToggle });
    });
  }

  if (kindBgToggle) {
    kindBgToggle.checked = getCartoucheKindAsBackgroundSetting();
    kindBgToggle.addEventListener('change', () => {
      setCartoucheKindAsBackgroundSetting(!!kindBgToggle.checked);
      enforceCartoucheAppearanceConstraints({ showNameToggle, showIdToggle, showKindToggle, kindBgToggle });
    });
  }

  enforceCartoucheAppearanceConstraints({ showNameToggle, showIdToggle, showKindToggle, kindBgToggle });
}

function renderRagSummary(ragData, pendingFallback) {
  const ragSummaryEl = document.getElementById('settingsRagSummary');
  const ragHintEl = document.getElementById('settingsRagHint');
  if (!ragSummaryEl) return;

  const { summaryText, hintText, titleText } = formatRagSummaryForSettings(ragData, pendingFallback);
  ragSummaryEl.textContent = summaryText;
  ragSummaryEl.title = titleText;
  if (ragHintEl) ragHintEl.textContent = hintText;
}

function formatRagSummaryForSettings(ragData, pendingFallback) {
  const pending = typeof pendingFallback === 'number' ? pendingFallback : null;
  if (!ragData) {
    if (pending === null || pending < 0) {
      return { summaryText: 'Unknown', hintText: 'RAG status unavailable', titleText: 'RAG status unavailable' };
    }
    if (pending === 0) {
      return { summaryText: 'Idle', hintText: 'No pending items to index', titleText: 'No pending items to index' };
    }
    return {
      summaryText: `${pending} pending`,
      hintText: 'Pending items waiting for indexing',
      titleText: `${pending} items waiting for indexing`
    };
  }

  const indexed = (typeof ragData.indexed === 'number') ? ragData.indexed : 0;
  const pendingCount = (typeof ragData.pending === 'number') ? ragData.pending : 0;
  const failed = (typeof ragData.failed === 'number') ? ragData.failed : 0;
  const skipped = (typeof ragData.skipped === 'number') ? ragData.skipped : 0;

  const sessNs = ragData.session_namespace || null;
  const chSessions = (typeof ragData.chat_history_sessions === 'number') ? ragData.chat_history_sessions : 0;
  const chMessages = (typeof ragData.chat_history_messages === 'number') ? ragData.chat_history_messages : 0;
  const chOk = (typeof ragData.chat_history_rag_success === 'number') ? ragData.chat_history_rag_success : 0;
  const chFail = (typeof ragData.chat_history_rag_failed === 'number') ? ragData.chat_history_rag_failed : 0;
  const hasChatCounts = Boolean(chSessions || chMessages || chOk || chFail);

  const hintParts = [
    `KA indexed=${indexed}`,
    `pending=${pendingCount}`,
    `failed=${failed}`,
    `skipped=${skipped}`
  ];
  if (hasChatCounts) {
    hintParts.push(`Conversations indexed=${chOk}`);
    hintParts.push(`Conversations failed=${chFail}`);
  }

  const titleParts = [];
  titleParts.push(...hintParts);
  if (sessNs) {
    titleParts.push(`session_ns=${sessNs}`);
  }
  if (hasChatCounts) {
    titleParts.push(`Conversation sessions=${chSessions}`);
    titleParts.push(`messages=${chMessages}`);
  }
  const titleText = titleParts.join(' • ');

  if (pendingCount === 0) {
    if (hasChatCounts) {
      if (chFail > 0) {
        return {
          summaryText: `KA ${indexed} • Conversations ${chOk}/${chFail} failed`,
          hintText: hintParts.join(' • '),
          titleText
        };
      }
      return {
        summaryText: `KA ${indexed} • Conversations ${chOk}`,
        hintText: hintParts.join(' • '),
        titleText
      };
    }
    return {
      summaryText: `KA ${indexed}`,
      hintText: hintParts.join(' • '),
      titleText
    };
  }

  if (chFail > 0) {
    return {
      summaryText: `KA ${indexed} • ${pendingCount} pending • Conversations ${chFail} failed`,
      hintText: hintParts.join(' • '),
      titleText
    };
  }
  return {
    summaryText: `KA ${indexed} • ${pendingCount} pending`,
    hintText: hintParts.join(' • '),
    titleText
  };
}

function getPreferredRagNamespace() {
  return getSessionScopedNamespace();
}

function renderActiveNamespace() {
  const nsEl = document.getElementById('settingsActiveNamespaceValue');
  const hintEl = document.getElementById('settingsActiveNamespaceHint');
  if (!nsEl) return;

  const ns = getPreferredRagNamespace();
  nsEl.textContent = ns || '—';
  if (hintEl) {
    hintEl.textContent = ns
      ? 'Used to scope RAG, conversation history, and knowledge acquisition sessions.'
      : 'No active namespace is set yet.';
  }
}

async function loadRagStatus(pendingFallback) {
  const ns = getPreferredRagNamespace();
  const url = ns ? `/admin/rag_status?namespace=${encodeURIComponent(ns)}` : '/admin/rag_status';
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    const res = await fetch(url, { cache: 'no-store', signal: controller.signal });
    clearTimeout(timeout);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    renderRagSummary(data, pendingFallback);
  } catch (err) {
    console.warn('Failed to load RAG status (settings)', err);
    renderRagSummary(null, pendingFallback);
  }
}

// Export for testing
export function __testOnly_formatRagSummaryForSettings(ragData, pendingFallback) {
  return formatRagSummaryForSettings(ragData, pendingFallback);
}

// Export for testing
export function __testOnly_getPreferredRagNamespace() {
  return getPreferredRagNamespace();
}

// Export for testing
export function __testOnly_resolveDisplayedProviderModels(settings) {
  return resolveDisplayedProviderModels(settings);
}

// Export for testing
export function __testOnly_resolveActiveLlmFromSelections(
  enabledLlms,
  preferredProvider = null,
  currentResolved = null,
) {
  return resolveActiveLlmFromSelections(
    enabledLlms,
    preferredProvider,
    currentResolved,
  );
}

// Export for testing
export function __testOnly_buildStoredUserContextFromOption(option) {
  return buildStoredUserContextFromOption(option);
}

// Export for testing
export function __testOnly_applyStoredSelection(selectId, stored, fallbackSelected = true) {
  return applyStoredSelection(selectId, stored, fallbackSelected);
}

// Export for testing
export async function __testOnly_syncInitialScopedSelections(overrides = {}) {
  await syncInitialScopedSelections(overrides);
}

function isSettingsRuntimePanelVisible() {
  try {
    const frameEl = window.frameElement;
    if (!frameEl) return true;
    const parentView = window.parent;
    const style = parentView?.getComputedStyle ? parentView.getComputedStyle(frameEl) : null;
    if (style && (style.display === 'none' || style.visibility === 'hidden')) {
      return false;
    }
    if (typeof frameEl.getClientRects === 'function' && frameEl.getClientRects().length === 0) {
      return false;
    }
    return true;
  } catch (_) {
    // Fail open if the host environment does not expose parent/frame visibility details.
    return true;
  }
}

async function loadRuntimeStatus(manualRefresh = false) {
  const localEl = document.getElementById('settingsLocalIpValue');
  const publicEl = document.getElementById('settingsPublicIpValue');
  const pidEl = document.getElementById('settingsPidValue');
  const uptimeEl = document.getElementById('settingsUptimeValue');
  const refreshBtn = document.getElementById('refreshRuntimeButton');

  if (!manualRefresh && !isSettingsRuntimePanelVisible()) {
    return;
  }

  if (runtimeStatusInFlight && !manualRefresh) {
    return;
  }

  if (refreshBtn && manualRefresh) {
    refreshBtn.disabled = true;
    refreshBtn.textContent = 'Refreshing…';
  }

  runtimeStatusInFlight = true
  try {
    try { runtimeAbortController?.abort(); } catch { }
    const controller = new AbortController();
    runtimeAbortController = controller;
    const timeout = setTimeout(() => controller.abort(), 8000);
    const res = await fetch('/health', { cache: 'no-store', signal: controller.signal });
    clearTimeout(timeout);
    if (!res.ok) throw new Error('HTTP ' + res.status);

    const data = await res.json();
    const { local_ip: localIp = '—', public_ip: publicIp = '—', pid = '—', start_time: startTimeIso = null, rag_pending_count: ragPending = null } = data;

    if (localEl) localEl.textContent = localIp || '—';
    if (publicEl) publicEl.textContent = publicIp || '—';
    if (pidEl) pidEl.textContent = pid ?? '—';
    if (uptimeEl && startTimeIso) {
      const started = Date.parse(startTimeIso);
      if (!Number.isNaN(started)) {
        const diff = Date.now() - started;
        uptimeEl.textContent = formatUptime(diff);
      } else {
        uptimeEl.textContent = '—';
      }
    }

    renderActiveNamespace();
    await loadRagStatus(ragPending);
  } catch (err) {
    if (err && err.name === 'AbortError') {
      // Expected when a newer poll supersedes an older one or the page is unloading.
      return;
    }
    console.warn('Failed to load runtime status', err);
    if (localEl) localEl.textContent = '—';
    if (publicEl) publicEl.textContent = '—';
    if (pidEl) pidEl.textContent = '—';
    if (uptimeEl) uptimeEl.textContent = '—';
    renderRagSummary(null, null);
  } finally {
    runtimeStatusInFlight = false;
    if (refreshBtn && manualRefresh) {
      refreshBtn.disabled = false;
      refreshBtn.textContent = 'Refresh';
    }
  }
}

function setupRuntimeSection() {
  wireCopyButton(document.getElementById('settingsLocalIpValue'));
  wireCopyButton(document.getElementById('settingsPublicIpValue'));
  wireCopyButton(document.getElementById('settingsPidValue'));
  wireCopyButton(document.getElementById('settingsActiveNamespaceValue'));

  const refreshBtn = document.getElementById('refreshRuntimeButton');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => loadRuntimeStatus(true));
  }

  loadRuntimeStatus();
  if (!runtimeIntervalId) {
    runtimeIntervalId = setInterval(loadRuntimeStatus, RUNTIME_REFRESH_MS);
  }
}

window.addEventListener('beforeunload', () => {
  try {
    if (runtimeIntervalId) {
      clearInterval(runtimeIntervalId);
      runtimeIntervalId = null;
    }
  } catch { }
  try { runtimeAbortController?.abort(); } catch { }
  try {
    if (backgroundTaskUnsubscribe) {
      backgroundTaskUnsubscribe();
      backgroundTaskUnsubscribe = null;
    }
  } catch { }
});

function getStoredJson(key) {
  // JVNAUTOSCI-1011: sessionStorage (per-window) first, localStorage fallback
  try {
    const sessionVal = sessionStorage.getItem(key);
    if (sessionVal) return JSON.parse(sessionVal);
    return JSON.parse(localStorage.getItem(key) || 'null');
  } catch { return null; }
}
function setStoredJson(key, value) {
  // JVNAUTOSCI-1011: Write to both sessionStorage (window-scoped) and localStorage (persistent)
  try {
    if (value == null) {
      sessionStorage.removeItem(key);
      localStorage.removeItem(key);
    } else {
      const json = JSON.stringify(value);
      sessionStorage.setItem(key, json);
      localStorage.setItem(key, json);
    }
  } catch { }
}

function normaliseOrgConceptId(value) {
  const raw = String(value || '').trim();
  if (!raw) return null;
  return raw.startsWith('#V#') ? raw.slice(3) : raw;
}

function resolveOrgNameFromSelect(orgConceptId) {
  const target = normaliseOrgConceptId(orgConceptId);
  if (!target) return null;
  const select = getOrganisationSelectFromUi();
  if (!select) return null;
  for (const opt of select.options) {
    const optCid = normaliseOrgConceptId(opt.dataset?.conceptId);
    if (optCid && optCid === target) {
      return normaliseOrganisationDisplayName(opt.textContent) || readOptionText(opt);
    }
  }
  return null;
}

function optionMatchesStoredContext(option, stored) {
  if (!option || !stored) return false;
  const { id, conceptId } = readOptionIdentity(option);
  return Boolean(
    (stored.id && id && String(stored.id) === String(id))
    || (stored.concept_id && conceptId && stored.concept_id === conceptId)
  );
}

function ensureStoredSelectionOption(selectId, stored) {
  const sel = document.getElementById(selectId);
  if (!sel || !stored || (!stored.id && !stored.concept_id)) return false;

  for (const opt of sel.options) {
    if (optionMatchesStoredContext(opt, stored)) {
      return true;
    }
  }

  const option = document.createElement('option');
  option.value = stored.concept_id || stored.id || '';
  if (stored.id) {
    option.dataset.id = String(stored.id);
  }
  if (stored.concept_id) {
    option.dataset.conceptId = stored.concept_id;
  }
  option.textContent = stored.name || stored.email || stored.concept_id || String(stored.id);
  sel.appendChild(option);
  return true;
}

function applyStoredSelection(selectId, stored, fallbackSelected = true) {
  const sel = document.getElementById(selectId);
  if (!sel) return null;
  if (stored && (stored.id || stored.concept_id)) {
    ensureStoredSelectionOption(selectId, stored);
    for (const opt of sel.options) {
      if (optionMatchesStoredContext(opt, stored)) {
        opt.selected = true;
        const { conceptId } = readOptionIdentity(opt);
        // Backfill concept_id if missing (required for per-user model saves)
        if (!stored.concept_id && conceptId) {
          stored.concept_id = conceptId;
          setStoredJson(selectId === 'currentUserSelect' ? LS_USER_KEY : LS_ORG_KEY, stored);
        }
        return stored;
      }
    }
  }
  // If nothing selected & we want fallback, persist current selection for future sessions
  if (fallbackSelected) {
    const current = sel.selectedOptions?.[0];
    if (current && (current.dataset?.id || current.dataset?.conceptId)) {
      const storedVal = selectId === 'currentUserSelect'
        ? buildStoredUserContextFromOption(current)
        : buildStoredOrganisationContextFromOption(current);
      setStoredJson(selectId === 'currentUserSelect' ? LS_USER_KEY : LS_ORG_KEY, storedVal);
      return storedVal;
    }
  }
  return null;
}

document.addEventListener('DOMContentLoaded', async () => {
  setupRuntimeSection();
  setupBackgroundTaskSection();
  // Initialize all settings sections
  await loadAndDisplaySettings();
  setupConversationHistorySettingsSection();
  setupSpeechSettingsSection();
  setupConceptUiSettings();
  // Load DB info
  try { await loadAndDisplayDbInfo(); } catch { }
  // Load deprecation metrics
  try { await loadDeprecationMetrics(); } catch { }

  // Set up event listeners for auto-saving
  document.getElementById('globalModelSelect')?.addEventListener('change', async (event) => {
    // Auto-switch to the host when selecting a global model
    const selectedOption = event.target.selectedOptions[0];
    if (selectedOption && selectedOption.dataset.hostUrl) {
      const hostUrl = selectedOption.dataset.hostUrl;

      // Check if we need to switch hosts by getting current active host
      try {
        const hostsData = await loadOllamaHosts();
        const currentActiveHost = hostsData.active_host;

        // Only switch if the selected model's host is different from the current active host
        if (hostUrl !== currentActiveHost) {
          console.log(`Auto-switching from ${currentActiveHost} to ${hostUrl} for model ${selectedOption.dataset.modelName}`);
          await window.setActiveOllamaHost(hostUrl);
        }
      } catch (error) {
        console.error('Failed to auto-switch host:', error);
        showStatusMessage('settingsStatusMessage', 'Failed to switch to model host', true);
      }
    }
    // Save settings after potentially switching host
    const ollamaSelection = resolveOllamaSelection(false);
    if (ollamaSelection) {
      setStoredOllamaSelection(ollamaSelection);
      setLocalPremiumModelUseEnabled(false);
    } else {
      clearStoredOllamaSelection();
    }
    notifyLocalModelPreferenceChanged();
  });
  document.getElementById('openaiModelSelect')?.addEventListener('change', async () => {
    const selectedModel = document.getElementById('openaiModelSelect')?.value || '';
    setStoredOpenAiSelectedModel(selectedModel);
    latestOpenAiModelProbe = null;
    updateOpenAiModelStatusMessage();
    await testSelectedOpenAiModel();
    await saveAllSettings();
    notifyLocalModelPreferenceChanged();
  });
  document.getElementById('enableOpenAiPremiumToggle')?.addEventListener('change', async (event) => {
    const premiumToggle = event?.target;
    if (!premiumToggle?.checked) {
      const ollamaSelection = resolveOllamaSelection(false);
      if (!ollamaSelection) {
        premiumToggle.checked = true;
        updateOpenAiModelStatusMessage();
        showStatusMessage(
          'settingsStatusMessage',
          'Select an Ollama model before disabling premium use on this machine.',
          true,
        );
        return;
      }
      setStoredOllamaSelection(ollamaSelection);
      setLocalPremiumModelUseEnabled(false);
      updateOpenAiModelStatusMessage();
      notifyLocalModelPreferenceChanged();
      return;
    }

    setLocalPremiumModelUseEnabled(true);
    updateOpenAiModelStatusMessage();
    const probe = await testSelectedOpenAiModel();
    if (!probe?.usable) {
      showStatusMessage(
        'settingsStatusMessage',
        'Premium use is enabled for this machine, but the selected model test failed. Check the premium model before relying on it.',
        true,
      );
    }
    await saveAllSettings();
    notifyLocalModelPreferenceChanged();
  });
  document.getElementById('currentUserSelect')?.addEventListener('change', async () => {
    const sel = document.getElementById('currentUserSelect');
    const opt = sel?.selectedOptions?.[0];
    if (opt) {
      setStoredJson(LS_USER_KEY, buildStoredUserContextFromOption(opt));
      if (window.parent?.updateModelInfoFooterDisplay) { window.parent.updateModelInfoFooterDisplay(); }
      // Keep the authenticated server session aligned with the selected user concept.
      if (opt.dataset.conceptId) {
        try {
          const resp = await postJson('/von/api/session/set_user_concept', {
            user_concept_id: opt.dataset.conceptId
          });
          setSessionScopedNamespace(resp?.namespace || null);
          renderActiveNamespace();
          void loadRagStatus(null);
        } catch (e) {
          console.warn('Failed to update server session user concept', e);
        }
      }
      // When user changes, attempt to load stored server-side prefs (language/org)
      if (opt.dataset.conceptId) {
        await loadUserConceptPreferences(opt.dataset.conceptId);
        // Refresh organisation selector so memberships reflect the selected user.
        if (window.refreshOrgSelector) {
          try { await window.refreshOrgSelector(); } catch (e) { console.warn('Org selector refresh failed', e); }
        }
        await loadRecommendationProfileForSelectedUser();
      }
    } else {
      setStoredJson(LS_USER_KEY, null);
      clearRecommendationProfileForm();
      setRecommendationProfileControlsDisabled(true);
      setRecommendationReviewControlsDisabled(true);
      setRecommendationReviewStatus('');
    }
  });
  document.getElementById('currentOrganisationSelect')?.addEventListener('change', async () => {
    const sel = document.getElementById('currentOrganisationSelect');
    const opt = sel?.selectedOptions?.[0];
    const conceptId = opt?.dataset?.conceptId || null;

    // Call backend to update session namespace (mirrors Phase 2 org selector behaviour)
    try {
      await switchOrganisation(conceptId);
      // switchOrganisation dispatches 'orgSwitched' event which updates namespace display
    } catch (e) {
      console.warn('Failed to switch organisation via backend:', e);
    }

    if (opt && (opt.dataset?.id || conceptId)) {
      setStoredJson(LS_ORG_KEY, buildStoredOrganisationContextFromOption(opt));
      if (window.parent?.updateModelInfoFooterDisplay) { window.parent.updateModelInfoFooterDisplay(); }
      // Persist organisation preference (and language if set)
      persistCurrentUserPreferences();
    } else {
      setStoredJson(LS_ORG_KEY, null);
      if (window.parent?.updateModelInfoFooterDisplay) { window.parent.updateModelInfoFooterDisplay(); }
      persistCurrentUserPreferences();
    }
  });
  document.getElementById('preferredLanguageSelect')?.addEventListener('change', () => {
    const val = document.getElementById('preferredLanguageSelect')?.value;
    if (val) localStorage.setItem(LS_LANG_KEY, val); else localStorage.removeItem(LS_LANG_KEY);
    try {
      const langLabel = document.getElementById('settingsPreferredLanguageForNlFilter');
      if (langLabel) {
        langLabel.textContent = getPreferredLanguage();
      }
      window.dispatchEvent(new CustomEvent('von-preferences-changed', {
        detail: { key: LS_LANG_KEY, value: getPreferredLanguage() }
      }));
    } catch (_) {
      // ignore
    }
    // Emit event so footer or other UI can react
    if (window.parent) { window.parent.document.dispatchEvent(new CustomEvent('von:settingsChanged')); }
    // Persist language preference (with current organisation if available)
    persistCurrentUserPreferences();
  });

  const gmailProfileSelect = document.getElementById('gmailProfileSelect');
  if (gmailProfileSelect) {
    const storedProfile = getStoredGmailProfile();
    if (storedProfile) {
      gmailProfileSelect.value = storedProfile;
    }
    updateGmailProfileStatus();
    gmailProfileSelect.addEventListener('change', () => {
      const val = gmailProfileSelect.value.trim();
      setStoredGmailProfile(val);
      updateGmailProfileStatus();
    });
  }

  setRecommendationProfileControlsDisabled(true);
  setRecommendationReviewControlsDisabled(true);
  document.getElementById('saveRecommendationProfileButton')?.addEventListener('click', () => {
    void saveRecommendationProfileForSelectedUser();
  });
  document.getElementById('runRecommendationReviewButton')?.addEventListener('click', () => {
    void generateRecommendationReviewForSelectedUser();
  });
  await loadRecommendationProfileForSelectedUser();
});

// Expose a lightweight hook so inline auth script can refresh org selector post-login
// without reloading the whole settings page.
window.refreshOrgSelector = async function () {
  try {
    await renderOrgSelector('orgSelectorContainer');
  } catch (e) {
    console.warn('refreshOrgSelector failed', e);
  }
};

// Auto Reload on Restart toggle
const autoReloadToggle = document.getElementById('autoReloadOnRestartToggle');
if (autoReloadToggle) {
  try { autoReloadToggle.checked = localStorage.getItem(LS_AUTO_RELOAD) === '1'; } catch { }
  autoReloadToggle.addEventListener('change', () => {
    try {
      if (autoReloadToggle.checked) localStorage.setItem(LS_AUTO_RELOAD, '1');
      else localStorage.removeItem(LS_AUTO_RELOAD);
      // Inform parent for immediate effect if polling already running
      if (window.parent) {
        window.parent.postMessage({ type: 'settings-content-loaded' }, '*');
      }
    } catch { }
  });
}

// Ollama hosts management event listeners
document.getElementById('addOllamaHostButton')?.addEventListener('click', addOllamaHost);
document.getElementById('refreshOllamaHostsButton')?.addEventListener('click', refreshOllamaHostsFromEnvironment);
document.getElementById('loadOllamaModelsButton')?.addEventListener('click', loadOllamaModels);

// Other event listeners
document.getElementById('verifyOpenAiApiKeyButton')?.addEventListener('click', verifyOpenAiApiKey);
document.getElementById('testOpenAiModelButton')?.addEventListener('click', testSelectedOpenAiModel);

// Add event listener for disable remote Ollama scan toggle
document.addEventListener('DOMContentLoaded', () => {
  const disableRemoteOllamaScanToggle = document.getElementById('disableRemoteOllamaScanToggle');
  if (disableRemoteOllamaScanToggle) {
    disableRemoteOllamaScanToggle.addEventListener('change', async () => {
      try {
        await saveAllSettings();
        showStatusMessage('settingsStatusMessage', 'Ollama scan setting saved successfully!', false);
      } catch (e) {
        console.warn('Failed to save Ollama scan toggle', e);
        showStatusMessage('settingsStatusMessage', 'Failed to save Ollama scan setting', true);
      }
    });
  }
});

// Fetch entity counts on initial load toggle (server-persisted)
const countsToggle = document.getElementById('fetchCountsOnLoadToggle');
if (countsToggle) {
  try {
    // Load current server value via existing loadAndDisplaySettings pipeline
    // We will set the checkbox after settings are fetched below.
  } catch { }
  countsToggle.addEventListener('change', async () => {
    try {
      await saveAllSettings();
      showStatusMessage('vontologyPerformanceStatus', 'Saved. Reload the Vontology tab to apply.', false);
      // Inform parent so preload can react on next navigation
      if (window.parent) { window.parent.document.dispatchEvent(new CustomEvent('von:settingsChanged')); }
    } catch (e) {
      console.warn('Failed to save counts toggle', e);
      showStatusMessage('vontologyPerformanceStatus', 'Failed to save setting', true);
    }
  });
}

// Preload Vontology tree toggle (server-persisted)
const preloadVontologyToggle = document.getElementById('preloadVontologyTreeToggle');
if (preloadVontologyToggle) {
  try {
    // Load current server value via existing loadAndDisplaySettings pipeline
  } catch { }
  preloadVontologyToggle.addEventListener('change', async () => {
    try {
      await saveAllSettings();
      showStatusMessage('vontologyPerformanceStatus', 'Saved. Reload the page to apply.', false);
      if (window.parent) { window.parent.document.dispatchEvent(new CustomEvent('von:settingsChanged')); }
    } catch (e) {
      console.warn('Failed to save preload toggle', e);
      showStatusMessage('vontologyPerformanceStatus', 'Failed to save setting', true);
    }
  });
}

// Quick-reply buttonify toggle (server-persisted)
const buttonifyToggle = document.getElementById('buttonifyModelEnabledToggle');
if (buttonifyToggle) {
  try {
    // Load current server value via existing loadAndDisplaySettings pipeline
  } catch { }
  buttonifyToggle.addEventListener('change', async () => {
    try {
      await saveAllSettings();
      showStatusMessage('settingsStatusMessage', 'Saved. Applies to new chat turns.', false);
    } catch (e) {
      console.warn('Failed to save buttonify toggle', e);
      showStatusMessage('settingsStatusMessage', 'Failed to save setting', true);
    }
  });
}

const autoProceedMinimalImpositionToggle = document.getElementById('autoProceedMinimalImpositionToggle');
if (autoProceedMinimalImpositionToggle) {
  try {
    // Load current server value via existing loadAndDisplaySettings pipeline
  } catch { }
  autoProceedMinimalImpositionToggle.addEventListener('change', async () => {
    try {
      await saveAllSettings();
      showStatusMessage('settingsStatusMessage', 'Saved. Applies to new chat turns.', false);
    } catch (e) {
      console.warn('Failed to save auto-proceed toggle', e);
      showStatusMessage('settingsStatusMessage', 'Failed to save setting', true);
    }
  });
}

const buttonifyPromptLink = document.getElementById('buttonifyPromptLink');
if (buttonifyPromptLink) {
  buttonifyPromptLink.addEventListener('click', (event) => {
    try {
      event.preventDefault();
    } catch { }
    const conceptId = buttonifyPromptLink.dataset?.conceptId;
    if (!conceptId) return;
    const detail = { conceptId, createConceptTab: true, modifierKeys: { shiftKey: !!event.shiftKey } };
    try {
      document.dispatchEvent(new CustomEvent('von:selectConceptById', { detail }));
    } catch { }
    try {
      if (window.parent?.document) {
        window.parent.document.dispatchEvent(new CustomEvent('von:selectConceptById', { detail }));
      }
    } catch { }
  });
}

// Salient inheritance recompute controls
document.getElementById('salientDryRunButton')?.addEventListener('click', () => triggerSalientRecompute({ dry_run: true }));
document.getElementById('salientRecomputeButton')?.addEventListener('click', () => triggerSalientRecompute({}));
document.getElementById('salientForceButton')?.addEventListener('click', () => triggerSalientRecompute({ force: true }));

// Server shutdown control
document.getElementById('shutdownServerButton')?.addEventListener('click', shutdownServer);
document.getElementById('refreshDeprecationMetricsButton')?.addEventListener('click', () => loadDeprecationMetrics(true));
// Reset local preferences button
document.getElementById('resetLocalPrefsButton')?.addEventListener('click', () => {
  try {
    localStorage.removeItem(LS_USER_KEY);
    localStorage.removeItem(LS_ORG_KEY);
    localStorage.removeItem(LS_LANG_KEY);
    localStorage.removeItem(LS_GMAIL_PROFILE);
    localStorage.removeItem(LS_SHOW_CODE_NAMES);
    localStorage.removeItem(LS_FILTER_NL_NAMES_TO_PREFERRED_LANGUAGE);
    localStorage.removeItem(LS_CARTOUCHE_SHORTEST_NAME);
    localStorage.removeItem(LS_CARTOUCHE_SHOW_NAME);
    localStorage.removeItem(LS_CARTOUCHE_SHOW_ID);
    localStorage.removeItem(LS_CARTOUCHE_SHOW_KIND);
    localStorage.removeItem(LS_CARTOUCHE_KIND_AS_BG);
    // Reset selects visually
    const userSel = document.getElementById('currentUserSelect'); if (userSel) userSel.selectedIndex = 0;
    const orgSel = document.getElementById('currentOrganisationSelect'); if (orgSel) orgSel.selectedIndex = 0;
    const langSel = document.getElementById('preferredLanguageSelect'); if (langSel) langSel.value = 'en-NZ';
    const gmailProfileSelect = document.getElementById('gmailProfileSelect');
    if (gmailProfileSelect) gmailProfileSelect.value = '';
    setStoredGmailProfile('');
    if (typeof window.updateGmailProfileStatus === 'function') {
      window.updateGmailProfileStatus();
    }
    const showCodeToggle = document.getElementById('settingsShowCodeNamesToggle'); if (showCodeToggle) showCodeToggle.checked = true;
    setShowCodeNamesSetting(true);
    const nlFilterToggle = document.getElementById('settingsFilterNlNamesToPreferredLanguageToggle');
    if (nlFilterToggle) nlFilterToggle.checked = false;
    setFilterNlNamesToPreferredLanguageSetting(false);
    const langLabel = document.getElementById('settingsPreferredLanguageForNlFilter');
    if (langLabel) langLabel.textContent = getPreferredLanguage();
    const shortestToggle = document.getElementById('cartoucheShortestNameToggle');
    if (shortestToggle) shortestToggle.checked = false;
    setCartoucheShortestNameSetting(false);
    const showNameToggle = document.getElementById('cartoucheShowNameToggle');
    if (showNameToggle) showNameToggle.checked = true;
    setCartoucheShowNameSetting(true);
    const showIdToggle = document.getElementById('cartoucheShowIdToggle');
    if (showIdToggle) showIdToggle.checked = false;
    setCartoucheShowIdSetting(false);
    const showKindToggle = document.getElementById('cartoucheShowKindToggle');
    if (showKindToggle) showKindToggle.checked = true;
    setCartoucheShowKindSetting(true);
    const kindBgToggle = document.getElementById('cartoucheKindAsBackgroundToggle');
    if (kindBgToggle) kindBgToggle.checked = false;
    setCartoucheKindAsBackgroundSetting(false);
    if (window.parent?.updateModelInfoFooterDisplay) { window.parent.updateModelInfoFooterDisplay(); }
    showStatusMessage('settingsStatusMessage', 'Local preferences cleared');
  } catch (e) {
    console.warn('Failed to reset local prefs', e);
    showStatusMessage('settingsStatusMessage', 'Failed to reset local preferences', true);
  }
});

async function loadAndDisplaySettings() {
  try {
    // Pass user context to get properly resolved LLM setting (user > org > global precedence)
    const storedUser = getStoredJson(LS_USER_KEY);
    const storedOrg = getStoredJson(LS_ORG_KEY);
    const params = new URLSearchParams();
    if (storedUser?.concept_id) params.set('user_concept_id', storedUser.concept_id);
    if (storedOrg?.concept_id) params.set('organisation_concept_id', storedOrg.concept_id);
    const settingsUrl = '/api/settings/' + (params.toString() ? '?' + params.toString() : '');

    const response = await fetch(settingsUrl, {
      cache: 'no-store',
      headers: buildSettingsFetchHeaders(),
    });
    if (!response.ok) throw new Error(`Failed to fetch settings: ${response.statusText}`);
    const settings = await response.json();

    // Admin-only controls: decide visibility based on session role.
    try {
      const sessionContext = await _fetchSessionContextForRole();
      const role = (sessionContext && sessionContext.role) ? String(sessionContext.role).toLowerCase() : '';
      __vonIsAdminOrOwner = role === 'admin' || role === 'owner';
      __canPersistWriteConservatism = __vonIsAdminOrOwner
        && Object.prototype.hasOwnProperty.call(settings, 'disable_write_tool_conservatism');
      const container = document.getElementById('disableWriteToolConservatismContainer');
      if (container) {
        container.classList.toggle('hidden', !__vonIsAdminOrOwner);
      }
    } catch {
      __vonIsAdminOrOwner = false;
      __canPersistWriteConservatism = false;
    }

    // The selector should reflect the resolved active model for the provider in scope,
    // not a stale enabled_llms entry from an older or broader context.
    const {
      effectiveLlm,
      currentOpenAIModel,
    } = resolveDisplayedProviderModels(settings);
    currentResolvedLlm = effectiveLlm || null;
    const storedOllamaSelection = getStoredOllamaSelection();
    const currentOllamaModel = storedOllamaSelection?.value || null;
    const storedOpenAiModel = getStoredOpenAiSelectedModel();
    const preferredOpenAiModel = currentOpenAIModel || storedOpenAiModel || null;
    const openAiPremiumEnabled = getLocalPremiumModelUseEnabled(
      !hasLocalPremiumModelUsePreference() && !!preferredOpenAiModel,
    );

    // Load and display Ollama hosts first
    await loadAndRenderOllamaHosts();

    // Populate Ollama models and select the saved one
    await populateModelDropdown('globalModelSelect', currentOllamaModel);

    // Try to populate OpenAI models directly (without verification, if API key is already configured)
    if (preferredOpenAiModel) {
      try {
        await populateOpenAIModelDropdown('openaiModelSelect', preferredOpenAiModel);
        const oac = document.getElementById('openaiModelsContainer');
        if (oac) { oac.style.display = 'block'; oac.classList.remove('hidden'); }
      } catch (error) {
        console.log('Could not directly load OpenAI models, will need verification:', error.message);
      }
    }

    // Populate People and select the saved one
    await populatePeopleDropdown('currentUserSelect', settings.current_user_person_id);
    // If we have concept_id too, attempt to match based on data attribute
    try {
      const userSelect = document.getElementById('currentUserSelect');
      const targetCid = settings.current_user_person_concept_id;
      if (userSelect && targetCid) {
        for (const opt of userSelect.options) {
          if (opt.dataset?.conceptId === targetCid) { opt.selected = true; break; }
        }
      }
    } catch { }
    // Override with stored local selection if present
    applyStoredSelection('currentUserSelect', getStoredJson(LS_USER_KEY));

    // Restore missing org/language context from persisted per-user preferences
    // before populating organisation-dependent UI or namespace state.
    try {
      const selectedUser = getSelectedUserContextFromUi();
      if (selectedUser?.concept_id) {
        await hydrateStoredSelectionsFromUserPreferences(selectedUser.concept_id);
      }
    } catch (e) {
      console.warn('Failed to hydrate stored selections from user preferences', e);
    }

    // Populate Organisations and select the saved one
    await populateOrganisationsDropdown('currentOrganisationSelect', settings.current_organisation_id);
    // If we have concept_id too, attempt to match based on data attribute
    try {
      const orgSelect = document.getElementById('currentOrganisationSelect');
      const targetCid = settings.current_organisation_concept_id;
      if (orgSelect && targetCid) {
        for (const opt of orgSelect.options) {
          if (opt.dataset?.conceptId === targetCid) { opt.selected = true; break; }
        }
      }
    } catch { }
    // Override with stored local selection if present
    applyStoredSelection('currentOrganisationSelect', getStoredJson(LS_ORG_KEY));

    // Phase 2: Render organisation selector (Phase 2 UI integration)
    try {
      await renderOrgSelector('orgSelectorContainer');
      // Set up listener for org switches (triggers RAG namespace update)
      setupOrgSwitchListener((orgId, namespace) => {
        console.log(`Organisation switched: ${orgId || 'personal'}, namespace: ${namespace}`);
        try {
          if (namespace) {
            // JVNAUTOSCI-1015: Update both sessionStorage (for immediate reads) and localStorage (for persistence)
            sessionStorage.setItem('current_user_namespace', namespace);
            localStorage.setItem('current_user_namespace', namespace);
          } else {
            sessionStorage.removeItem('current_user_namespace');
            localStorage.removeItem('current_user_namespace');
          }
        } catch { }

        // Keep footer/user-visible org display in sync with Phase 2 org selection.
        try {
          if (orgId) {
            const resolvedName = resolveOrgNameFromSelect(orgId);
            const orgData = { id: null, concept_id: orgId, name: resolvedName };
            // Update both storages so footer reads correct value immediately
            setStoredJson(LS_ORG_KEY, orgData);
            try { window.parent?.sessionStorage?.setItem('von_current_org', JSON.stringify(orgData)); } catch { }
          } else {
            setStoredJson(LS_ORG_KEY, null);
            try { window.parent?.sessionStorage?.removeItem('von_current_org'); } catch { }
          }
        } catch { }
        if (window.parent?.updateModelInfoFooterDisplay) { window.parent.updateModelInfoFooterDisplay(); }

        try {
          if (window.refreshChatSessionTabsForOrgSwitch) {
            window.refreshChatSessionTabsForOrgSwitch();
          } else if (window.parent?.refreshChatSessionTabsForOrgSwitch) {
            window.parent.refreshChatSessionTabsForOrgSwitch();
          }
        } catch { }

        try {
          if (window.parent?.document) {
            const resolvedNameForEvent = orgId ? resolveOrgNameFromSelect(orgId) : null;
            window.parent.document.dispatchEvent(new CustomEvent('orgSwitched', {
              detail: {
                organisation_id: orgId || null,
                organisation_name: resolvedNameForEvent,
                role: null,
                namespace: namespace || null
              }
            }));
          }
        } catch { }

        renderActiveNamespace();
        void loadRagStatus(null);
      });
    } catch (error) {
      console.error('Error initializing organisation selector:', error);
    }

    // Keep storage and server session aligned with the restored dropdown state.
    // Otherwise the page can look correctly selected while saves and namespace reads
    // still operate on stale user-only context.
    await syncInitialScopedSelections();

    // Populate OpenAI settings
    const envVarInput = document.getElementById('openaiApiKeyEnvVar');
    if (envVarInput && settings.openai_api_key_env_var) {
      envVarInput.value = settings.openai_api_key_env_var;
    }
    const openAiPremiumToggle = document.getElementById('enableOpenAiPremiumToggle');
    if (openAiPremiumToggle) {
      openAiPremiumToggle.checked = openAiPremiumEnabled;
    }
    if (preferredOpenAiModel) {
      setStoredOpenAiSelectedModel(preferredOpenAiModel);
    }
    latestOpenAiModelProbe = null;
    updateOpenAiModelStatusMessage();

    // Populate Gmail profile selector
    try {
      const gmailProfiles = normaliseGmailProfileList(settings.gmail_profiles);
      const defaultProfile = String(settings.gmail_default_profile || '').trim();
      renderGmailProfileOptions(gmailProfiles, defaultProfile);
      if (typeof window.updateGmailProfileStatus === 'function') {
        window.updateGmailProfileStatus();
      }
    } catch (error) {
      console.warn('Failed to render Gmail profiles', error);
    }

    // Populate fetch_counts_on_load toggle
    try {
      const countsToggleEl = document.getElementById('fetchCountsOnLoadToggle');
      if (countsToggleEl) {
        const flag = Object.prototype.hasOwnProperty.call(settings, 'fetch_counts_on_load') ? !!settings.fetch_counts_on_load : true;
        countsToggleEl.checked = !!flag;
      }
    } catch { }

    // Populate preload_vontology_tree toggle
    try {
      const preloadToggleEl = document.getElementById('preloadVontologyTreeToggle');
      if (preloadToggleEl) {
        const flag = Object.prototype.hasOwnProperty.call(settings, 'preload_vontology_tree') ? !!settings.preload_vontology_tree : false;
        preloadToggleEl.checked = !!flag;
      }
    } catch { }

    // Populate disable_remote_ollama_scan toggle
    try {
      const remoteScanToggleEl = document.getElementById('disableRemoteOllamaScanToggle');
      if (remoteScanToggleEl) {
        const flag = Object.prototype.hasOwnProperty.call(settings, 'disable_remote_ollama_scan') ? !!settings.disable_remote_ollama_scan : false;
        remoteScanToggleEl.checked = !!flag;
      }
    } catch { }

    // Populate internal MCP execution caps
    try {
      const maxInvEl = document.getElementById('internalMcpMaxToolInvocations');
      if (maxInvEl) {
        const raw = Object.prototype.hasOwnProperty.call(settings, 'internal_mcp_max_tool_invocations')
          ? settings.internal_mcp_max_tool_invocations
          : 30;
        maxInvEl.value = String(clampNumber(raw, 0, 50, 30));
      }
      const batchCapEl = document.getElementById('internalMcpToolBatchCap');
      if (batchCapEl) {
        const raw = Object.prototype.hasOwnProperty.call(settings, 'internal_mcp_tool_batch_cap')
          ? settings.internal_mcp_tool_batch_cap
          : 10;
        batchCapEl.value = String(clampNumber(raw, 1, 50, 10));
      }
    } catch { }

    // Populate tool-use during thinking toggle (JVNAUTOSCI-942)
    try {
      const toolUseToggleEl = document.getElementById('showToolUseDuringThinkingToggle');
      if (toolUseToggleEl) {
        const flag = Object.prototype.hasOwnProperty.call(settings, 'show_tool_use_during_thinking')
          ? !!settings.show_tool_use_during_thinking
          : true;
        toolUseToggleEl.checked = !!flag;
      }
    } catch { }

    // Populate buttonify quick-reply toggle
    try {
      const buttonifyToggleEl = document.getElementById('buttonifyModelEnabledToggle');
      if (buttonifyToggleEl) {
        const flag = Object.prototype.hasOwnProperty.call(settings, 'buttonify_model_enabled')
          ? !!settings.buttonify_model_enabled
          : true;
        buttonifyToggleEl.checked = !!flag;
      }
    } catch { }

    // Populate minimal-imposition auto-proceed toggle
    try {
      const autoProceedToggleEl = document.getElementById('autoProceedMinimalImpositionToggle');
      if (autoProceedToggleEl) {
        const flag = Object.prototype.hasOwnProperty.call(settings, 'auto_proceed_minimal_imposition_enabled')
          ? !!settings.auto_proceed_minimal_imposition_enabled
          : true;
        autoProceedToggleEl.checked = !!flag;
      }
    } catch { }

    // Populate buttonify prompt link
    try {
      const promptLinkEl = document.getElementById('buttonifyPromptLink');
      if (promptLinkEl) {
        const promptId = settings.buttonify_prompt_active || '#V#buttonify_prompt_v1';
        promptLinkEl.textContent = promptId || '—';
        if (promptId) {
          promptLinkEl.dataset.conceptId = promptId;
          promptLinkEl.disabled = false;
        } else {
          delete promptLinkEl.dataset.conceptId;
          promptLinkEl.disabled = true;
        }
      }
    } catch { }

    // Populate admin-only write-tool conservatism toggle (inverse of disable flag).
    try {
      const adminToggleEl = document.getElementById('disableWriteToolConservatismToggle');
      if (adminToggleEl) {
        const disabled = Object.prototype.hasOwnProperty.call(settings, 'disable_write_tool_conservatism')
          ? !!settings.disable_write_tool_conservatism
          : true;
        adminToggleEl.checked = !disabled;
        adminToggleEl.disabled = !__vonIsAdminOrOwner;

        _setWriteConservatismOverrideBadgeEnabled(!!adminToggleEl.checked);

        // Live-update badge for immediate feedback (even before save).
        if (!adminToggleEl.__vonBound) {
          adminToggleEl.__vonBound = true;
          adminToggleEl.addEventListener('change', () => {
            _setWriteConservatismOverrideBadgeEnabled(!!adminToggleEl.checked);
          });
        }
      }
    } catch { }

    // Populate Jira tool guardrails (read-only env visibility)
    try {
      const { allowListText, executeModeText } = __testOnly_formatJiraGuardrailSettings(settings);

      const allowEl = document.getElementById('settingsJiraProjectAllowListValue');
      if (allowEl) {
        allowEl.textContent = allowListText || 'JVNAUTOSCI';
      }

      const executeEl = document.getElementById('settingsJiraExecuteModeValue');
      if (executeEl) {
        executeEl.textContent = executeModeText || 'disabled (0)';
      }
    } catch { }

    // Populate preferred language setting
    const languageSelect = document.getElementById('preferredLanguageSelect');
    if (languageSelect) {
      // Prefer locally stored value over server
      const storedLang = localStorage.getItem(LS_LANG_KEY) || settings.preferred_language;
      populateLanguageSelect(languageSelect, storedLang);
      if (storedLang) localStorage.setItem(LS_LANG_KEY, storedLang);
    }

    // Update Gmail profile status line
    updateGmailProfileStatus();

    // Check if the API key exists and verify it to load models
    if (await checkOpenAiEnvVar()) {
      await verifyOpenAiApiKey(currentOpenAIModel); // Pass the current OpenAI model
    }

    // Trigger height update after all settings content is loaded
    setTimeout(() => {
      if (window.parent) {
        window.parent.postMessage({ type: 'settings-content-loaded' }, '*');
      }
    }, 100);

  } catch (error) {
    console.error('Error loading settings:', error);
    showStatusMessage('settingsStatusMessage', 'Failed to load settings.', true);
  }
}

// ---------------- Deprecation Metrics ----------------
async function loadDeprecationMetrics(manual = false) {
  const statusEl = document.getElementById('deprecationMetricsStatus');
  const contentEl = document.getElementById('deprecationMetricsContent');
  const tsEl = document.getElementById('deprecationMetricsTimestamp');
  const perfContentEl = document.getElementById('performanceMetricsContent');
  const perfTsEl = document.getElementById('performanceMetricsTimestamp');
  if (!contentEl) return;
  if (statusEl) {
    statusEl.style.display = 'inline-block';
    statusEl.textContent = manual ? 'Refreshing…' : 'Loading…';
    statusEl.className = 'status-message';
  }
  try {
    const res = await fetch('/api/settings/metrics/deprecations', { cache: 'no-cache' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const counters = data.counters || {};
    const entries = Object.entries(counters);
    if (!entries.length) {
      contentEl.innerHTML = '<div class="metric-empty">No deprecation usage recorded yet.</div>';
    } else {
      contentEl.innerHTML = entries.map(([k, v]) => `
        <div class="metric-item">
          <div class="metric-key">${k}</div>
          <div class="metric-value">${v}</div>
        </div>`).join('');
    }
    if (tsEl) {
      const ts = data.updated_at ? new Date(data.updated_at).toLocaleString() : 'N/A';
      tsEl.textContent = 'Last Updated: ' + ts;
    }
    // Performance metrics (tree build timings)
    if (perfContentEl) {
      const tree = data.performance?.tree_build;
      if (!tree) {
        perfContentEl.innerHTML = '<div class="metric-empty">No performance data recorded yet.</div>';
      } else {
        const rows = [
          ['tree_build.count', tree.count],
          ['tree_build.last_sec', tree.last_sec?.toFixed ? tree.last_sec.toFixed(3) : tree.last_sec],
          ['tree_build.max_sec', tree.max_sec?.toFixed ? tree.max_sec.toFixed(3) : tree.max_sec],
          ['tree_build.avg_sec', tree.avg_sec?.toFixed ? tree.avg_sec.toFixed(3) : tree.avg_sec],
          ['tree_build.last_built_at', tree.last_built_at ? new Date(tree.last_built_at).toLocaleString() : 'N/A']
        ];
        perfContentEl.innerHTML = rows.map(([k, v]) => `
          <div class="metric-item">
            <div class="metric-key">${k}</div>
            <div class="metric-value">${v ?? '—'}</div>
          </div>`).join('');
      }
      if (perfTsEl) {
        const ts2 = data.performance?.tree_build?.last_built_at ? new Date(data.performance.tree_build.last_built_at).toLocaleString() : 'N/A';
        perfTsEl.textContent = 'Last Build: ' + ts2;
      }
    }
    if (statusEl) {
      statusEl.textContent = 'Loaded';
      statusEl.className = 'status-message success';
      setTimeout(() => { if (statusEl) statusEl.style.display = 'none'; }, 2000);
    }
  } catch (e) {
    console.warn('Failed to load deprecation metrics', e);
    if (contentEl) contentEl.innerHTML = '<div class="metric-error">Error loading metrics</div>';
    if (perfContentEl) perfContentEl.innerHTML = '<div class="metric-error">Error loading performance metrics</div>';
    if (statusEl) {
      statusEl.textContent = 'Failed to load';
      statusEl.className = 'status-message error';
    }
  }
}

async function loadAndDisplayDbInfo() {
  try {
    const res = await fetch('/api/settings/db/info');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const info = await res.json();
    const uriEl = document.getElementById('dbUri');
    const nameEl = document.getElementById('dbName');
    const pingEl = document.getElementById('dbPingStatus');
    const errEl = document.getElementById('dbPingError');
    if (uriEl) uriEl.textContent = info.sanitized_uri || 'Unknown';
    if (nameEl) nameEl.textContent = info.database_name || 'Unknown';
    if (pingEl) {
      if (info.ping_ok) {
        pingEl.textContent = 'Connected';
        pingEl.style.background = '#d4edda';
        pingEl.style.color = '#155724';
      } else {
        pingEl.textContent = 'Unavailable';
        pingEl.style.background = '#f8d7da';
        pingEl.style.color = '#721c24';
        if (errEl && info.error) { errEl.textContent = info.error; errEl.style.display = 'inline'; }
      }
    }
  } catch (e) {
    const uriEl = document.getElementById('dbUri');
    const nameEl = document.getElementById('dbName');
    const pingEl = document.getElementById('dbPingStatus');
    const errEl = document.getElementById('dbPingError');
    if (uriEl) uriEl.textContent = 'Error';
    if (nameEl) nameEl.textContent = 'Error';
    if (pingEl) { pingEl.textContent = 'Unavailable'; pingEl.style.background = '#f8d7da'; pingEl.style.color = '#721c24'; }
    if (errEl) { errEl.textContent = String(e.message || e); errEl.style.display = 'inline'; }
  }
}

async function saveAllSettings() {
  const { scope: llmScope, conceptId: llmConceptId } = resolveActiveLlmScopeContext();
  const enabledLlms = buildPersistedLlmSelections();
  let activeLlm = resolveActiveLlmFromSelections(enabledLlms, 'openai');

  if (activeLlm && llmScope && llmConceptId) {
    activeLlm.scope = llmScope;
    activeLlm.concept_id = llmConceptId;
  } else {
    activeLlm = null;
  }

  // We now persist user/org/language only in localStorage; do not send to backend
  const settings = {
    openai_api_key_env_var: document.getElementById('openaiApiKeyEnvVar')?.value,
    preload_vontology_tree: !!document.getElementById('preloadVontologyTreeToggle')?.checked,
    fetch_counts_on_load: !!document.getElementById('fetchCountsOnLoadToggle')?.checked,
    disable_remote_ollama_scan: !!document.getElementById('disableRemoteOllamaScanToggle')?.checked,
    internal_mcp_max_tool_invocations: clampNumber(
      document.getElementById('internalMcpMaxToolInvocations')?.value,
      0,
      50,
      30,
    ),
    internal_mcp_tool_batch_cap: clampNumber(
      document.getElementById('internalMcpToolBatchCap')?.value,
      1,
      50,
      10,
    ),
    show_tool_use_during_thinking: !!document.getElementById('showToolUseDuringThinkingToggle')?.checked,
    buttonify_model_enabled: !!document.getElementById('buttonifyModelEnabledToggle')?.checked,
    auto_proceed_minimal_imposition_enabled: !!document.getElementById('autoProceedMinimalImpositionToggle')?.checked,
  };

  if (__canPersistWriteConservatism) {
    const adminToggleEl = document.getElementById('disableWriteToolConservatismToggle');
    if (adminToggleEl) {
      settings.disable_write_tool_conservatism = !adminToggleEl.checked;
    }
  }

  if (activeLlm && enabledLlms.length) {
    settings.active_llm = activeLlm;
    settings.enabled_llms = enabledLlms;
  }

  try {
    const response = await fetch('/api/settings/', {
      method: 'POST',
      headers: buildSettingsFetchHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(settings),
    });
    const payload = await response.json().catch(() => ({}));

    if (!response.ok) {
      const message = payload?.message || `Failed to save settings (${response.status})`;
      throw new Error(message);
    }

    if (
      activeLlm
      && (
        !payload?.resolved_llm
        || payload.resolved_llm.provider !== activeLlm.provider
        || payload.resolved_llm.model !== activeLlm.model
      )
    ) {
      throw new Error('Model change did not take effect for the current context.');
    }

    if (payload?.resolved_llm) {
      currentResolvedLlm = payload.resolved_llm;
    }

    showStatusMessage('settingsStatusMessage', payload.message || 'Settings saved successfully!');

    // Update parent window footer
    if (window.parent?.updateModelInfoFooterDisplay) {
      window.parent.updateModelInfoFooterDisplay();
    }

    // Emit event to update language indicator in footer
    if (window.parent) {
      window.parent.document.dispatchEvent(new CustomEvent('von:settingsChanged'));
    }
    return true;
  } catch (error) {
    console.error('Error saving settings:', error);
    showStatusMessage(
      'settingsStatusMessage',
      error?.message || 'Failed to save settings.',
      true,
    );
    return false;
  }
}

// --- User concept preference helpers (server-side stored) ---

async function loadUserConceptPreferences(userConceptId) {
  try {
    const data = await fetchUserPreferences(userConceptId);
    if (!data) return;
    if (data.preferred_language) {
      const langSel = document.getElementById('preferredLanguageSelect');
      if (langSel) {
        langSel.value = data.preferred_language;
        localStorage.setItem(LS_LANG_KEY, data.preferred_language);
      }
    }
    if (data.organisation_concept_id) {
      const orgSel = document.getElementById('currentOrganisationSelect');
      if (orgSel) {
        for (const opt of orgSel.options) {
          if (opt.dataset?.conceptId === data.organisation_concept_id) { opt.selected = true; break; }
        }
        const selOpt = orgSel.selectedOptions?.[0];
        if (selOpt) {
          setStoredJson(LS_ORG_KEY, buildStoredOrganisationContextFromOption(selOpt));
        }
      }
    } else {
      const orgSel = document.getElementById('currentOrganisationSelect');
      if (orgSel) {
        orgSel.selectedIndex = 0;
      }
      setStoredJson(LS_ORG_KEY, null);
      if (window.parent?.updateModelInfoFooterDisplay) { window.parent.updateModelInfoFooterDisplay(); }
    }
  } catch (e) {
    console.warn('Failed to load user concept preferences', e);
  }
}

async function persistCurrentUserPreferences() {
  try {
    const storedUser = getStoredJson(LS_USER_KEY);
    if (!storedUser || !storedUser.concept_id) return; // need user concept id
    const language = localStorage.getItem(LS_LANG_KEY) || null;
    const storedOrg = getStoredJson(LS_ORG_KEY);
    const organisationConceptId = storedOrg?.concept_id || null;
    const payload = { preferred_language: language, organisation_concept_id: organisationConceptId };
    const resp = await fetch(`/api/settings/user_prefs/${encodeURIComponent(storedUser.concept_id)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!resp.ok) {
      console.warn('Failed to persist user prefs', resp.status);
    }
  } catch (e) {
    console.warn('Error persisting user preferences', e);
  }
}

function parseCommaSeparatedProfileValues(rawValue) {
  const raw = String(rawValue || '').trim();
  if (!raw) return [];

  const deduped = [];
  const seen = new Set();
  for (const part of raw.split(',')) {
    const cleaned = part.trim();
    if (!cleaned) continue;
    const fingerprint = cleaned.toLocaleLowerCase();
    if (seen.has(fingerprint)) continue;
    seen.add(fingerprint);
    deduped.push(cleaned);
  }
  return deduped;
}

function joinProfileValues(values) {
  if (!Array.isArray(values) || !values.length) return '';
  return values
    .filter((value) => typeof value === 'string' && value.trim())
    .map((value) => value.trim())
    .join(', ');
}

function setRecommendationProfileStatus(message, isError = false) {
  const el = document.getElementById('recommendationProfileStatusMessage');
  if (!el) return;
  el.textContent = message || '';
  el.className = isError ? 'status-message error' : 'status-message success';
  el.style.display = message ? 'block' : 'none';
}

function setRecommendationProfileControlsDisabled(disabled) {
  const elementIds = [
    'recommendationProjectDescriptionInput',
    'recommendationInterestTermsInput',
    'recommendationNegativeTermsInput',
    'recommendationPreferredAuthorsInput',
    'recommendationPreferredVenuesInput',
    'recommendationProfileNotesInput',
    'saveRecommendationProfileButton',
  ];
  for (const id of elementIds) {
    const el = document.getElementById(id);
    if (el) el.disabled = !!disabled;
  }
}

function parseCandidatePaperConceptIds(rawValue) {
  const raw = String(rawValue || '').trim();
  if (!raw) return [];

  const deduped = [];
  const seen = new Set();
  for (const part of raw.split(/[\s,]+/)) {
    const cleaned = part.trim();
    if (!cleaned) continue;
    if (seen.has(cleaned)) continue;
    seen.add(cleaned);
    deduped.push(cleaned);
  }
  return deduped;
}

function normaliseCandidateLimit(rawValue) {
  const parsed = Number.parseInt(String(rawValue || ''), 10);
  if (!Number.isFinite(parsed)) return 25;
  return Math.min(100, Math.max(1, parsed));
}

function setRecommendationReviewStatus(message, isError = false) {
  const el = document.getElementById('recommendationReviewStatusMessage');
  if (!el) return;
  el.textContent = message || '';
  el.className = isError ? 'status-message error' : 'status-message success';
  el.style.display = message ? 'block' : 'none';
}

function setRecommendationReviewControlsDisabled(disabled) {
  const elementIds = [
    'recommendationCandidatePaperIdsInput',
    'recommendationCandidateLimitInput',
    'recommendationIncludeAllCandidatesToggle',
    'recommendationAutoReviewAfterProfileSaveToggle',
    'runRecommendationReviewButton',
  ];
  for (const id of elementIds) {
    const el = document.getElementById(id);
    if (el) el.disabled = !!disabled;
  }
}

function clearRecommendationReviewResults(
  message = 'Run a review to inspect recommendation rationale and provenance.',
) {
  const summary = document.getElementById('recommendationReviewSummary');
  if (summary) {
    summary.textContent = message;
  }
  const container = document.getElementById('recommendationReviewResults');
  if (container) {
    container.replaceChildren();
  }
}

function formatRecommendationScalar(value, fallback = 'None') {
  const cleaned = String(value || '').trim();
  return cleaned || fallback;
}

function resolveRecommendationRationaleSummary(row) {
  const summary = String(row?.rationale_summary || '').trim();
  if (summary) return summary;
  const rationale = String(row?.rationale || '').trim();
  if (rationale) return rationale;
  const generationStatus = String(row?.rationale_generation?.status || '').trim().toLowerCase();
  if (generationStatus === 'unavailable') {
    return 'No authoritative relevance explanation was generated for this recommendation yet.';
  }
  return '';
}

function appendRecommendationList(container, title, items) {
  if (!container || !Array.isArray(items) || !items.length) return;
  const heading = document.createElement('div');
  heading.className = 'speech-settings-note';
  heading.textContent = title;
  container.appendChild(heading);

  const list = document.createElement('ul');
  list.className = 'recommendation-review-list';
  for (const item of items) {
    const li = document.createElement('li');
    li.textContent = item;
    list.appendChild(li);
  }
  container.appendChild(list);
}

function renderRecommendationReviewPayload(payload) {
  const summary = document.getElementById('recommendationReviewSummary');
  const container = document.getElementById('recommendationReviewResults');
  if (!summary || !container) return;

  const trigger = payload?.trigger || {};
  const candidateSelection = payload?.candidate_selection || {};
  const report = payload?.recommendation_report || {};
  const authoritativeSurface = payload?.authoritative_review_surface || 'settings_tab';
  const externalAuthoritative = payload?.external_channels_authoritative === true;

  const summaryBits = [
    `Trigger: ${formatRecommendationScalar(trigger.label || trigger.trigger_source, 'manual review')}.`,
    `Candidate source: ${formatRecommendationScalar(candidateSelection.source, 'unknown')}.`,
    `Candidates: ${Number(candidateSelection.candidate_count || 0)}.`,
    `Authoritative surface: ${formatRecommendationScalar(authoritativeSurface)}.`,
    externalAuthoritative
      ? 'External channels are authoritative.'
      : 'External channels are not authoritative.',
  ];
  summary.textContent = summaryBits.join(' ');
  container.replaceChildren();

  if (!report?.success) {
    const panel = document.createElement('div');
    panel.className = 'recommendation-review-card is-skipped';

    const title = document.createElement('h4');
    title.className = 'recommendation-review-title';
    title.textContent = formatRecommendationScalar(
      report?.message,
      'Recommendation review could not produce ranked results.',
    );
    panel.appendChild(title);

    appendRecommendationList(panel, 'Suggestions', report?.suggestions || []);
    container.appendChild(panel);
    return;
  }

  const results = Array.isArray(report?.results) ? report.results : [];
  if (!results.length) {
    const hint = document.createElement('div');
    hint.className = 'runtime-hint';
    hint.textContent = 'No recommendation rows were returned for this review.';
    container.appendChild(hint);
    return;
  }

  for (const row of results) {
    const card = document.createElement('article');
    const status = String(row?.status || '').trim().toLowerCase();
    card.className = `recommendation-review-card${status === 'skipped' ? ' is-skipped' : ''}`;

    const header = document.createElement('div');
    header.className = 'recommendation-review-header';

    const title = document.createElement('h4');
    title.className = 'recommendation-review-title';
    title.textContent = formatRecommendationScalar(row?.paper_title, row?.paper_concept_id);
    header.appendChild(title);

    const badge = document.createElement('span');
    const tier = String(row?.recommendation_tier || status || 'unmatched').trim().toLowerCase();
    badge.className = `recommendation-review-badge ${status === 'skipped' ? 'status-skipped' : `tier-${tier}`}`;
    badge.textContent = status === 'skipped'
      ? 'Skipped'
      : `${formatRecommendationScalar(row?.recommendation_tier, 'unmatched')} · ${Number(
        row?.score || 0,
      ).toFixed(2)}`;
    header.appendChild(badge);
    card.appendChild(header);

    const meta = document.createElement('ul');
    meta.className = 'recommendation-review-meta';
    const metaItems = [
      `Paper concept: ${formatRecommendationScalar(row?.paper_concept_id)}`,
      `Publication date: ${formatRecommendationScalar(row?.paper_representation?.publication_date)}`,
    ];
    if (status === 'skipped') {
      metaItems.push(`Skip reason: ${formatRecommendationScalar(row?.skip_reason)}`);
    }
    for (const item of metaItems) {
      const li = document.createElement('li');
      li.textContent = item;
      meta.appendChild(li);
    }
    card.appendChild(meta);

    const summaryText = document.createElement('div');
    summaryText.className = 'recommendation-review-summary';
    summaryText.textContent = formatRecommendationScalar(
      resolveRecommendationRationaleSummary(row),
      'No authoritative relevance explanation was generated for this recommendation yet.',
    );
    card.appendChild(summaryText);

    appendRecommendationList(card, 'Rationale', row?.rationale || []);

    const evidenceItems = Array.isArray(row?.evidence)
      ? row.evidence.map((item) => {
          const evidenceType = formatRecommendationScalar(item?.evidence_type);
          const profileValue = formatRecommendationScalar(item?.profile_value);
          const matchedText = formatRecommendationScalar(item?.matched_paper_text);
          const referenceId = formatRecommendationScalar(item?.paper_reference_id);
          return `${evidenceType}: ${profileValue} -> ${matchedText} (ref: ${referenceId})`;
        })
      : [];
    appendRecommendationList(card, 'Evidence', evidenceItems);

    const provenance = row?.provenance || {};
    const provenanceItems = [
      `Paper concept: ${formatRecommendationScalar(provenance?.paper_concept_id)}`,
      `Author concepts: ${Array.isArray(provenance?.author_concept_ids) && provenance.author_concept_ids.length
        ? provenance.author_concept_ids.join(', ')
        : 'None'}`,
      `Topic concepts: ${Array.isArray(provenance?.topic_concept_ids) && provenance.topic_concept_ids.length
        ? provenance.topic_concept_ids.join(', ')
        : 'None'}`,
      `Summary relation: ${formatRecommendationScalar(provenance?.summary_relation_id)}`,
    ];
    appendRecommendationList(card, 'Provenance', provenanceItems);

    if (status === 'skipped' && Array.isArray(row?.representation_failures) && row.representation_failures.length) {
      appendRecommendationList(card, 'Representation failures', row.representation_failures);
    }

    container.appendChild(card);
  }
}

function clearRecommendationProfileForm() {
  const fieldIds = [
    'recommendationProjectDescriptionInput',
    'recommendationInterestTermsInput',
    'recommendationNegativeTermsInput',
    'recommendationPreferredAuthorsInput',
    'recommendationPreferredVenuesInput',
    'recommendationProfileNotesInput',
  ];
  for (const id of fieldIds) {
    const el = document.getElementById(id);
    if (el) el.value = '';
  }
  const observed = document.getElementById('recommendationObservedInterests');
  if (observed) {
    observed.textContent = 'Select a user to load profile context.';
  }
  clearRecommendationReviewResults();
}

function renderObservedRecommendationInterests(derivedContext) {
  const observed = document.getElementById('recommendationObservedInterests');
  if (!observed) return;
  const interests = Array.isArray(derivedContext?.research_interest_concepts)
    ? derivedContext.research_interest_concepts
    : [];
  if (!interests.length) {
    observed.textContent = 'No linked research-interest concepts found for this user yet.';
    return;
  }
  observed.textContent = interests
    .map((item) => String(item?.name || item?.concept_id || '').trim())
    .filter(Boolean)
    .join(', ');
}

function applyRecommendationProfileToForm(profilePayload) {
  const profile = profilePayload?.profile || {};
  const projectDescriptionInput = document.getElementById('recommendationProjectDescriptionInput');
  const interestTermsInput = document.getElementById('recommendationInterestTermsInput');
  const negativeTermsInput = document.getElementById('recommendationNegativeTermsInput');
  const preferredAuthorsInput = document.getElementById('recommendationPreferredAuthorsInput');
  const preferredVenuesInput = document.getElementById('recommendationPreferredVenuesInput');
  const notesInput = document.getElementById('recommendationProfileNotesInput');

  if (projectDescriptionInput) projectDescriptionInput.value = profile.project_description || '';
  if (interestTermsInput) interestTermsInput.value = joinProfileValues(profile.stated_interest_terms);
  if (negativeTermsInput) negativeTermsInput.value = joinProfileValues(profile.negative_interest_terms);
  if (preferredAuthorsInput) preferredAuthorsInput.value = joinProfileValues(profile.preferred_authors);
  if (preferredVenuesInput) preferredVenuesInput.value = joinProfileValues(profile.preferred_venues);
  if (notesInput) notesInput.value = profile.notes || '';

  renderObservedRecommendationInterests(profilePayload?.derived_context);
}

export function __testOnly_buildRecommendationProfilePayload(overrides = {}) {
  const values = {
    project_description: document.getElementById('recommendationProjectDescriptionInput')?.value || '',
    stated_interest_terms: parseCommaSeparatedProfileValues(
      document.getElementById('recommendationInterestTermsInput')?.value,
    ),
    negative_interest_terms: parseCommaSeparatedProfileValues(
      document.getElementById('recommendationNegativeTermsInput')?.value,
    ),
    preferred_authors: parseCommaSeparatedProfileValues(
      document.getElementById('recommendationPreferredAuthorsInput')?.value,
    ),
    preferred_venues: parseCommaSeparatedProfileValues(
      document.getElementById('recommendationPreferredVenuesInput')?.value,
    ),
    notes: document.getElementById('recommendationProfileNotesInput')?.value || '',
  };
  return { ...values, ...overrides };
}

export function __testOnly_buildRecommendationReviewRequest(overrides = {}) {
  const values = {
    candidate_paper_concept_ids: parseCandidatePaperConceptIds(
      document.getElementById('recommendationCandidatePaperIdsInput')?.value,
    ),
    candidate_limit: normaliseCandidateLimit(
      document.getElementById('recommendationCandidateLimitInput')?.value,
    ),
    include_all_candidates: !!document.getElementById('recommendationIncludeAllCandidatesToggle')?.checked,
    trigger_source: 'manual_review',
  };
  return { ...values, ...overrides };
}

export function __testOnly_renderRecommendationReviewPayload(payload) {
  renderRecommendationReviewPayload(payload);
}

async function loadRecommendationProfileForSelectedUser() {
  const storedUser = getStoredJson(LS_USER_KEY);
  const userConceptId = storedUser?.concept_id || null;
  if (!userConceptId) {
    clearRecommendationProfileForm();
    setRecommendationProfileControlsDisabled(true);
    setRecommendationReviewControlsDisabled(true);
    setRecommendationProfileStatus('Select a current user to inspect a recommendation profile.');
    setRecommendationReviewStatus('');
    return;
  }

  setRecommendationProfileControlsDisabled(false);
  setRecommendationReviewControlsDisabled(false);
  setRecommendationProfileStatus('Loading recommendation profile...');
  try {
    const response = await fetch(`/api/settings/recommendation_profile/${encodeURIComponent(userConceptId)}`, {
      cache: 'no-store',
      headers: buildSettingsFetchHeaders(),
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const data = await response.json();
    applyRecommendationProfileToForm(data);
    setRecommendationProfileStatus('Recommendation profile loaded.');
    clearRecommendationReviewResults();
    setRecommendationReviewStatus('');
  } catch (error) {
    console.warn('Failed to load recommendation profile', error);
    clearRecommendationProfileForm();
    setRecommendationProfileStatus('Failed to load recommendation profile.', true);
    setRecommendationReviewStatus('Recommendation review is unavailable until the profile loads.', true);
  }
}

async function generateRecommendationReviewForSelectedUser({ triggerSource = 'manual_review' } = {}) {
  const storedUser = getStoredJson(LS_USER_KEY);
  const userConceptId = storedUser?.concept_id || null;
  if (!userConceptId) {
    setRecommendationReviewStatus('Select a current user before running a recommendation review.', true);
    return;
  }

  setRecommendationReviewStatus('Building recommendation review...');
  try {
    const response = await fetch(`/api/settings/recommendation_review/${encodeURIComponent(userConceptId)}`, {
      method: 'POST',
      headers: buildSettingsFetchHeaders({
        'Content-Type': 'application/json',
      }),
      body: JSON.stringify(
        __testOnly_buildRecommendationReviewRequest({
          trigger_source: triggerSource,
        }),
      ),
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const data = await response.json();
    renderRecommendationReviewPayload(data);
    const reportMessage = data?.recommendation_report?.message;
    if (data?.success) {
      setRecommendationReviewStatus(reportMessage || 'Recommendation review ready.');
    } else {
      setRecommendationReviewStatus(
        reportMessage || 'Recommendation review completed with diagnostics rather than ranked results.',
        true,
      );
    }
  } catch (error) {
    console.warn('Failed to build recommendation review', error);
    clearRecommendationReviewResults();
    setRecommendationReviewStatus('Failed to build recommendation review.', true);
  }
}

async function saveRecommendationProfileForSelectedUser() {
  const storedUser = getStoredJson(LS_USER_KEY);
  const userConceptId = storedUser?.concept_id || null;
  if (!userConceptId) {
    setRecommendationProfileStatus('Select a current user before saving a recommendation profile.', true);
    return;
  }

  setRecommendationProfileStatus('Saving recommendation profile...');
  try {
    const response = await fetch(`/api/settings/recommendation_profile/${encodeURIComponent(userConceptId)}`, {
      method: 'POST',
      headers: buildSettingsFetchHeaders({
        'Content-Type': 'application/json',
      }),
      body: JSON.stringify(__testOnly_buildRecommendationProfilePayload()),
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const data = await response.json();
    applyRecommendationProfileToForm(data);
    setRecommendationProfileStatus('Recommendation profile saved.');
    if (document.getElementById('recommendationAutoReviewAfterProfileSaveToggle')?.checked) {
      await generateRecommendationReviewForSelectedUser({ triggerSource: 'profile_saved' });
    }
  } catch (error) {
    console.warn('Failed to save recommendation profile', error);
    setRecommendationProfileStatus('Failed to save recommendation profile.', true);
  }
}

export async function checkOpenAiEnvVar() {
  const apiKeyEnvVar = document.getElementById('openaiApiKeyEnvVar').value;
  const verifyButton = document.getElementById('verifyOpenAiApiKeyButton');
  const statusMessage = document.getElementById('openaiStatusMessage');

  try {
    const response = await postJson('/api/settings/env_var/check', { env_var_name: apiKeyEnvVar });
    if (response.exists) {
      verifyButton.classList.remove('hidden');
      verifyButton.style.backgroundColor = '';
      verifyButton.style.display = 'inline-block';
      verifyButton.disabled = false;
      if (response.source === 'dotenv') {
        statusMessage.textContent = `Key found (from .env because not in process environment): ${response.masked_value}. Verify and test the selected model before enabling premium use.`;
      } else {
        statusMessage.textContent = `Key found: ${response.masked_value}. Verify and test the selected model before enabling premium use.`;
      }
      statusMessage.className = 'status-message';
      statusMessage.style.display = 'block';
      return true;
    } else {
      verifyButton.classList.add('hidden');
      verifyButton.style.display = 'none';
      verifyButton.disabled = true;
      statusMessage.textContent = 'No key found for this environment variable.';
      statusMessage.className = 'status-message error';
      statusMessage.style.display = 'block';
      return false;
    }
  } catch (error) {
    console.error('Error checking environment variable:', error);
    verifyButton.classList.add('hidden');
    verifyButton.style.display = 'none';
    verifyButton.disabled = true;
    statusMessage.textContent = 'Error checking for key.';
    statusMessage.className = 'status-message error';
    statusMessage.style.display = 'block';
    return false;
  }
}

async function verifyOpenAiApiKey(savedModel = null) {
  const apiKeyEnvVar = document.getElementById('openaiApiKeyEnvVar').value;
  const statusMessage = document.getElementById('openaiStatusMessage');
  const modelsContainer = document.getElementById('openaiModelsContainer');
  const preferredModel = savedModel || getStoredOpenAiSelectedModel() || null;

  setInlineStatusMessage(
    statusMessage,
    'Verifying API key and loading premium models...',
    null,
  );

  try {
    const response = await postJson('/api/settings/openai/verify', { api_key_env_var: apiKeyEnvVar });

    if (response.success) {
      setInlineStatusMessage(
        statusMessage,
        'API key verified. Premium models loaded. Test the selected model before relying on it.',
        null,
      );
      modelsContainer.style.display = 'block';
      modelsContainer.classList.remove('hidden');

      renderOpenAIModelOptions('openaiModelSelect', response.models || [], preferredModel);
      const selectedOpenAiModel = document.getElementById('openaiModelSelect')?.value;
      if (selectedOpenAiModel) {
        setStoredOpenAiSelectedModel(selectedOpenAiModel);
      }
      latestOpenAiModelProbe = null;
      updateOpenAiModelStatusMessage();
    } else {
      throw new Error(response.error || 'Failed to verify API key.');
    }
  } catch (error) {
    // As a fallback, try the direct OpenAI models endpoint
    try {
      await populateOpenAIModelDropdown('openaiModelSelect', preferredModel);
      setInlineStatusMessage(
        statusMessage,
        'Loaded a cached premium model list, but live verification failed. Test the selected model before enabling premium use.',
        'error',
      );
      modelsContainer.style.display = 'block';
      modelsContainer.classList.remove('hidden');
      latestOpenAiModelProbe = null;
      updateOpenAiModelStatusMessage();
    } catch (_) {
      setInlineStatusMessage(statusMessage, `Error: ${error.message}`, 'error');
      modelsContainer.style.display = 'none';
      modelsContainer.classList.add('hidden');
      latestOpenAiModelProbe = null;
      updateOpenAiModelStatusMessage();
    }
  }
}

// Remove the old, separate save functions (saveGlobalModel, saveCurrentUser, saveOpenAISettings)
// as they are now replaced by the single saveAllSettings function.

export async function loadSettings() {
  try {
    const response = await fetch('/api/settings/', {
      cache: 'no-store',
      headers: buildSettingsFetchHeaders(),
    });
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }
    return await response.json();
  } catch (error) {
    console.error('Error loading settings:', error);
    return {};
  }
}

export async function saveSettings(settings) {
  try {
    const response = await fetch('/api/settings/', {
      method: 'POST',
      headers: buildSettingsFetchHeaders({
        'Content-Type': 'application/json'
      }),
      body: JSON.stringify(settings)
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    return await response.json();
  } catch (error) {
    console.error('Error saving settings:', error);
    throw error;
  }
}

export function __testOnly_formatJiraGuardrailSettings(settings) {
  const rawAllow = typeof settings?.jira_project_allow_list_raw === 'string'
    ? settings.jira_project_allow_list_raw
    : null;

  let effective = settings?.jira_project_allow_list_effective;
  if (!Array.isArray(effective) || !effective.length) {
    effective = ['JVNAUTOSCI'];
  }
  const allowListText = effective.map(x => String(x)).filter(Boolean).join(', ');

  const rawExecute = (settings && Object.prototype.hasOwnProperty.call(settings, 'internal_mcp_jira_execute_mode_raw'))
    ? String(settings.internal_mcp_jira_execute_mode_raw)
    : '0';

  const enabled = !!settings?.internal_mcp_jira_execute_mode_enabled;
  const executeModeText = enabled
    ? `enabled (${rawExecute})`
    : `disabled (${rawExecute})`;

  return {
    allowListText,
    rawAllow,
    executeModeText,
    executeModeEnabled: enabled
  };
}

export function validateSettings() {
  const apiEndpoint = document.getElementById('apiEndpoint')?.value;
  const defaultEntityType = document.getElementById('defaultEntityType')?.value;
  const theme = document.getElementById('theme')?.value;

  // Validate API endpoint URL
  try {
    new URL(apiEndpoint);
  } catch {
    return false;
  }

  // Validate entity type
  if (!defaultEntityType) {
    return false;
  }

  // Validate theme
  if (!['light', 'dark'].includes(theme)) {
    return false;
  }

  return true;
}

export function resetSettings() {
  const apiEndpoint = document.getElementById('apiEndpoint');
  const defaultEntityType = document.getElementById('defaultEntityType');
  const enableAutoSave = document.getElementById('enableAutoSave');
  const theme = document.getElementById('theme');
  const settingsStatus = document.getElementById('settingsStatus');

  if (apiEndpoint) apiEndpoint.value = 'http://localhost:5000';
  if (defaultEntityType) defaultEntityType.value = 'Concept/Person';
  if (enableAutoSave) enableAutoSave.checked = true;
  if (theme) theme.value = 'light';

  if (settingsStatus) {
    settingsStatus.textContent = 'Settings reset to defaults';
  }
}

// Ollama hosts management functions
async function addOllamaHost() {
  const input = document.getElementById('newOllamaHostUrl');
  const hostUrl = input.value.trim();

  if (!hostUrl) {
    showStatusMessage('settingsStatusMessage', 'Please enter a host URL', true);
    return;
  }

  // Validate URL format
  try {
    new URL(hostUrl);
  } catch (_) {
    showStatusMessage('settingsStatusMessage', 'Invalid URL format', true);
    return;
  }

  try {
    // Test the connection first
    showStatusMessage('settingsStatusMessage', `Testing connection to ${hostUrl}...`);
    const testResult = await verifyOllamaHost(hostUrl);

    if (!testResult.success) {
      showStatusMessage('settingsStatusMessage', `Failed to connect to ${hostUrl}: ${testResult.error}`, true);
      return;
    }

    // Add to hosts list
    const data = await loadOllamaHosts();

    // Check if host already exists
    if (data.hosts.some(host => host.url === hostUrl)) {
      showStatusMessage('settingsStatusMessage', 'Host already exists', true);
      return;
    }

    // Extract host name for display
    const hostName = hostUrl.includes('://') ? hostUrl.split('://')[1].split(':')[0] : hostUrl.split(':')[0];
    const isLocal = hostName === 'localhost' || hostName === '127.0.0.1';

    const newHost = {
      url: hostUrl,
      name: hostName,
      is_local: isLocal
    };

    const updatedHosts = [...data.hosts, newHost];
    const activeHost = data.active_host || hostUrl; // Set as active if no current active host

    await saveOllamaHosts(updatedHosts, activeHost);

    showStatusMessage('settingsStatusMessage', `✓ Added ${hostUrl} successfully. Found ${testResult.models.length} models.`);
    input.value = ''; // Clear input

    // Refresh the UI
    await loadAndRenderOllamaHosts();

  } catch (err) {
    console.error('Error adding Ollama host:', err);
    showStatusMessage('settingsStatusMessage', 'Error adding host', true);
  }
}

async function refreshOllamaHostsFromEnvironment() {
  try {
    showStatusMessage('settingsStatusMessage', 'Refreshing hosts from environment variables...');

    // Force refresh by fetching from the backend which will re-read environment variables
    const response = await fetch('/api/settings/ollama/hosts', {
      method: 'GET',
      cache: 'no-cache'  // Ensure fresh data
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    const data = await response.json();

    if (data.success) {
      await loadAndRenderOllamaHosts();
      showStatusMessage('settingsStatusMessage', `Refreshed hosts from environment. Found ${data.hosts.length} hosts.`);
    } else {
      showStatusMessage('settingsStatusMessage', 'Failed to refresh hosts from environment', true);
    }
  } catch (err) {
    console.error('Error refreshing hosts from environment:', err);
    showStatusMessage('settingsStatusMessage', 'Error refreshing hosts from environment', true);
  }
}

async function loadOllamaModels() {
  try {
    showStatusMessage('settingsStatusMessage', 'Loading models from all Ollama hosts...');

    // Refresh the model dropdown with latest data
    await populateModelDropdown('globalModelSelect');

    showStatusMessage('settingsStatusMessage', 'Successfully loaded models from all hosts');
  } catch (err) {
    console.error('Error loading Ollama models:', err);
    showStatusMessage('settingsStatusMessage', 'Error loading models', true);
  }
}

// ---------------- Salient inheritance recompute admin actions ----------------
async function triggerSalientRecompute(body) {
  const statusEl = document.getElementById('salientRecomputeStatus');
  const resultEl = document.getElementById('salientRecomputeResult');
  const controls = ['salientDryRunButton', 'salientRecomputeButton', 'salientForceButton']
    .map((id) => document.getElementById(id))
    .filter((el) => Boolean(el));

  const formatDuration = (ms) => {
    const totalSeconds = Math.floor(ms / 1000);
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    if (minutes > 0) {
      return `${minutes}m ${seconds.toString().padStart(2, '0')}s`;
    }
    return `${seconds}s`;
  };

  let spinner;
  let statusText;
  let ticker;
  const startTime = Date.now();

  if (statusEl) {
    statusEl.classList.remove('hidden', 'success', 'error');
    statusEl.textContent = '';
    statusEl.className = 'status-message loading';
    statusEl.style.display = 'flex';

    spinner = document.createElement('span');
    spinner.className = 'loading-spinner';
    spinner.setAttribute('aria-hidden', 'true');

    statusText = document.createElement('span');
    statusText.textContent = 'Running...';

    statusEl.appendChild(spinner);
    statusEl.appendChild(statusText);

    const updateElapsed = () => {
      if (!statusText) {
        return;
      }
      const elapsed = Date.now() - startTime;
      statusText.textContent = `Running... ${formatDuration(elapsed)}`;
    };
    updateElapsed();
    ticker = window.setInterval(updateElapsed, 1000);
  }

  if (resultEl) {
    resultEl.classList.add('hidden');
    resultEl.textContent = '';
  }

  controls.forEach((btn) => {
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
  });

  try {
    // NOTE: Endpoint path updated to match blueprint prefix '/vontology/api/vontology'
    // Previous '/api/vontology/...' caused 404 after blueprint prefix change.
    const response = await fetch('/vontology/api/vontology/predicates/salient/recompute_inherited', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
    const payloadText = await response.text();
    let data = {};
    if (payloadText) {
      try {
        data = JSON.parse(payloadText);
      } catch (parseErr) {
        console.warn('Unable to parse recompute response as JSON', parseErr);
      }
    }
    if (!response.ok) {
      const msg = data.error || `HTTP ${response.status}`;
      throw new Error(msg);
    }

    if (statusEl) {
      statusEl.classList.remove('loading');
      statusEl.className = 'status-message ' + (data.success ? 'success' : 'error');
      statusEl.style.display = 'block';
      const duration = formatDuration(Date.now() - startTime);
      const processed = data.types_processed ?? data.types_total;
      const updated = data.types_updated ?? 0;
      const scopeUpdates = data.scope_updates ?? 0;
      const cycleFlag = data.cycles_detected ? ' - cycles detected' : '';
      const detail = [`processed ${processed ?? 'n/a'} types`, `updated ${updated}`, `scope updates ${scopeUpdates}`]
        .filter(Boolean)
        .join(', ');
      statusEl.textContent = data.success
        ? `Completed in ${duration} - ${detail}${cycleFlag}`
        : `Completed with issues in ${duration} - ${detail}${cycleFlag}`;
    }

    if (resultEl) {
      resultEl.textContent = JSON.stringify(data, null, 2);
      resultEl.classList.remove('hidden');
      resultEl.style.display = 'block';
    }
  } catch (err) {
    console.error('Salient recompute failed', err);
    if (statusEl) {
      statusEl.classList.remove('loading');
      statusEl.className = 'status-message error';
      statusEl.style.display = 'block';
      statusEl.textContent = 'Failed: ' + err.message;
    }
  } finally {
    if (ticker) {
      window.clearInterval(ticker);
    }
    controls.forEach((btn) => {
      btn.disabled = false;
      btn.removeAttribute('aria-busy');
    });
  }
}

async function shutdownServer() {
  const statusEl = document.getElementById('shutdownStatus');
  if (statusEl) { statusEl.classList.remove('hidden'); statusEl.textContent = 'Sending shutdown...'; statusEl.className = 'status-message'; }
  let token = document.getElementById('adminTokenInput')?.value.trim();
  if (!token) {
    // Attempt to fetch token from parent context (iframe can't read file). Optional future enhancement.
  }
  try {
    const res = await fetch('/admin/shutdown', {
      method: 'POST',
      headers: token ? { 'X-Admin-Token': token } : {}
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.success) {
      if (statusEl) { statusEl.textContent = 'Shutdown initiated. Server will stop shortly.'; statusEl.className = 'status-message success'; }
      setTimeout(() => { if (statusEl) statusEl.textContent += ' (Refresh to confirm)'; }, 1500);
    } else {
      throw new Error(data.error || ('HTTP ' + res.status));
    }
  } catch (e) {
    if (statusEl) { statusEl.textContent = 'Shutdown failed: ' + e.message; statusEl.className = 'status-message error'; }
  }
}
