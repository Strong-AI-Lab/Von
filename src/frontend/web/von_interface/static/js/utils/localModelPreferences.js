const LS_LOCAL_MODEL_PREFERENCE = 'von:localModelPreference';
const LS_OPENAI_SELECTED_MODEL = 'von:openaiSelectedModel';
const LS_OPENAI_MODEL_PARAMETERS = 'von:openaiModelParameters';
const LS_OLLAMA_SELECTION = 'von:ollamaSelection';
const LS_PREMIUM_MODEL_USE_ENABLED = 'von:premiumModelUseEnabled';
const LOCAL_MODEL_PREFERENCE_SCHEMA = 'localModelPreference.v1';
const LOCAL_MODEL_UNAVAILABLE_REASON_NO_OLLAMA_MODEL = 'premium_disabled_no_ollama_model';
const LOCAL_MODEL_UNAVAILABLE_REASON_NO_PREMIUM_MODEL = 'premium_enabled_no_model';
const PREMIUM_MODEL_PROVIDERS = new Set(['openai', 'openrouter', 'gemini']);

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
  return PREMIUM_MODEL_PROVIDERS.has(value) || value === 'ollama' ? value : null;
}

function normalisePremiumProvider(value) {
  const provider = typeof value === 'string' ? value.trim().toLowerCase() : '';
  return PREMIUM_MODEL_PROVIDERS.has(provider) ? provider : null;
}

function isObjectStringArtifact(value) {
  return /^\[object\s+[^\]]+\](?:\s|$)/i.test(value);
}

export function normaliseLocalModelName(value) {
  if (value == null) return null;

  if (typeof value !== 'string') {
    if (typeof value === 'number' || typeof value === 'boolean') {
      value = String(value);
    } else {
      return null;
    }
  }

  const trimmed = value.trim();
  if (isObjectStringArtifact(trimmed)) return null;
  return trimmed || null;
}

function normaliseOllamaSelection(selection) {
  if (!selection || typeof selection !== 'object') return null;

  const value = normaliseLocalModelName(selection.value);
  const model = normaliseLocalModelName(selection.model);
  const host = normaliseLocalModelName(selection.host);

  if (!value && !model) return null;

  return {
    value,
    model,
    host,
  };
}

function allowedReasoningEffortValues(capability = null) {
  const values = capability?.parameters?.reasoning_effort?.allowed_values;
  if (!Array.isArray(values) || !values.length) return null;
  const tokens = values
    .map((item) => String(item || '').trim().toLowerCase())
    .filter(Boolean);
  return tokens.length ? new Set(tokens) : null;
}

export function normaliseModelParameters(value, capability = null) {
  if (!value || typeof value !== 'object') return null;
  const params = {};
  let effort = value.reasoning_effort ?? value.reasoningEffort ?? null;
  if (effort == null && value.reasoning && typeof value.reasoning === 'object') {
    effort = value.reasoning.effort ?? null;
  }
  if (typeof effort === 'string') {
    const token = effort.trim().toLowerCase();
    const allowedValues = allowedReasoningEffortValues(capability);
    if (token && (!allowedValues || allowedValues.has(token))) {
      params.reasoning_effort = token;
    }
  }
  return Object.keys(params).length ? params : null;
}

function buildCanonicalLocalModelPreference({
  activeSource = null,
  premiumProvider = null,
  openaiModel = null,
  openaiModelParameters = null,
  geminiModel = null,
  geminiModelParameters = null,
  openrouterModel = null,
  openrouterModelParameters = null,
  ollamaSelection = null,
} = {}) {
  const normalisedActiveSource = normaliseActiveSource(activeSource);
  const normalisedPremiumProvider = normalisePremiumProvider(premiumProvider)
    || (PREMIUM_MODEL_PROVIDERS.has(normalisedActiveSource) ? normalisedActiveSource : null);
  const normalisedOpenAiParameters = normaliseModelParameters(openaiModelParameters);
  const normalisedGeminiParameters = normaliseModelParameters(geminiModelParameters);
  const normalisedOpenRouterParameters = normaliseModelParameters(openrouterModelParameters);
  const canonical = {
    schemaVersion: LOCAL_MODEL_PREFERENCE_SCHEMA,
    activeSource: normalisedActiveSource,
    openaiModel: normaliseLocalModelName(openaiModel),
    ollamaSelection: normaliseOllamaSelection(ollamaSelection),
  };
  // OpenAI remains the backwards-compatible default premium provider. Persist
  // the provider only when a different provider must survive an Ollama switch.
  if (normalisedPremiumProvider && normalisedPremiumProvider !== 'openai') {
    canonical.premiumProvider = normalisedPremiumProvider;
  }
  const normalisedGeminiModel = normaliseLocalModelName(geminiModel);
  if (normalisedGeminiModel) {
    canonical.geminiModel = normalisedGeminiModel;
  }
  if (normalisedOpenAiParameters) {
    canonical.openaiModelParameters = normalisedOpenAiParameters;
  }
  if (normalisedGeminiParameters) {
    canonical.geminiModelParameters = normalisedGeminiParameters;
  }
  const normalisedOpenRouterModel = normaliseLocalModelName(openrouterModel);
  if (normalisedOpenRouterModel) {
    canonical.openrouterModel = normalisedOpenRouterModel;
  }
  if (normalisedOpenRouterParameters) {
    canonical.openrouterModelParameters = normalisedOpenRouterParameters;
  }

  if (
    !canonical.activeSource
    && (!normalisedPremiumProvider || normalisedPremiumProvider === 'openai')
    && !canonical.openaiModel
    && !canonical.geminiModel
    && !canonical.openrouterModel
    && !canonical.ollamaSelection
  ) {
    return null;
  }

  return canonical;
}

