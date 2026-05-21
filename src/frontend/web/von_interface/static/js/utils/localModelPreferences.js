const LS_LOCAL_MODEL_PREFERENCE = 'von:localModelPreference';
const LS_OPENAI_SELECTED_MODEL = 'von:openaiSelectedModel';
const LS_OLLAMA_SELECTION = 'von:ollamaSelection';
const LS_PREMIUM_MODEL_USE_ENABLED = 'von:premiumModelUseEnabled';
const LOCAL_MODEL_PREFERENCE_SCHEMA = 'localModelPreference.v1';
const LOCAL_MODEL_UNAVAILABLE_REASON_NO_OLLAMA_MODEL = 'premium_disabled_no_ollama_model';

function readStoredJson(key) {
  try {
    return JSON.parse(localStorage.getItem(key) || 'null');
  } catch {
    return null;
  }
}

function writeStoredJson(key, value) {
  try {
    if (value == null) {
      localStorage.removeItem(key);
      return;
    }
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Ignore localStorage failures.
  }
}

function readStoredString(key) {
  try {
    return String(localStorage.getItem(key) || '').trim();
  } catch {
    return '';
  }
}

function normaliseActiveSource(value) {
  return value === 'openai' || value === 'ollama' ? value : null;
}

function normaliseModelName(value) {
  const trimmed = String(value || '').trim();
  return trimmed || null;
}

function normaliseOllamaSelection(selection) {
  if (!selection || typeof selection !== 'object') return null;

  const value = normaliseModelName(selection.value);
  const model = normaliseModelName(selection.model);
  const host = normaliseModelName(selection.host);

  if (!value && !model) return null;

  return {
    value,
    model,
    host,
  };
}

function buildCanonicalLocalModelPreference({
  activeSource = null,
  openaiModel = null,
  ollamaSelection = null,
} = {}) {
  const canonical = {
    schemaVersion: LOCAL_MODEL_PREFERENCE_SCHEMA,
    activeSource: normaliseActiveSource(activeSource),
    openaiModel: normaliseModelName(openaiModel),
    ollamaSelection: normaliseOllamaSelection(ollamaSelection),
  };

  if (!canonical.activeSource && !canonical.openaiModel && !canonical.ollamaSelection) {
    return null;
  }

  return canonical;
}

function cloneLocalModelPreference(preference) {
  if (!preference) return null;
  return {
    schemaVersion: preference.schemaVersion || LOCAL_MODEL_PREFERENCE_SCHEMA,
    activeSource: preference.activeSource || null,
    openaiModel: preference.openaiModel || null,
    ollamaSelection: preference.ollamaSelection
      ? { ...preference.ollamaSelection }
      : null,
  };
}

function normaliseStoredLocalModelPreference(stored) {
  if (!stored || typeof stored !== 'object') return null;

  return buildCanonicalLocalModelPreference({
    activeSource: stored.activeSource ?? stored.active_source ?? null,
    openaiModel: stored.openaiModel ?? stored.openai_model ?? null,
    ollamaSelection: stored.ollamaSelection ?? stored.ollama_selection ?? null,
  });
}

function readLegacyLocalModelPreference() {
  const openaiModel = normaliseModelName(readStoredString(LS_OPENAI_SELECTED_MODEL));
  const ollamaSelection = normaliseOllamaSelection(readStoredJson(LS_OLLAMA_SELECTION));

  let activeSource = null;
  try {
    const stored = localStorage.getItem(LS_PREMIUM_MODEL_USE_ENABLED);
    if (stored === 'true') activeSource = 'openai';
    if (stored === 'false') activeSource = 'ollama';
  } catch {
    activeSource = null;
  }

  if (!activeSource && ollamaSelection) {
    activeSource = 'ollama';
  }

  return buildCanonicalLocalModelPreference({
    activeSource,
    openaiModel,
    ollamaSelection,
  });
}

function mirrorLegacyKeys(preference) {
  try {
    const openaiModel = preference?.openaiModel || null;
    if (openaiModel) {
      localStorage.setItem(LS_OPENAI_SELECTED_MODEL, openaiModel);
    } else {
      localStorage.removeItem(LS_OPENAI_SELECTED_MODEL);
    }

    if (preference?.ollamaSelection) {
      localStorage.setItem(
        LS_OLLAMA_SELECTION,
        JSON.stringify(preference.ollamaSelection),
      );
    } else {
      localStorage.removeItem(LS_OLLAMA_SELECTION);
    }

    if (preference?.activeSource === 'openai') {
      localStorage.setItem(LS_PREMIUM_MODEL_USE_ENABLED, 'true');
    } else if (preference?.activeSource === 'ollama') {
      localStorage.setItem(LS_PREMIUM_MODEL_USE_ENABLED, 'false');
    } else {
      localStorage.removeItem(LS_PREMIUM_MODEL_USE_ENABLED);
    }
  } catch {
    // Ignore localStorage failures.
  }
}

function persistLocalModelPreference(preference) {
  const canonical = buildCanonicalLocalModelPreference(preference || {});
  writeStoredJson(LS_LOCAL_MODEL_PREFERENCE, canonical);
  mirrorLegacyKeys(canonical);
  return canonical;
}

