import { postJson } from './apiService.js';
import { renderOrgSelector, setupOrgSwitchListener } from './components/orgSelector.js';
import { populateLanguageSelect } from './languageConfig.js';
import {
  loadAndRenderOllamaHosts,
  loadOllamaHosts,
  populateModelDropdown,
  populateOpenAIModelDropdown,
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
const LS_STT_LANGUAGE = 'chatSttLanguage';
const LS_STT_CONTINUOUS = 'chatSttContinuous';
const LS_STT_INTERIM_RESULTS = 'chatSttInterimResults';
const RUNTIME_REFRESH_MS = 12000;

let runtimeIntervalId = null;
let runtimeAbortController = null;

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

  const sttLanguage = normaliseLanguageSetting(safeLocalStorageGet(LS_STT_LANGUAGE)) || preferredLanguage;
  const sttContinuous = parseBoolSetting(safeLocalStorageGet(LS_STT_CONTINUOUS), true);
  const sttInterimResults = parseBoolSetting(safeLocalStorageGet(LS_STT_INTERIM_RESULTS), true);

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
    if (ttsPreviewButton) {
      ttsPreviewButton.disabled = true;
      ttsPreviewButton.title = 'Text-to-speech is not supported in this browser.';
    }
  }

  if (!sttSupported) {
    if (sttLanguageInput) sttLanguageInput.disabled = true;
    if (sttContinuousToggle) sttContinuousToggle.disabled = true;
    if (sttInterimToggle) sttInterimToggle.disabled = true;
  }

  if (ttsVoiceSelect) {
    ttsVoiceSelect.addEventListener('change', (e) => {
      safeLocalStorageSet(LS_TTS_VOICE_URI, String(e.target.value || ''));
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
      safeLocalStorageSet(LS_TTS_LANGUAGE, normaliseLanguageSetting(e.target.value));
    });
  }
  if (ttsRateRange) {
    ttsRateRange.addEventListener('input', (e) => {
      const value = clampNumber(e.target.value, 0.5, 2, 1);
      safeLocalStorageSet(LS_TTS_RATE, String(value));
      if (ttsRateValue) ttsRateValue.textContent = String(value.toFixed(1));
    });
  }
  if (ttsPitchRange) {
    ttsPitchRange.addEventListener('input', (e) => {
      const value = clampNumber(e.target.value, 0, 2, 1);
      safeLocalStorageSet(LS_TTS_PITCH, String(value));
      if (ttsPitchValue) ttsPitchValue.textContent = String(value.toFixed(1));
    });
  }
  if (ttsVolumeRange) {
    ttsVolumeRange.addEventListener('input', (e) => {
      const value = clampNumber(e.target.value, 0, 1, 1);
      safeLocalStorageSet(LS_TTS_VOLUME, String(value));
      if (ttsVolumeValue) ttsVolumeValue.textContent = String(value.toFixed(2));
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

function renderRagSummary(ragData, pendingFallback) {
  const ragSummaryEl = document.getElementById('settingsRagSummary');
  const ragHintEl = document.getElementById('settingsRagHint');
  if (!ragSummaryEl) return;

  const pending = typeof pendingFallback === 'number' ? pendingFallback : null;
  if (!ragData) {
    if (pending === null || pending < 0) {
      ragSummaryEl.textContent = 'Unknown';
      if (ragHintEl) ragHintEl.textContent = 'RAG status unavailable';
    } else if (pending === 0) {
      ragSummaryEl.textContent = 'Idle';
      if (ragHintEl) ragHintEl.textContent = 'No pending items to index';
    } else {
      ragSummaryEl.textContent = `${pending} pending`;
      if (ragHintEl) ragHintEl.textContent = 'Pending items waiting for indexing';
    }
    return;
  }

  const { indexed = 0, pending: pendingCount = 0, failed = 0, skipped = 0 } = ragData;
  if (pendingCount === 0) {
    ragSummaryEl.textContent = `Indexed ${indexed}`;
    if (ragHintEl) ragHintEl.textContent = `Indexed=${indexed} • Failed=${failed} • Skipped=${skipped}`;
  } else {
    ragSummaryEl.textContent = `Indexed ${indexed} • ${pendingCount} pending`;
    if (ragHintEl) ragHintEl.textContent = `Indexed=${indexed} • Pending=${pendingCount} • Failed=${failed} • Skipped=${skipped}`;
  }
}

async function loadRagStatus(pendingFallback) {
  const ns = (localStorage.getItem('von_namespace') || localStorage.getItem('current_user_namespace')) || '';
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

async function loadRuntimeStatus(manualRefresh = false) {
  const localEl = document.getElementById('settingsLocalIpValue');
  const publicEl = document.getElementById('settingsPublicIpValue');
  const pidEl = document.getElementById('settingsPidValue');
  const uptimeEl = document.getElementById('settingsUptimeValue');
  const refreshBtn = document.getElementById('refreshRuntimeButton');

  if (refreshBtn && manualRefresh) {
    refreshBtn.disabled = true;
    refreshBtn.textContent = 'Refreshing…';
  }

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
});

function getStoredJson(key) {
  try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch { return null; }
}
function setStoredJson(key, value) {
  try { if (value == null) localStorage.removeItem(key); else localStorage.setItem(key, JSON.stringify(value)); } catch { }
}

function applyStoredSelection(selectId, stored, fallbackSelected = true) {
  const sel = document.getElementById(selectId);
  if (!sel) return null;
  if (stored && (stored.id || stored.concept_id)) {
    for (const opt of sel.options) {
      if (opt.dataset) {
        if ((stored.id && opt.dataset.id === String(stored.id)) || (stored.concept_id && opt.dataset.conceptId === stored.concept_id)) {
          opt.selected = true;
          return stored;
        }
      }
    }
  }
  // If nothing selected & we want fallback, persist current selection for future sessions
  if (fallbackSelected) {
    const current = sel.selectedOptions?.[0];
    if (current && (current.dataset?.id || current.dataset?.conceptId)) {
      const storedVal = {
        id: current.dataset.id || null,
        concept_id: current.dataset.conceptId || null,
        name: current.textContent || null
      };
      setStoredJson(selectId === 'currentUserSelect' ? LS_USER_KEY : LS_ORG_KEY, storedVal);
      return storedVal;
    }
  }
  return null;
}

document.addEventListener('DOMContentLoaded', async () => {
  setupRuntimeSection();
  // Initialize all settings sections
  await loadAndDisplaySettings();
  setupSpeechSettingsSection();
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
    saveAllSettings('ollama');
  });
  document.getElementById('openaiModelSelect')?.addEventListener('change', () => saveAllSettings('openai'));
  document.getElementById('currentUserSelect')?.addEventListener('change', () => {
    const sel = document.getElementById('currentUserSelect');
    const opt = sel?.selectedOptions?.[0];
    if (opt) {
      setStoredJson(LS_USER_KEY, { id: opt.dataset.id || null, concept_id: opt.dataset.conceptId || null, name: opt.textContent || null });
      if (window.parent?.updateModelInfoFooterDisplay) { window.parent.updateModelInfoFooterDisplay(); }
      // When user changes, attempt to load stored server-side prefs (language/org)
      if (opt.dataset.conceptId) {
        loadUserConceptPreferences(opt.dataset.conceptId);
      }
    } else { setStoredJson(LS_USER_KEY, null); }
  });
  document.getElementById('currentOrganisationSelect')?.addEventListener('change', () => {
    const sel = document.getElementById('currentOrganisationSelect');
    const opt = sel?.selectedOptions?.[0];
    if (opt) {
      setStoredJson(LS_ORG_KEY, { id: opt.dataset.id || null, concept_id: opt.dataset.conceptId || null, name: opt.textContent || null });
      if (window.parent?.updateModelInfoFooterDisplay) { window.parent.updateModelInfoFooterDisplay(); }
      // Persist organisation preference (and language if set)
      persistCurrentUserPreferences();
    } else { setStoredJson(LS_ORG_KEY, null); }
  });
  document.getElementById('preferredLanguageSelect')?.addEventListener('change', () => {
    const val = document.getElementById('preferredLanguageSelect')?.value;
    if (val) localStorage.setItem(LS_LANG_KEY, val); else localStorage.removeItem(LS_LANG_KEY);
    // Emit event so footer or other UI can react
    if (window.parent) { window.parent.document.dispatchEvent(new CustomEvent('von:settingsChanged')); }
    // Persist language preference (with current organisation if available)
    persistCurrentUserPreferences();
  });

  const gmailProfileInput = document.getElementById('gmailProfileInput');
  if (gmailProfileInput) {
    try { gmailProfileInput.value = localStorage.getItem(LS_GMAIL_PROFILE) || ''; } catch { gmailProfileInput.value = ''; }
    updateGmailProfileStatus();
    gmailProfileInput.addEventListener('input', () => {
      try {
        const val = gmailProfileInput.value.trim();
        if (val) localStorage.setItem(LS_GMAIL_PROFILE, val); else localStorage.removeItem(LS_GMAIL_PROFILE);
        updateGmailProfileStatus();
      } catch { /* ignore localStorage errors */ }
    });
  }
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
    // Reset selects visually
    const userSel = document.getElementById('currentUserSelect'); if (userSel) userSel.selectedIndex = 0;
    const orgSel = document.getElementById('currentOrganisationSelect'); if (orgSel) orgSel.selectedIndex = 0;
    const langSel = document.getElementById('preferredLanguageSelect'); if (langSel) langSel.value = 'en-NZ';
    const gmailProfileInput = document.getElementById('gmailProfileInput'); if (gmailProfileInput) gmailProfileInput.value = '';
    if (window.parent?.updateModelInfoFooterDisplay) { window.parent.updateModelInfoFooterDisplay(); }
    showStatusMessage('settingsStatusMessage', 'Local preferences cleared');
  } catch (e) {
    console.warn('Failed to reset local prefs', e);
    showStatusMessage('settingsStatusMessage', 'Failed to reset local preferences', true);
  }
});