function cloneLocalModelPreference(preference) {
  if (!preference) return null;
  return {
    schemaVersion: preference.schemaVersion || LOCAL_MODEL_PREFERENCE_SCHEMA,
    activeSource: preference.activeSource || null,
    ...(normalisePremiumProvider(preference.premiumProvider)
      && normalisePremiumProvider(preference.premiumProvider) !== 'openai'
      ? { premiumProvider: normalisePremiumProvider(preference.premiumProvider) }
      : {}),
    openaiModel: normaliseLocalModelName(preference.openaiModel),
    ...(normaliseModelParameters(preference.openaiModelParameters)
      ? { openaiModelParameters: normaliseModelParameters(preference.openaiModelParameters) }
      : {}),
    ...(normaliseLocalModelName(preference.geminiModel)
      ? { geminiModel: normaliseLocalModelName(preference.geminiModel) }
      : {}),
    ...(normaliseModelParameters(preference.geminiModelParameters)
      ? { geminiModelParameters: normaliseModelParameters(preference.geminiModelParameters) }
      : {}),
    ...(normaliseLocalModelName(preference.openrouterModel)
      ? { openrouterModel: normaliseLocalModelName(preference.openrouterModel) }
      : {}),
    ...(normaliseModelParameters(preference.openrouterModelParameters)
      ? { openrouterModelParameters: normaliseModelParameters(preference.openrouterModelParameters) }
      : {}),
    ollamaSelection: normaliseOllamaSelection(preference.ollamaSelection),
  };
}

function normaliseStoredLocalModelPreference(stored) {
  if (!stored || typeof stored !== 'object') return null;

  return buildCanonicalLocalModelPreference({
    activeSource: stored.activeSource ?? stored.active_source ?? null,
    premiumProvider: stored.premiumProvider ?? stored.premium_provider ?? null,
    openaiModel: stored.openaiModel ?? stored.openai_model ?? null,
    openaiModelParameters: (
      stored.openaiModelParameters
      ?? stored.openai_model_parameters
      ?? stored.model_parameters
      ?? null
    ),
    geminiModel: stored.geminiModel ?? stored.gemini_model ?? null,
    geminiModelParameters: (
      stored.geminiModelParameters
      ?? stored.gemini_model_parameters
      ?? null
    ),
    openrouterModel: stored.openrouterModel ?? stored.openrouter_model ?? null,
    openrouterModelParameters: (
      stored.openrouterModelParameters
      ?? stored.openrouter_model_parameters
      ?? null
    ),
    ollamaSelection: stored.ollamaSelection ?? stored.ollama_selection ?? null,
  });
}