function buildOpenAiRequestedLlm(modelName) {
  const model = normaliseModelName(modelName);
  if (!model) return null;

  return {
    provider: 'openai',
    model,
    requestModel: `openai:${model}`,
  };
}

function buildOllamaRequestedLlm(ollamaSelection) {
  const selection = normaliseOllamaSelection(ollamaSelection);
  if (!selection?.model) return null;

  return {
    provider: 'ollama',
    model: selection.model,
    host: selection.host || null,
    requestModel: `ollama:${selection.model}`,
  };
}

function buildEffectiveLocalModelPreference(preference) {
  const canonical = cloneLocalModelPreference(preference)
    || buildCanonicalLocalModelPreference();

  if (!canonical) {
    return {
      schemaVersion: LOCAL_MODEL_PREFERENCE_SCHEMA,
      activeSource: null,
      openaiModel: null,
      ollamaSelection: null,
      requestedLlm: null,
      modelUnavailable: false,
      modelUnavailableReason: null,
    };
  }

  let requestedLlm = null;
  if (canonical.activeSource === 'openai') {
    requestedLlm = buildOpenAiRequestedLlm(canonical.openaiModel);
  } else if (canonical.activeSource === 'ollama') {
    requestedLlm = buildOllamaRequestedLlm(canonical.ollamaSelection);
  }

  const modelUnavailableReason = canonical.activeSource === 'ollama' && !requestedLlm
    ? LOCAL_MODEL_UNAVAILABLE_REASON_NO_OLLAMA_MODEL
    : null;

  return {
    ...canonical,
    requestedLlm,
    modelUnavailable: !!modelUnavailableReason,
    modelUnavailableReason,
  };
}

export function getStoredLocalModelPreference() {
  const stored = normaliseStoredLocalModelPreference(
    readStoredJson(LS_LOCAL_MODEL_PREFERENCE),
  );
  if (stored) {
    return stored;
  }

  const migrated = readLegacyLocalModelPreference();
  if (!migrated) {
    return null;
  }

  persistLocalModelPreference(migrated);
  return migrated;
}

export function getEffectiveLocalModelPreference() {
  return buildEffectiveLocalModelPreference(getStoredLocalModelPreference());
}

export function hasLocalPremiumModelUsePreference() {
  return getEffectiveLocalModelPreference().activeSource === 'openai';
}

export function getLocalPremiumModelUseEnabled(defaultValue = false) {
  const effective = getEffectiveLocalModelPreference();
  if (!effective.activeSource) return !!defaultValue;
  return effective.activeSource === 'openai';
}

export function setLocalPremiumModelUseEnabled(enabled) {
  const stored = getStoredLocalModelPreference();
  const next = cloneLocalModelPreference(stored) || {
    schemaVersion: LOCAL_MODEL_PREFERENCE_SCHEMA,
    activeSource: null,
    openaiModel: null,
    ollamaSelection: null,
  };
  next.activeSource = enabled ? 'openai' : 'ollama';
  persistLocalModelPreference(next);
}

export function getStoredOpenAiSelectedModel() {
  return getEffectiveLocalModelPreference().openaiModel || '';
}

export function setStoredOpenAiSelectedModel(modelName) {
  const stored = getStoredLocalModelPreference();
  const next = cloneLocalModelPreference(stored) || {
    schemaVersion: LOCAL_MODEL_PREFERENCE_SCHEMA,
    activeSource: null,
    openaiModel: null,
    ollamaSelection: null,
  };
  next.openaiModel = normaliseModelName(modelName);
  persistLocalModelPreference(next);
}

export function getStoredOllamaSelection() {
  return getEffectiveLocalModelPreference().ollamaSelection || null;
}

export function setStoredOllamaSelection(selection) {
  const stored = getStoredLocalModelPreference();
  const next = cloneLocalModelPreference(stored) || {
    schemaVersion: LOCAL_MODEL_PREFERENCE_SCHEMA,
    activeSource: null,
    openaiModel: null,
    ollamaSelection: null,
  };
  next.ollamaSelection = normaliseOllamaSelection(selection);
  if (next.ollamaSelection && !next.activeSource) {
    next.activeSource = 'ollama';
  }
  persistLocalModelPreference(next);
}

export function clearStoredOllamaSelection() {
  const stored = getStoredLocalModelPreference();
  if (!stored) return;

  const next = cloneLocalModelPreference(stored);
  next.ollamaSelection = null;
  persistLocalModelPreference(next);
}

export function resolveLocalRequestedLlm() {
  return getEffectiveLocalModelPreference().requestedLlm;
}

export function applyLocalModelPreferenceOverlay(settings) {
  const localPreference = getEffectiveLocalModelPreference();
  if (!settings) return settings;
  if (localPreference.modelUnavailable) {
    return {
      ...settings,
      active_llm: null,
      local_model_unavailable_reason: localPreference.modelUnavailableReason,
    };
  }

  const localOverride = localPreference.requestedLlm;
  if (!localOverride) return settings;

  return {
    ...settings,
    active_llm: {
      provider: localOverride.provider,
      model: localOverride.model,
      ...(localOverride.host ? { host: localOverride.host } : {}),
    },
  };
}