async function loadAndDisplaySettings() {
  try {
    const response = await fetch('/api/settings/');
    if (!response.ok) throw new Error(`Failed to fetch settings: ${response.statusText}`);
    const settings = await response.json();

    // Extract current model information from active_llm setting
    let currentOllamaModel = null;
    let currentOpenAIModel = null;

    if (settings.active_llm) {
      if (settings.active_llm.provider === 'ollama') {
        currentOllamaModel = settings.active_llm.model;
      } else if (settings.active_llm.provider === 'openai') {
        currentOpenAIModel = settings.active_llm.model;
      }
    }

    // Load and display Ollama hosts first
    await loadAndRenderOllamaHosts();

    // Populate Ollama models and select the saved one
    await populateModelDropdown('globalModelSelect', currentOllamaModel);

    // Try to populate OpenAI models directly (without verification, if API key is already configured)
    if (currentOpenAIModel) {
      try {
        await populateOpenAIModelDropdown('openaiModelSelect', currentOpenAIModel);
        const oac = document.getElementById('openaiModelsContainer');
        if (oac) { oac.style.display = 'block'; oac.classList.remove('hidden'); }
      } catch (error) {
        console.log('Could not directly load OpenAI models, will need verification:', error.message);
      }
    }

    // Track if we should persist a backfilled concept_id
    let shouldPersistBackfill = false;

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
      // If settings lacks concept_id but the selected option has one, mark for backfill
      if (userSelect && !targetCid) {
        const sel = userSelect.selectedOptions?.[0];
        if (sel?.dataset?.conceptId) {
          shouldPersistBackfill = true;
        }
      }
    } catch { }
    // Override with stored local selection if present
    applyStoredSelection('currentUserSelect', getStoredJson(LS_USER_KEY));

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
      // If settings lacks concept_id but the selected option has one, mark for backfill
      if (orgSelect && !targetCid) {
        const sel = orgSelect.selectedOptions?.[0];
        if (sel?.dataset?.conceptId) {
          shouldPersistBackfill = true;
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
        // TODO: Trigger RAG namespace update when org switches
      });
    } catch (error) {
      console.error('Error initializing organisation selector:', error);
    }

    // Populate OpenAI settings
    const envVarInput = document.getElementById('openaiApiKeyEnvVar');
    if (envVarInput && settings.openai_api_key_env_var) {
      envVarInput.value = settings.openai_api_key_env_var;
    }

    // Populate fetch_counts_on_load toggle
    try {
      const countsToggleEl = document.getElementById('fetchCountsOnLoadToggle');
      if (countsToggleEl) {
        const flag = Object.prototype.hasOwnProperty.call(settings, 'fetch_counts_on_load') ? !!settings.fetch_counts_on_load : true;
        countsToggleEl.checked = !!flag;
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

    // If we identified missing concept_ids but selections have them, persist once
    // We no longer persist user/org/language backfills to the server; they are client-side only now.
    try {
      if (shouldPersistBackfill) {
        // Save only active_llm & api key (exclude user/org/language) if needed
        await saveAllSettings();
      }
    } catch (e) { console.warn('Save after backfill failed (non-critical):', e); }

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

async function saveAllSettings(changedProvider = null) {
  const ollamaModelSelect = document.getElementById('globalModelSelect');
  const openaiModelSelect = document.getElementById('openaiModelSelect');

  let activeLlm = null;

  if (changedProvider === 'openai') {
    activeLlm = { provider: 'openai', model: openaiModelSelect.value };
    // Clear the other dropdown to avoid confusion
    ollamaModelSelect.value = '';
  } else if (changedProvider === 'ollama') {
    // Extract both the model name and host URL from the selected option
    const selectedOption = ollamaModelSelect.selectedOptions[0];
    const modelName = selectedOption ? selectedOption.dataset.modelName : ollamaModelSelect.value;
    const hostUrl = selectedOption ? selectedOption.dataset.hostUrl : null;
    activeLlm = { provider: 'ollama', model: modelName, host: hostUrl };
    // Clear the other dropdown
    openaiModelSelect.value = '';
  } else {
    // If no specific provider changed (e.g., user change), determine the active one
    const openaiModel = openaiModelSelect?.value;
    const ollamaModel = ollamaModelSelect?.value;
    if (openaiModel) {
      activeLlm = { provider: 'openai', model: openaiModel };
    } else if (ollamaModel) {
      // Extract both the model name and host URL from the selected option
      const selectedOption = ollamaModelSelect.selectedOptions[0];
      const modelName = selectedOption ? selectedOption.dataset.modelName : ollamaModel;
      const hostUrl = selectedOption ? selectedOption.dataset.hostUrl : null;
      activeLlm = { provider: 'ollama', model: modelName, host: hostUrl };
    }
  }

  // We now persist user/org/language only in localStorage; do not send to backend
  const settings = {
    active_llm: activeLlm,
    openai_api_key_env_var: document.getElementById('openaiApiKeyEnvVar')?.value,
    fetch_counts_on_load: !!document.getElementById('fetchCountsOnLoadToggle')?.checked,
    disable_remote_ollama_scan: !!document.getElementById('disableRemoteOllamaScanToggle')?.checked,
  };

  try {
    const response = await postJson('/api/settings/', settings);
    showStatusMessage('settingsStatusMessage', response.message || 'Settings saved successfully!');

    // Update parent window footer
    if (window.parent?.updateModelInfoFooterDisplay) {
      window.parent.updateModelInfoFooterDisplay();
    }

    // Emit event to update language indicator in footer
    if (window.parent) {
      window.parent.document.dispatchEvent(new CustomEvent('von:settingsChanged'));
    }
  } catch (error) {
    console.error('Error saving settings:', error);
    showStatusMessage('settingsStatusMessage', 'Failed to save settings.', true);
  }
}

// --- User concept preference helpers (server-side stored) ---

async function loadUserConceptPreferences(userConceptId) {
  try {
    const resp = await fetch(`/api/settings/user_prefs/${encodeURIComponent(userConceptId)}`);
    if (!resp.ok) return;
    const data = await resp.json();
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
          setStoredJson(LS_ORG_KEY, { id: selOpt.dataset.id || null, concept_id: selOpt.dataset.conceptId || null, name: selOpt.textContent || null });
        }
      }
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

export async function checkOpenAiEnvVar() {
  const apiKeyEnvVar = document.getElementById('openaiApiKeyEnvVar').value;
  const verifyButton = document.getElementById('verifyOpenAiApiKeyButton');
  const statusMessage = document.getElementById('openaiStatusMessage');

  try {
    const response = await postJson('/api/settings/env_var/check', { env_var_name: apiKeyEnvVar });
    if (response.exists) {
      verifyButton.style.backgroundColor = '#28a745'; // Green
      verifyButton.style.display = 'inline-block';
      verifyButton.disabled = false;
      statusMessage.textContent = `Key found: ${response.masked_value}`;
      statusMessage.className = 'status-message success';
      statusMessage.style.display = 'block';
      return true;
    } else {
      verifyButton.style.display = 'none';
      verifyButton.disabled = true;
      statusMessage.textContent = 'No key found for this environment variable.';
      statusMessage.className = 'status-message error';
      statusMessage.style.display = 'block';
      return false;
    }
  } catch (error) {
    console.error('Error checking environment variable:', error);
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
  const modelSelect = document.getElementById('openaiModelSelect');

  statusMessage.style.display = 'block';
  statusMessage.textContent = 'Verifying API key...';
  statusMessage.className = 'status-message';

  try {
    const response = await postJson('/api/settings/openai/verify', { api_key_env_var: apiKeyEnvVar });

    if (response.success) {
      statusMessage.textContent = 'API key is valid. Models loaded.';
      statusMessage.classList.add('success');
      modelsContainer.style.display = 'block';
      modelsContainer.classList.remove('hidden');

      modelSelect.innerHTML = '<option value="">Select an OpenAI Model</option>';
      response.models.forEach(model => {
        const option = document.createElement('option');
        option.value = model;
        option.textContent = model;
        modelSelect.appendChild(option);
      });

      // Select the saved model after populating the dropdown
      if (savedModel) {
        modelSelect.value = savedModel;
      }
    } else {
      throw new Error(response.error || 'Failed to verify API key.');
    }
  } catch (error) {
    // As a fallback, try the direct OpenAI models endpoint
    try {
      await populateOpenAIModelDropdown('openaiModelSelect', savedModel);
      statusMessage.textContent = 'Models loaded from cache. Please verify your API key for latest models.';
      statusMessage.classList.add('success');
      modelsContainer.style.display = 'block';
      modelsContainer.classList.remove('hidden');
    } catch (fallbackError) {
      statusMessage.textContent = `Error: ${error.message}`;
      statusMessage.classList.add('error');
      modelsContainer.style.display = 'none';
      modelsContainer.classList.add('hidden');
    }
  }
}

// Remove the old, separate save functions (saveGlobalModel, saveCurrentUser, saveOpenAISettings)
// as they are now replaced by the single saveAllSettings function.

export async function loadSettings() {
  try {
    const response = await fetch('/api/settings');
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
    const response = await fetch('/api/settings', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
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
  } catch (e) {
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
