const LS_OPENAI_SELECTED_MODEL = 'von:openaiSelectedModel';
const LS_OLLAMA_SELECTION = 'von:ollamaSelection';
const LS_PREMIUM_MODEL_USE_ENABLED = 'von:premiumModelUseEnabled';

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

export function hasLocalPremiumModelUsePreference() {
  try {
    return localStorage.getItem(LS_PREMIUM_MODEL_USE_ENABLED) !== null;
  } catch {
    return false;
  }
}

export function getLocalPremiumModelUseEnabled(defaultValue = false) {
  try {
    const stored = localStorage.getItem(LS_PREMIUM_MODEL_USE_ENABLED);
    if (stored === null) return !!defaultValue;
    return stored === 'true';
  } catch {
    return !!defaultValue;
  }
}

export function setLocalPremiumModelUseEnabled(enabled) {
  try {
    localStorage.setItem(LS_PREMIUM_MODEL_USE_ENABLED, enabled ? 'true' : 'false');
  } catch {
    // Ignore localStorage failures.
  }
}

export function getStoredOpenAiSelectedModel() {
  try {
    return String(localStorage.getItem(LS_OPENAI_SELECTED_MODEL) || '').trim();
  } catch {
    return '';
  }
}

export function setStoredOpenAiSelectedModel(modelName) {
  try {
    const trimmed = String(modelName || '').trim();
    if (trimmed) {
      localStorage.setItem(LS_OPENAI_SELECTED_MODEL, trimmed);
    } else {
      localStorage.removeItem(LS_OPENAI_SELECTED_MODEL);
    }
  } catch {
    // Ignore localStorage failures.
  }
}

export function getStoredOllamaSelection() {
  const stored = readStoredJson(LS_OLLAMA_SELECTION);
  if (!stored || typeof stored !== 'object') return null;

  const value = String(stored.value || '').trim();
  const model = String(stored.model || '').trim();
  const host = String(stored.host || '').trim();

  if (!value && !model) return null;

  return {
    value: value || null,
    model: model || null,
    host: host || null,
  };
}

export function setStoredOllamaSelection(selection) {
  if (!selection || typeof selection !== 'object') {
    writeStoredJson(LS_OLLAMA_SELECTION, null);
    return;
  }

  const value = String(selection.value || '').trim();
  const model = String(selection.model || '').trim();
  const host = String(selection.host || '').trim();

  if (!value && !model) {
    writeStoredJson(LS_OLLAMA_SELECTION, null);
    return;
  }

  writeStoredJson(LS_OLLAMA_SELECTION, {
    value: value || null,
    model: model || null,
    host: host || null,
  });
}

export function clearStoredOllamaSelection() {
  writeStoredJson(LS_OLLAMA_SELECTION, null);
}

function buildOllamaRequestedLlm(ollamaSelection) {
  if (!ollamaSelection?.model) return null;

  return {
    provider: 'ollama',
    model: ollamaSelection.model,
    host: ollamaSelection.host || null,
    requestModel: `ollama:${ollamaSelection.model}`,
  };
}

export function resolveLocalRequestedLlm() {
  if (!hasLocalPremiumModelUsePreference()) {
    // A persisted Ollama selection is itself an explicit local-model choice.
    // Older browser state may predate the machine-local premium toggle key.
    return buildOllamaRequestedLlm(getStoredOllamaSelection());
  }

  if (getLocalPremiumModelUseEnabled(false)) {
    const openaiModel = getStoredOpenAiSelectedModel();
    if (openaiModel) {
      return {
        provider: 'openai',
        model: openaiModel,
        requestModel: `openai:${openaiModel}`,
      };
    }
    return null;
  }

  return buildOllamaRequestedLlm(getStoredOllamaSelection());
}

export function applyLocalModelPreferenceOverlay(settings) {
  const localOverride = resolveLocalRequestedLlm();
  if (!settings || !localOverride) return settings;

  return {
    ...settings,
    active_llm: {
      provider: localOverride.provider,
      model: localOverride.model,
      ...(localOverride.host ? { host: localOverride.host } : {}),
    },
  };
}
