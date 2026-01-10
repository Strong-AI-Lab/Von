const rawFlags = (() => {
  if (typeof window === 'undefined') return {};
  const payload = window.__VON_FEATURE_FLAGS__;
  return payload && typeof payload === 'object' ? payload : {};
})();

const expertTabsEnabled = Boolean(rawFlags.expertTabsEnabled);

export function isExpertTabsEnabled() {
  return expertTabsEnabled;
}

export function isAnnotationEnabled() {
  return expertTabsEnabled;
}

export function isVontologyEnabled() {
  return expertTabsEnabled;
}

export function isImportExportEnabled() {
  return expertTabsEnabled;
}

export function getFeatureFlags() {
  return {
    expertTabsEnabled,
    annotationEnabled: expertTabsEnabled,
    vontologyEnabled: expertTabsEnabled,
    importExportEnabled: expertTabsEnabled
  };
}