function readLegacyLocalModelPreference() {
  const openaiModel = normaliseLocalModelName(readStoredString(LS_OPENAI_SELECTED_MODEL));
  const openaiModelParameters = normaliseModelParameters(readStoredJson(LS_OPENAI_MODEL_PARAMETERS));
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
    openaiModelParameters,
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
    const openaiModelParameters = normaliseModelParameters(preference?.openaiModelParameters);
    if (openaiModelParameters) {
      localStorage.setItem(LS_OPENAI_MODEL_PARAMETERS, JSON.stringify(openaiModelParameters));
    } else {
      localStorage.removeItem(LS_OPENAI_MODEL_PARAMETERS);
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

function buildPremiumRequestedLlm(providerName, modelName, modelParameters = null) {
  const provider = normalisePremiumProvider(providerName);
  const model = normaliseLocalModelName(modelName);
  if (!provider || !model) return null;
  const params = normaliseModelParameters(modelParameters);

  const requested = {
    provider,
    model,
    requestModel: `${provider}:${model}`,
  };
  if (params) requested.model_parameters = params;
  return requested;
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
      openrouterModel: null,
      ollamaSelection: null,
      requestedLlm: null,
      modelUnavailable: false,
      modelUnavailableReason: null,
    };
  }

  let requestedLlm = null;
  if (PREMIUM_MODEL_PROVIDERS.has(canonical.activeSource)) {
    const provider = canonical.activeSource;
    requestedLlm = buildPremiumRequestedLlm(
      provider,
      provider === 'gemini'
        ? canonical.geminiModel
        : provider === 'openrouter'
          ? canonical.openrouterModel
          : canonical.openaiModel,
      provider === 'gemini'
        ? canonical.geminiModelParameters
        : provider === 'openrouter'
          ? canonical.openrouterModelParameters
          : canonical.openaiModelParameters,
    );
  } else if (canonical.activeSource === 'ollama') {
    requestedLlm = buildOllamaRequestedLlm(canonical.ollamaSelection);
  }

  let modelUnavailableReason = null;
  if (!requestedLlm && canonical.activeSource === 'ollama') {
    modelUnavailableReason = LOCAL_MODEL_UNAVAILABLE_REASON_NO_OLLAMA_MODEL;
  } else if (!requestedLlm && PREMIUM_MODEL_PROVIDERS.has(canonical.activeSource)) {
    modelUnavailableReason = LOCAL_MODEL_UNAVAILABLE_REASON_NO_PREMIUM_MODEL;
  }

  return {
    ...canonical,
    requestedLlm,
    modelUnavailable: !!modelUnavailableReason,
    modelUnavailableReason,
  };
}

export function getStoredLocalModelPreference() {
  const rawStored = readStoredJson(LS_LOCAL_MODEL_PREFERENCE);
  const stored = normaliseStoredLocalModelPreference(rawStored);
  if (stored) {
    const legacyOpenAiModel = readStoredString(LS_OPENAI_SELECTED_MODEL);
    const legacyOpenAiModelClean = normaliseLocalModelName(legacyOpenAiModel);
    const legacyOpenAiModelMismatched = !!legacyOpenAiModel
      && legacyOpenAiModelClean !== (stored.openaiModel || null);
    if (JSON.stringify(rawStored) !== JSON.stringify(stored) || legacyOpenAiModelMismatched) {
      persistLocalModelPreference(stored);
    }
    return stored;
  }

  const migrated = readLegacyLocalModelPreference();
  if (migrated) {
    persistLocalModelPreference(migrated);
    return migrated;
  }

  if (rawStored) {
    persistLocalModelPreference(null);
  }
  return null;
}

export function getEffectiveLocalModelPreference() {
  return buildEffectiveLocalModelPreference(getStoredLocalModelPreference());
}

export function hasLocalPremiumModelUsePreference() {
  return PREMIUM_MODEL_PROVIDERS.has(getEffectiveLocalModelPreference().activeSource);
}

export function getLocalPremiumModelUseEnabled(defaultValue = false) {
  const effective = getEffectiveLocalModelPreference();
  if (!effective.activeSource) return !!defaultValue;
  return PREMIUM_MODEL_PROVIDERS.has(effective.activeSource);
}

export function getStoredPremiumModelProvider() {
  const effective = getEffectiveLocalModelPreference();
  return normalisePremiumProvider(effective.premiumProvider)
    || (PREMIUM_MODEL_PROVIDERS.has(effective.activeSource) ? effective.activeSource : null)
    || 'openai';
}

export function setStoredPremiumModelProvider(providerName) {
  const requestedProvider = normalisePremiumProvider(providerName);
  if (providerName != null && !requestedProvider) {
    return getStoredPremiumModelProvider();
  }
  const provider = requestedProvider || 'openai';
  const stored = getStoredLocalModelPreference();
  const next = cloneLocalModelPreference(stored) || {
    schemaVersion: LOCAL_MODEL_PREFERENCE_SCHEMA,
    activeSource: null,
    openaiModel: null,
    ollamaSelection: null,
  };
  if (provider !== 'openai') {
    next.premiumProvider = provider;
  } else {
    delete next.premiumProvider;
  }
  if (PREMIUM_MODEL_PROVIDERS.has(next.activeSource)) {
    next.activeSource = provider;
  }
  persistLocalModelPreference(next);
  return provider;
}

export function setLocalPremiumModelUseEnabled(enabled, providerName = null) {
  const stored = getStoredLocalModelPreference();
  const next = cloneLocalModelPreference(stored) || {
    schemaVersion: LOCAL_MODEL_PREFERENCE_SCHEMA,
    activeSource: null,
    openaiModel: null,
    openaiModelParameters: null,
    ollamaSelection: null,
  };
  const provider = normalisePremiumProvider(providerName)
    || normalisePremiumProvider(next.premiumProvider)
    || (PREMIUM_MODEL_PROVIDERS.has(next.activeSource) ? next.activeSource : null)
    || 'openai';
  if (provider !== 'openai') {
    next.premiumProvider = provider;
  } else {
    delete next.premiumProvider;
  }
  next.activeSource = enabled ? provider : 'ollama';
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
    openaiModelParameters: null,
    ollamaSelection: null,
  };
  next.openaiModel = normaliseLocalModelName(modelName);
  persistLocalModelPreference(next);
}

export function getStoredOpenAiModelParameters() {
  return getEffectiveLocalModelPreference().openaiModelParameters || null;
}

export function setStoredOpenAiModelParameters(modelParameters) {
  const stored = getStoredLocalModelPreference();
  const next = cloneLocalModelPreference(stored) || {
    schemaVersion: LOCAL_MODEL_PREFERENCE_SCHEMA,
    activeSource: null,
    openaiModel: null,
    openaiModelParameters: null,
    ollamaSelection: null,
  };
  const params = normaliseModelParameters(modelParameters);
  if (params) {
    next.openaiModelParameters = params;
  } else {
    delete next.openaiModelParameters;
  }
  persistLocalModelPreference(next);
}

export function getStoredGeminiSelectedModel() {
  return getEffectiveLocalModelPreference().geminiModel || '';
}

export function setStoredGeminiSelectedModel(modelName) {
  const stored = getStoredLocalModelPreference();
  const next = cloneLocalModelPreference(stored) || {
    schemaVersion: LOCAL_MODEL_PREFERENCE_SCHEMA,
    activeSource: null,
    openaiModel: null,
    ollamaSelection: null,
  };
  const model = normaliseLocalModelName(modelName);
  if (model) {
    next.geminiModel = model;
  } else {
    delete next.geminiModel;
  }
  persistLocalModelPreference(next);
}

export function getStoredGeminiModelParameters() {
  return getEffectiveLocalModelPreference().geminiModelParameters || null;
}

export function setStoredGeminiModelParameters(modelParameters) {
  const stored = getStoredLocalModelPreference();
  const next = cloneLocalModelPreference(stored) || {
    schemaVersion: LOCAL_MODEL_PREFERENCE_SCHEMA,
    activeSource: null,
    openaiModel: null,
    ollamaSelection: null,
  };
  const params = normaliseModelParameters(modelParameters);
  if (params) {
    next.geminiModelParameters = params;
  } else {
    delete next.geminiModelParameters;
  }
  persistLocalModelPreference(next);
}

export function getStoredOpenRouterSelectedModel() {
  return getEffectiveLocalModelPreference().openrouterModel || '';
}

export function setStoredOpenRouterSelectedModel(modelName) {
  const stored = getStoredLocalModelPreference();
  const next = cloneLocalModelPreference(stored) || {
    schemaVersion: LOCAL_MODEL_PREFERENCE_SCHEMA,
    activeSource: null,
    openaiModel: null,
    ollamaSelection: null,
  };
  const model = normaliseLocalModelName(modelName);
  if (model) {
    next.openrouterModel = model;
  } else {
    delete next.openrouterModel;
  }
  persistLocalModelPreference(next);
}

export function getStoredOpenRouterModelParameters() {
  return getEffectiveLocalModelPreference().openrouterModelParameters || null;
}

export function setStoredOpenRouterModelParameters(modelParameters) {
  const stored = getStoredLocalModelPreference();
  const next = cloneLocalModelPreference(stored) || {
    schemaVersion: LOCAL_MODEL_PREFERENCE_SCHEMA,
    activeSource: null,
    openaiModel: null,
    ollamaSelection: null,
  };
  const params = normaliseModelParameters(modelParameters);
  if (params) {
    next.openrouterModelParameters = params;
  } else {
    delete next.openrouterModelParameters;
  }
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
    openaiModelParameters: null,
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

export function buildLocalModelRequestFields(requestedLlm = resolveLocalRequestedLlm()) {
  const provider = normaliseActiveSource(requestedLlm?.provider);
  const model = normaliseLocalModelName(requestedLlm?.model);
  if (!provider || !model) return {};

  const modelParameters = normaliseModelParameters(requestedLlm?.model_parameters);
  return {
    model,
    model_provider: provider,
    ...(modelParameters ? { model_parameters: modelParameters } : {}),
  };
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
      ...(localOverride.model_parameters ? { model_parameters: localOverride.model_parameters } : {}),
    },
  };
}
