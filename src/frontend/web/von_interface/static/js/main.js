import { setWorkflowStudioAccess } from './workflowStudioAccess.js';
import { setupDynamicLayout } from './components/dynamicLayout.js';
import { initializeDomElements, initializeInfoPopup, loadAndDisplayGlobalModelInFooter, setFooterServerReachability } from './domUtils.js';
import {
  closeDynamicConceptTab,
  createOrActivateConceptTab,
  restorePersistedConceptTabs
} from './dynamicTabs.js';
import { isExpertTabsEnabled } from './featureFlags.js';
import { getLanguageDisplayName } from './languageConfig.js';
import { escapeHtml } from './markdownUtils.js';
import './suppressTooltips.js';
import { activateTab, loadTabData, setupTabNavigation } from './tabNavigation.js';
import { evaluateServerHealthState } from './utils/serverHealthState.js';
import { handleSelectConceptByIdDetail } from './utils/selectConceptByIdHandler.js';
import {
  hasAuthenticatedVonActor,
  applyHomeAuthUnavailable,
  initialiseHomeAuthentication,
  shouldClearHomeIdentityMirrors,
} from './homeAuthGate.js';
import { formatBackgroundTaskSummary, formatBackgroundTaskTooltip, subscribeBackgroundTaskUpdates } from './backgroundTaskTracker.js';
import {
  copyJsonTextWithButtonFeedback,
  copyTextWithClipboardFallback,
  initialiseCopyJsonButtonPreCopyState,
  resetCopyJsonButtonPreCopyState
} from './utils/copyJsonButtonState.js';
import {
  clearAllOrgContext,
  getSessionScopedNamespace,
  getSessionScopedOrgContext,
  hasSessionOrgContext,
  hasSessionPersonalOrgContext,
  setSessionScopedNamespace,
  setSessionScopedOrgContext,
  syncNamespaceFromLocalStorage,
  syncOrgContextFromLocalStorage
} from './utils/sessionScopedStorage.js';
import { buildHealthTelemetryCopyPayload, buildHealthTelemetrySnapshot } from './utils/healthTelemetrySnapshot.js';
import {
  parseStoredContextValue,
  resolveBrowserBootstrapNamespace,
  resolveBrowserBootstrapOrganisationContext,
  resolveBrowserBootstrapUserContext
} from './utils/runtimeIdentityBootstrap.js';
import { recoverPersonalContextAfterMembershipDenial } from './utils/organisationSessionRecovery.js';
import { hydrateStoredSelectionsFromUserPreferences } from './utils/userPreferenceBootstrap.js';
import { initialiseLoginOrganisation } from './utils/loginOrganisationBootstrap.js';
import { isVontologyBusy, loadKeyConceptsForUser, preloadVontologyData, selectVontologyNodeByIdentifier, setupVontologySearchUI } from './vontology.js';

// JVNAUTOSCI-1011: getCurrentNamespace is now provided by sessionScopedStorage.js
const getCurrentNamespace = getSessionScopedNamespace;

document.addEventListener('DOMContentLoaded', async () => {
  console.log("DOM fully loaded and parsed.");

  const authStatus = await initialiseHomeAuthentication();
  if (!hasAuthenticatedVonActor(authStatus)) {
    if (shouldClearHomeIdentityMirrors(authStatus)) {
      clearAllOrgContext();
      try {
        localStorage.removeItem('von_current_user');
        sessionStorage.removeItem('von_current_user');
      } catch {
        // Browser mirrors carry no authority; remain signed out if storage is unavailable.
      }
    }
    console.log('[main] Home application initialization stopped until login establishes an actor.');
    return;
  }

  initializeDomElements();
  initializeInfoPopup();
  applyExpertTabGuards();
  setWorkflowStudioAccess(authStatus);
  setupTabNavigation();
  setupSettingsFrameResizing();
  initialiseCopyJsonButtonPreCopyState();

  // Initialize global search UI (JVNAUTOSCI-550)
  setupVontologySearchUI();

  // Dynamic positioning: calculate header height and position tabs accordingly
  setupDynamicLayout();

  // Ensure user context is loaded BEFORE initializing chat to prevent race condition
  // where conversation history loads with null user_id
  try {
    await ensureUserContext(authStatus);
  } catch (error) {
    applyHomeAuthUnavailable(new Error(`Organisation context could not be confirmed. ${error.message}`));
    return;
  }
  loadDeferredSettingsFrame();

  // JVNAUTOSCI-954: report bounded, client-reported capability hints (speech/audio)
  import('./clientCapabilitiesReporter.js').then(module => {
    if (module.startClientCapabilitiesReporting) {
      try {
        window.__vonClientCapabilitiesReporter = module.startClientCapabilitiesReporting();
      } catch (e) {
        console.warn('[main] Client capability reporter failed to start:', e);
      }
    }
  }).catch(err => console.error('Error loading client capability reporter:', err));

  // CRITICAL: Initialize chat tab AFTER ensureUserContext completes (must await)
  // This ensures Flask session org is set before /history/sessions is called
  console.log("Initializing chat tab...");
  try {
    const chatModule = await import('./chatTab.js');
    if (chatModule.initializeChatTab) {
      chatModule.initializeChatTab();
    }
  } catch (err) {
    console.error('Error loading chat tab module:', err);
  }

  // JVNAUTOSCI-1071: Initialize messages button
  try {
    const messagePanelModule = await import('./components/messagePanel.js');
    const messagesBtn = document.getElementById('messagesBtn');
    if (messagesBtn && messagePanelModule.showMessagesTab) {
      messagesBtn.addEventListener('click', () => {
        messagePanelModule.showMessagesTab();
      });
      console.log('[main] Messages button initialized');
    }
    // Load initial unread count
    if (messagePanelModule.loadUnreadCount) {
      messagePanelModule.loadUnreadCount();
    }
  } catch (err) {
    console.warn('[main] Failed to initialize messages button:', err);
  }

  // Start background preload of Vontology data while chat is active
  try {
    preloadVontologyData();
    startHealthPolling();
  } catch (e) {
    console.warn('Failed to start Vontology preload:', e);
    setTimeout(startHealthPolling, 3000);
  }

  // Load key concepts for current user immediately (needed before any concept tabs open)
  loadKeyConceptsForUser().catch(err => {
    console.warn('Failed to load key concepts during initialization:', err);
  });

  await loadAndDisplayGlobalModelInFooter();
  await loadAndDisplayLanguageIndicator();

  // Global handler: clicking a Vontology token navigates to concept and selects node
  document.addEventListener('von:selectConceptById', (e) => {
    try {
      // Async handler (do not block UI thread).
      void handleSelectConceptByIdDetail(e?.detail, {
        createOrActivateConceptTab,
        closeDynamicConceptTab,
        activateTab,
        selectVontologyNodeByIdentifier
      });
    } catch (err) {
      console.warn('Failed to handle von:selectConceptById:', err);
    }
  });

  const restoredConceptTabs = await restorePersistedConceptTabs();

  // Check URL hash for initial tab
  const hash = window.location.hash.substring(1);
  let initialTabId = restoredConceptTabs.activeTabId || 'chatTab'; // Default to chatTab

  if (hash) {
    const potentialTab = document.getElementById(hash);

    // Check if it's an existing tab with 'tab-content' class
    if (potentialTab && potentialTab.classList.contains('tab-content')) {
      initialTabId = hash;
      console.log(`Initial tab set from URL hash: #${initialTabId}`);
    }
    // Check if it's a dynamic concept tab that needs to be created
    else if (hash.startsWith('conceptTab_')) {
      // Extract the concept ID from the tab ID
      // conceptTab__V_person -> #V#person
      const conceptIdPart = hash.replace(/^conceptTab_/, '');
      const conceptId = conceptIdPart.replace(/_/g, '#');

      console.log(`Detected concept tab hash: ${hash}, extracted concept ID: ${conceptId}`);

      // Try to find the concept to get its display name
      try {
        // For now, just use the concept ID as the display name
        // TODO: Could enhance this to fetch proper display name from the server
        const conceptName = conceptId;

        // Create the concept tab, but don't activate it yet (we'll do that below)
        createOrActivateConceptTab(conceptId, conceptName, false);

        // Now set it as the initial tab
        initialTabId = hash;
        console.log(`Created concept tab from URL hash: #${initialTabId} for concept ${conceptId}`);
      } catch (err) {
        console.warn(`Failed to create concept tab for hash '${hash}':`, err);
        console.warn('Defaulting to chatTab.');
      }
    }
    else {
      console.warn(`URL hash '#${hash}' does not correspond to a valid tab ID. Defaulting to chatTab.`);
    }
  }

  // Activate the determined initial tab
  activateTab(initialTabId);

  // Load data for the initially activated tab
  console.log("Performing initial data loads for tabs...");
  await loadTabData(initialTabId);
  console.log("Initial data loads complete.");
});

function loadDeferredSettingsFrame() {
  const settingsFrame = document.getElementById('settingsFrame');
  if (!(settingsFrame instanceof HTMLIFrameElement)) return;
  const deferredSrc = String(settingsFrame.dataset.src || '').trim();
  if (!deferredSrc || settingsFrame.dataset.actorContextReady === 'true') return;
  settingsFrame.dataset.actorContextReady = 'true';
  settingsFrame.src = deferredSrc;
}

window.addEventListener('von:windowSessionIdentityChanged', () => {
  // A late same-origin collision response can rotate the provisional ID after
  // startup. Rebind the retained per-tab organisation to the replacement ID.
  void syncFlaskSessionOrg();
});

function applyExpertTabGuards() {
  if (isExpertTabsEnabled()) return;

  const guardedTabs = ['vontologyTab', 'importExportTab', 'annotationTab'];
  guardedTabs.forEach((tabId) => {
    const button = document.querySelector(`.tab-button[data-tab="${tabId}"]`);
    if (button) {
      button.remove();
    }
    const content = document.getElementById(tabId);
    if (content) {
      content.remove();
    }
  });

  const annotationToggle = document.getElementById('annotationToggle');
  const annotationLabel = annotationToggle ? annotationToggle.closest('.annotation-toggle') : null;
  if (annotationLabel) {
    annotationLabel.remove();
  } else if (annotationToggle) {
    annotationToggle.remove();
  }
}

// Function to handle iframe resizing
function setupSettingsFrameResizing() {
  const settingsFrame = document.getElementById('settingsFrame');
  if (!settingsFrame) return;

  let lastAutoHeight = 0;
  let userResized = false;

  // Observe inline style changes to detect manual resize
  const resizeObserver = new MutationObserver(() => {
    // If the iframe has a height style different from last auto height, treat as user resize
    const current = parseInt(settingsFrame.style.height || settingsFrame.clientHeight, 10);
    if (lastAutoHeight && Math.abs(current - lastAutoHeight) > 20) {
      userResized = true;
    }
  });
  resizeObserver.observe(settingsFrame, { attributes: true, attributeFilter: ['style'] });

  function computeAvailableHeight(requested) {
    try {
      const footer = document.querySelector('.footer-container');
      const wrapper = settingsFrame.closest('.settings-frame-wrapper');
      const frameTop = (wrapper || settingsFrame).getBoundingClientRect().top;
      const footerTop = footer ? footer.getBoundingClientRect().top : window.innerHeight - 64;
      const max = Math.max(240, Math.floor(footerTop - frameTop - 8));
      let target = Math.min(requested || max, max);
      target = Math.max(target, Math.min(320, max));
      return target;
    } catch { return requested || 600; }
  }

  window.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'settings-frame-height') {
      if (userResized) return; // do not override manual resize
      const rawHeight = event.data.height;
      if (rawHeight > 30000) return;
      const adjusted = computeAvailableHeight(rawHeight);
      if (Math.abs(adjusted - lastAutoHeight) < 5) return;
      settingsFrame.style.height = `${adjusted}px`;
      settingsFrame.style.minHeight = '0px';
      lastAutoHeight = adjusted;
      // console.debug('Auto-resized settings frame to', adjusted);
    }
    if (event.data && event.data.type === 'settings-content-loaded') {
      try { settingsFrame.contentWindow.postMessage({ type: 'request-height' }, '*'); } catch (e) { console.error('Height request failed', e); }
    }
  });

  settingsFrame.addEventListener('load', () => {
    userResized = false; // reset when reloading
    try { settingsFrame.contentWindow.postMessage({ type: 'request-height' }, '*'); } catch (e) { console.error('Error sending message to settings frame:', e); }
  });

  // Recompute on window resize if still auto-controlled
  window.addEventListener('resize', () => {
    if (userResized) return;
    const current = parseInt(settingsFrame.style.height || settingsFrame.clientHeight, 10);
    const recomputed = computeAvailableHeight(current);
    settingsFrame.style.height = `${recomputed}px`;
    settingsFrame.style.minHeight = '0px';
    lastAutoHeight = recomputed;
  });
}

/**
 * JVNAUTOSCI-1011: Initialise sessionStorage from localStorage on new window/tab.
 * Now uses central helpers from sessionScopedStorage.js.
 */
function initSessionStorageFromLocalStorage() {
  if (hasSessionOrgContext()) {
    console.log('[main] sessionStorage already has org context, repairing namespace if needed');
  }
  syncOrgContextFromLocalStorage();
  syncNamespaceFromLocalStorage();
}

/**
 * Sync the Flask session's organisation_concept_id.
 * First checks if Flask session already has an org (from a previous request in this browser session).
 * If not, syncs from sessionStorage/localStorage to Flask session.
 * This avoids race conditions where /history/sessions is called before org context is set.
 *
 * JVNAUTOSCI-1011: Now uses sessionStorage (window-scoped) instead of localStorage,
 * enabling different organisation contexts in different browser windows.
 * The X-Von-Window-Session header is automatically added by getJson/postJson.
 */
async function syncFlaskSessionOrg() {
  try {
    // Import to ensure window session ID is generated
    const { getJson, postJson, ensureUniqueWindowSessionId } = await import('./apiService.js');
    // Resolve a copied duplicate-tab ID before reading or mutating the
    // server-side window organisation context.
    await ensureUniqueWindowSessionId();

    // First, check if window session already has an org
    const context = await getJson('/von/api/session/context');

    // JVNAUTOSCI-1011 FIX: When context comes from flask_session fallback (server restarted,
    // window session store empty), we MUST still sync from localStorage to window session.
    // The Flask session cookie may have stale org data from before restart.
    // Only trust window_session source as authoritative.
    const isWindowSessionSource = context.context_source === 'window_session';

    if (context.organisation_id && isWindowSessionSource) {
      // Window session store already has org - trust it as source of truth
      console.log('[main] Window session has org:', context.organisation_id);
      const parsed = parseStoredContextValue(sessionStorage.getItem('von_current_org')) || {};
      if (parsed?.concept_id !== context.organisation_id) {
        setSessionScopedOrgContext({
          id: null,
          concept_id: context.organisation_id,
          name: parsed?.name || null
        });
        console.log('[main] Updated sessionStorage org to match window session');
      }
      if (context.namespace) {
        setSessionScopedNamespace(context.namespace);
      }
      return;
    }

    // Window session store is empty (or context came from Flask session fallback).
    // Sync from localStorage to window session store - this is the user's INTENDED org.
    const storedOrg = getSessionScopedOrgContext();
    if (!storedOrg) {
      console.log('[main] No org in sessionStorage/localStorage, proceeding without org');
      // Personal is an explicit per-tab binding. Materialise it even when the
      // cookie fallback also happens to be null, so later calls cannot drift to
      // another tab's organisation through a non-window fallback.
      if (!isWindowSessionSource) {
        console.log('[main] Binding Personal context to this window session');
        await postJson('/von/api/session/set_organisation', {
          organisation_concept_id: null
        });
      }
      return;
    }
    const orgConceptId = storedOrg?.concept_id;
    if (!orgConceptId) {
      console.log('[main] No org concept_id in storage, proceeding without org');
      return;
    }

    // Check if we need to sync (org mismatch or flask_session fallback)
    const needsSync = !isWindowSessionSource || context.organisation_id !== orgConceptId;
    if (!needsSync) {
      console.log('[main] Org already synced:', orgConceptId);
      return;
    }

    console.log('[main] Syncing organisation to window session from storage:', orgConceptId,
      context.organisation_id ? `(was: ${context.organisation_id} from ${context.context_source})` : '(no previous org)');
    let syncData;
    try {
      syncData = await postJson('/von/api/session/set_organisation', {
        organisation_concept_id: orgConceptId
      });
    } catch (err) {
      const currentUser = parseStoredContextValue(localStorage.getItem('von_current_user'));
      const recovery = await recoverPersonalContextAfterMembershipDenial({
        error: err,
        postJson,
        userContext: currentUser,
      });
      if (!recovery) {
        throw err;
      }
      console.warn(
        '[main] Stored organisation is unavailable to the authenticated user; recovered to Personal:',
        orgConceptId
      );
      return recovery;
    }
    console.log('[main] Window session org synced:', syncData);
    if (syncData.namespace) {
      setSessionScopedNamespace(syncData.namespace);
    }
  } catch (err) {
    console.warn('[main] Error syncing org to session:', err);
  }
}

/**
 * Mirror the authenticated actor into compatibility storage before app initialization.
 * The login-bound server session remains authoritative; browser state may retain only
 * the matching user projection and organisation/language preferences.
 *
 * JVNAUTOSCI-1011: Now uses sessionStorage for org context (window-scoped),
 * while the derived user projection remains in localStorage (shared across windows).
 */
async function ensureUserContext(authStatus) {
  try {
    // JVNAUTOSCI-1011: On new window/restart, copy org preference from localStorage
    // This ensures the user's last-used org is restored even in a new window
    initSessionStorageFromLocalStorage();

    // Import to ensure window session ID is generated
    const { getJson, postJson, ensureUniqueWindowSessionId, replaceMismatchedWindowSessionId } = await import('./apiService.js');
    // Resolve a copied duplicate-tab ID before any actor-scoped bootstrap call.
    await ensureUniqueWindowSessionId();

    let storedUser = parseStoredContextValue(localStorage.getItem('von_current_user'));
    const authenticatedUser = resolveBrowserBootstrapUserContext({
      authStatus,
      storedUser,
    });
    if (!authenticatedUser?.concept_id) {
      throw new Error('Authenticated login did not resolve a Von user identity.');
    }

    if (storedUser?.concept_id !== authenticatedUser.concept_id) {
      clearAllOrgContext();
      try {
        localStorage.removeItem('von_current_user');
        sessionStorage.removeItem('von_current_user');
      } catch {
        // Ignore compatibility-cache cleanup failures; the session remains authoritative.
      }
    }

    const encodedAuthenticatedUser = JSON.stringify(authenticatedUser);
    localStorage.setItem('von_current_user', encodedAuthenticatedUser);
    sessionStorage.setItem('von_current_user', encodedAuthenticatedUser);
    storedUser = authenticatedUser;

    await initialiseLoginOrganisation(authStatus, {
      postJson,
      rotateWindow: replaceMismatchedWindowSessionId,
    });

    try {
      await hydrateStoredSelectionsFromUserPreferences(authenticatedUser.concept_id);
    } catch (e) {
      console.warn('[main] Failed to hydrate stored selections from authenticated user preferences:', e);
    }

    // Preference hydration may have supplied an organisation for an otherwise
    // unset tab. Bind that explicit tab selection before settings can observe
    // a cookie-global fallback through the newly unique window ID.
    await syncFlaskSessionOrg();

    console.log('[main] Fetching settings to populate user context...');
    const settings = await getJson('/api/settings/');
    const sessionContext = await getJson('/von/api/session/context').catch(() => null);

    const resolvedUser = resolveBrowserBootstrapUserContext({
      authStatus,
      storedUser
    });

    if (resolvedUser) {
      const encodedUser = JSON.stringify(resolvedUser);
      localStorage.setItem('von_current_user', encodedUser);
      sessionStorage.setItem('von_current_user', encodedUser);
      console.log('[main] Populated von_current_user from browser bootstrap context');
    }

    const personalContextSelected = hasSessionPersonalOrgContext();
    const resolvedOrg = personalContextSelected
      ? null
      : resolveBrowserBootstrapOrganisationContext({
        settings,
        sessionContext,
        storedOrganisation: getSessionScopedOrgContext()
      });

    if (resolvedOrg) {
      setSessionScopedOrgContext(resolvedOrg);
      console.log('[main] Populated von_current_org from browser bootstrap context');
    }

    const resolvedNamespace = resolveBrowserBootstrapNamespace({
      settings: personalContextSelected
        ? {
          ...settings,
          current_organisation_id: null,
          current_organisation_concept_id: null,
          current_organisation_name: null
        }
        : settings,
      sessionContext: personalContextSelected ? null : sessionContext,
      userContext: resolvedUser,
      organisationContext: resolvedOrg
    });
    if (resolvedNamespace) {
      setSessionScopedNamespace(resolvedNamespace);
      console.log('[main] Populated current_user_namespace from browser bootstrap context:', resolvedNamespace);
    }

    // CRITICAL: Sync the session's organisation_concept_id to avoid race conditions
    // where /history/sessions is called before the org context is set in the server session.
    await syncFlaskSessionOrg();
  } catch (e) {
    console.warn('[main] Failed to ensure user context:', e);
    throw e; // Do not initialise chat/tools in an unconfirmed organisation.
  }
}

/**
 * Load and display the current language preference in the footer indicator
 */
async function loadAndDisplayLanguageIndicator() {
  try {
    const response = await fetch('/api/settings');
    const settings = await response.json();

    const languageIndicator = document.getElementById('languageIndicator');
    if (languageIndicator && settings.preferred_language) {
      const languageName = getLanguageDisplayName(settings.preferred_language);
      languageIndicator.textContent = `🌐 ${languageName}`;
      languageIndicator.title = `Current language preference: ${languageName}`;
    }
  } catch (error) {
    console.error('Failed to load language preference:', error);
    // Fallback to default
    const languageIndicator = document.getElementById('languageIndicator');
    if (languageIndicator) {
      languageIndicator.textContent = '🌐 en-NZ';
      languageIndicator.title = 'Current language preference: English (NZ)';
    }
  }
}

// Also listen for settings changes to update the language indicator
document.addEventListener('von:settingsChanged', loadAndDisplayLanguageIndicator);

// JVNAUTOSCI-1011: Listen for org switches (from settings iframe) and update parent's sessionStorage
// This ensures the footer reads the correct org value immediately (not stale sessionStorage)
document.addEventListener('orgSwitched', (event) => {
  const detail = event.detail || {};
  const orgId = detail.organisation_id;
  const orgName = detail.organisation_name || null;
  const namespace = detail.namespace;
  console.log('[main] orgSwitched event received, updating sessionStorage:', { orgId, orgName, namespace });

  try {
    if (orgId) {
      // Sync org to this tab so footer and request context update together.
      const parsed = getSessionScopedOrgContext() || {};
      setSessionScopedOrgContext({
        id: parsed.id || null,
        concept_id: orgId,
        name: orgName || parsed.name || null
      });
    } else {
      // Personal is an explicit per-tab selection, not permission to fall
      // through to another tab's shared localStorage organisation.
      setSessionScopedOrgContext(null);
    }
    if (namespace) {
      sessionStorage.setItem('current_user_namespace', namespace);
    }
  } catch (e) {
    console.warn('[main] Error updating sessionStorage on orgSwitched:', e);
  }

  // Refresh footer immediately with updated storage values
  if (typeof window.updateModelInfoFooterDisplay === 'function') {
    window.updateModelInfoFooterDisplay();
  }
});



function startHealthPolling() {
  const localIpSpan = document.getElementById('serverLocalIpValue');
  const publicIpSpan = document.getElementById('serverPublicIpValue');
  const pidSpan = document.getElementById('serverPidValue');
  const uptimeSpan = document.getElementById('serverUptimeValue');
  const buildInfo = document.getElementById('serverBuildInfo');
  const buildSpan = document.getElementById('serverBuildValue');
  const ragSpan = document.getElementById('ragIndexingValue');
  const ragDetailsBtn = document.getElementById('ragIndexingValue');
  const ragModal = document.getElementById('ragStatusModal');
  const ragModalBody = ragModal ? document.getElementById('ragStatusBody') : null;
  const ragRuntimeHint = ragModal ? document.getElementById('ragRuntimeHint') : null;
  const ragModalClose = ragModal ? document.getElementById('ragStatusClose') : null;
  const ragModalCheck = ragModal ? document.getElementById('ragStatusCheck') : null;
  const ragChatBackfillBtn = ragModal ? document.getElementById('ragChatBackfill') : null;
  const busyEl = document.getElementById('vontologyBusyIndicator');
  const busySr = document.getElementById('vontologyBusySrStatus');
  const backgroundTaskEl = document.getElementById('backgroundTaskFooterStatus');
  if (!localIpSpan && !publicIpSpan && !pidSpan && !uptimeSpan && !ragSpan && !ragModal && !busyEl && !backgroundTaskEl) {
    return;
  }
  // Copy-to-clipboard behavior for local IP address
  if (localIpSpan) {
    localIpSpan.addEventListener('click', async () => {
      const ipText = localIpSpan.textContent.trim();
      if (!ipText || ipText === '?') return;
      try {
        await navigator.clipboard.writeText(ipText);
        localIpSpan.classList.add('copied');
        const oldTitle = localIpSpan.title;
        localIpSpan.title = 'Copied!';
        setTimeout(() => { localIpSpan.classList.remove('copied'); localIpSpan.title = oldTitle; }, 1200);
      } catch (err) { console.warn('Local IP copy failed', err); }
    });
  }
  // Copy-to-clipboard behavior for public IP address
  if (publicIpSpan) {
    publicIpSpan.addEventListener('click', async () => {
      const ipText = publicIpSpan.textContent.trim();
      if (!ipText || ipText === '?') return;
      try {
        await navigator.clipboard.writeText(ipText);
        publicIpSpan.classList.add('copied');
        const oldTitle = publicIpSpan.title;
        publicIpSpan.title = 'Copied!';
        setTimeout(() => { publicIpSpan.classList.remove('copied'); publicIpSpan.title = oldTitle; }, 1200);
      } catch (err) { console.warn('Public IP copy failed', err); }
    });
  }
  // Copy-to-clipboard behavior for PID
  if (pidSpan) {
    pidSpan.addEventListener('click', async () => {
      const pidText = pidSpan.textContent.trim();
      if (!pidText || pidText === '?' || /[^0-9]/.test(pidText)) return;
      try {
        await navigator.clipboard.writeText(pidText);
        pidSpan.classList.add('copied');
        const oldTitle = pidSpan.title;
        pidSpan.title = 'Copied!';
        setTimeout(() => { pidSpan.classList.remove('copied'); pidSpan.title = oldTitle; }, 1200);
      } catch (err) { console.warn('PID copy failed', err); }
    });
  }
  if (uptimeSpan && uptimeSpan.dataset.healthTelemetryCopyBound !== 'true') {
    uptimeSpan.dataset.healthTelemetryCopyBound = 'true';
    uptimeSpan.setAttribute('role', 'button');
    uptimeSpan.setAttribute('tabindex', '0');
    uptimeSpan.addEventListener('click', () => {
      if (!canCopyHealthTelemetryFromUptime()) return;
      void copyCurrentHealthTelemetry('uptime_badge_click');
    });
    uptimeSpan.addEventListener('keydown', (event) => {
      if (!canCopyHealthTelemetryFromUptime()) return;
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      void copyCurrentHealthTelemetry('uptime_badge_keypress');
    });
  }
  const HEALTH_START_TIME_CACHE_KEY = 'von_server_start_time_iso';
  const healthLoopStartedAtMs = Date.now();
  function readCachedStartTimeIso() {
    try {
      const value = localStorage.getItem(HEALTH_START_TIME_CACHE_KEY);
      if (!value) return null;
      return Number.isNaN(Date.parse(value)) ? null : value;
    } catch (_) {
      return null;
    }
  }
  function writeCachedStartTimeIso(value) {
    try {
      if (!value) {
        localStorage.removeItem(HEALTH_START_TIME_CACHE_KEY);
      } else {
        localStorage.setItem(HEALTH_START_TIME_CACHE_KEY, value);
      }
    } catch (_) {
      // Ignore storage errors; uptime still updates from live health polls.
    }
  }
  let startTimeIso = readCachedStartTimeIso();
  let lastIdentity = { pid: null, start: null };
  let reloadTriggered = false;
  let serverReachable = null;
  let serverHealthUiState = 'waiting';
  let failureCount = 0;
  let healthPollInFlight = false;
  let healthPollQueuedImmediate = false;
  let healthPollTimerId = null;
  let nextScheduledHealthPollAtMs = null;
  let lastHealthCheckCompletedAtMs = null;
  let hasSeenSuccessfulHealthPoll = false;
  let firstFailureAtMs = null;
  let lastHealthSuccessAtMs = null;
  let lastHealthErrorKind = null;
  let lastHealthErrorDetail = null;
  let lastHealthSuccessPid = null;
  let lastHealthStateSource = 'health_poll_initialise';
  let latestHealthDiagnostics = null;
  let latestHealthTelemetry = null;
  let lastHealthTelemetryCopyAttempt = null;
  let lastBusyState = null;
  let unsubscribeBackgroundTaskUpdates = null;
  let uptimeTelemetryCopyFeedbackTimerId = null;

  function publishHealthPollDiagnostics(diagnostics) {
    if (!diagnostics || typeof diagnostics !== 'object') return;
    latestHealthDiagnostics = diagnostics;
    try {
      window.__vonHealthPollDiagnostics = diagnostics;
    } catch (_) {
      // Ignore non-writable globals in constrained environments.
    }
    try {
      document.dispatchEvent(new CustomEvent('von:healthPollDiagnostics', { detail: diagnostics }));
    } catch (_) {
      // Non-fatal diagnostic event.
    }
  }

  function setServerReachableState(isReachable) {
    serverReachable = (typeof isReachable === 'boolean') ? isReachable : null;
    setFooterServerReachability(serverReachable);
  }

  function setServerHealthUiState(nextState, diagnostics = null) {
    const safeState = (nextState === 'healthy' || nextState === 'waiting' || nextState === 'degraded' || nextState === 'down')
      ? nextState
      : 'waiting';
    serverHealthUiState = safeState;
    if (safeState === 'down') {
      setServerReachableState(false);
    } else if (safeState === 'waiting') {
      setServerReachableState(null);
    } else {
      setServerReachableState(true);
    }
    if (diagnostics && typeof diagnostics === 'object') {
      const diagnosticsPayload = { state: safeState, ...diagnostics };
      if (typeof diagnosticsPayload.source === 'string' && diagnosticsPayload.source.trim()) {
        lastHealthStateSource = diagnosticsPayload.source.trim();
      }
      publishHealthPollDiagnostics(diagnosticsPayload);
    }
    publishHealthTelemetry('health_state_transition');
  }

  function isThinkingActive() {
    try {
      const wrapper = document.getElementById('thinkingCardWrapper');
      if (wrapper) {
        return wrapper.getAttribute('aria-hidden') !== 'true';
      }
      const loadingIndicator = document.getElementById('loadingIndicator');
      if (loadingIndicator) {
        return loadingIndicator.style.display !== 'none';
      }
    } catch (_) {
      // Ignore transient DOM read issues.
    }
    return false;
  }

  setServerHealthUiState('waiting', {
    failureCount: 0,
    failureWindowMs: 0,
    downFailureThreshold: null,
    downFailureWindowThresholdMs: null,
    hasSeenSuccessfulHealthPoll: false,
    isThinkingActive: false,
    source: 'health_poll_initialise'
  });

  function autoReloadEnabled() {
    try { return localStorage.getItem('von:autoReloadOnRestart') === '1'; } catch (_) { return false; }
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

  function getBrowserOnlineState() {
    try {
      return (typeof navigator !== 'undefined' && typeof navigator.onLine === 'boolean')
        ? navigator.onLine
        : null;
    } catch (_) {
      return null;
    }
  }

  function publishHealthTelemetry(eventSource = null) {
    const locationHref = (typeof window !== 'undefined' && window.location)
      ? (window.location.href || null)
      : null;
    const locationOrigin = (typeof window !== 'undefined' && window.location)
      ? (window.location.origin || null)
      : null;
    const locationPathname = (typeof window !== 'undefined' && window.location)
      ? (window.location.pathname || null)
      : null;
    const locationPort = (typeof window !== 'undefined' && window.location)
      ? (window.location.port || null)
      : null;
    const realtimeConnectionTelemetry = (typeof window !== 'undefined' && window.__vonRealtimeConnectionTelemetry && typeof window.__vonRealtimeConnectionTelemetry === 'object')
      ? window.__vonRealtimeConnectionTelemetry
      : null;
    const snapshot = buildHealthTelemetrySnapshot({
      state: serverHealthUiState,
      eventSource,
      stateSource: lastHealthStateSource,
      nowMs: Date.now(),
      serverReachable,
      hasSeenSuccessfulHealthPoll,
      failureCount,
      firstFailureAtMs,
      lastHealthCheckCompletedAtMs,
      lastHealthSuccessAtMs,
      lastHealthSuccessPid,
      lastErrorKind: lastHealthErrorKind,
      lastErrorDetail: lastHealthErrorDetail,
      diagnostics: latestHealthDiagnostics,
      pollInFlight: healthPollInFlight,
      pollQueuedImmediate: healthPollQueuedImmediate,
      nextPollAtMs: nextScheduledHealthPollAtMs,
      browserOnline: getBrowserOnlineState(),
      isVontologyBusy: !!lastBusyState,
      healthLoopStartedAtMs,
      lastCopyAttempt: lastHealthTelemetryCopyAttempt,
      realtimeConnectionTelemetry,
      locationHref,
      locationOrigin,
      locationPathname,
      locationPort,
    });
    latestHealthTelemetry = snapshot;
    try {
      window.__vonHealthTelemetry = snapshot;
    } catch (_) {
      // Ignore non-writable globals in constrained environments.
    }
    try {
      document.dispatchEvent(new CustomEvent('von:healthTelemetryUpdated', { detail: snapshot }));
    } catch (_) {
      // Non-fatal telemetry event.
    }
    return snapshot;
  }
  function formatSecondsAgo(ms) {
    if (!Number.isFinite(ms) || ms < 0) return null;
    return `${Math.max(0, Math.floor(ms / 1000))}s ago`;
  }

  function canCopyHealthTelemetryFromUptime() {
    return serverHealthUiState === 'down' || serverHealthUiState === 'degraded' || serverHealthUiState === 'waiting';
  }

  function updateUptimeCopyFeedbackClass(copied) {
    if (!uptimeSpan) return;
    uptimeSpan.classList.remove('health-telemetry-copy-success', 'health-telemetry-copy-error');
    uptimeSpan.classList.add(copied ? 'health-telemetry-copy-success' : 'health-telemetry-copy-error');
    if (uptimeTelemetryCopyFeedbackTimerId) {
      clearTimeout(uptimeTelemetryCopyFeedbackTimerId);
    }
    uptimeTelemetryCopyFeedbackTimerId = setTimeout(() => {
      uptimeTelemetryCopyFeedbackTimerId = null;
      if (uptimeSpan) {
        uptimeSpan.classList.remove('health-telemetry-copy-success', 'health-telemetry-copy-error');
      }
    }, 1800);
  }

  async function copyCurrentHealthTelemetry(copySource = 'uptime_badge_click') {
    const attemptStartedAtMs = Date.now();
    lastHealthTelemetryCopyAttempt = {
      copySource,
      attemptedAtMs: attemptStartedAtMs,
      attemptedAtIso: new Date(attemptStartedAtMs).toISOString(),
      copied: null,
    };
    const snapshot = publishHealthTelemetry('health_telemetry_copy_attempted');
    const snapshotForCopy = (latestHealthTelemetry && typeof latestHealthTelemetry === 'object')
      ? latestHealthTelemetry
      : snapshot;
    const payload = buildHealthTelemetryCopyPayload(snapshotForCopy, {
      copySource,
      nowMs: attemptStartedAtMs,
    });
    const payloadText = JSON.stringify(payload, null, 2);
    try {
      window.__vonLastHealthTelemetryCopyPayload = payload;
    } catch (_) {
      // Ignore non-writable globals.
    }
    let copied = false;
    try {
      copied = await copyTextWithClipboardFallback(payloadText);
    } catch (_) {
      // Ignore copy failures and record them below.
    }
    const attemptCompletedAtMs = Date.now();
    lastHealthTelemetryCopyAttempt = {
      ...lastHealthTelemetryCopyAttempt,
      copied,
      completedAtMs: attemptCompletedAtMs,
      completedAtIso: new Date(attemptCompletedAtMs).toISOString(),
      copyPayloadBytes: payloadText.length,
    };
    publishHealthTelemetry(copied ? 'health_telemetry_copy_succeeded' : 'health_telemetry_copy_failed');
    updateUptimeCopyFeedbackClass(copied);
    return copied;
  }

  function updateUptimeLoop() {
    if (uptimeSpan) {
      const uptimeContainer = uptimeSpan.parentElement;
      const telemetryCopyEnabled = canCopyHealthTelemetryFromUptime();
      const copyHint = telemetryCopyEnabled ? 'Click to copy health telemetry JSON' : null;
      uptimeSpan.classList.toggle('health-telemetry-copyable', telemetryCopyEnabled);
      uptimeSpan.setAttribute('aria-disabled', telemetryCopyEnabled ? 'false' : 'true');
      if (serverHealthUiState === 'down') {
        const lastCheckAgeMs = Number.isFinite(lastHealthCheckCompletedAtMs)
          ? Math.max(0, Date.now() - lastHealthCheckCompletedAtMs)
          : null;
        const lastCheckLabel = formatSecondsAgo(lastCheckAgeMs);
        const lastSuccessLabel = Number.isFinite(lastHealthSuccessAtMs)
          ? formatUptime(Math.max(0, Date.now() - lastHealthSuccessAtMs))
          : null;
        uptimeSpan.textContent = lastCheckLabel
          ? `server down (checked ${lastCheckLabel})`
          : 'server down';
        const baseTitle = lastSuccessLabel
          ? `Von server is unreachable | Last healthy response ${lastSuccessLabel} ago`
          : 'Von server is unreachable';
        uptimeSpan.title = copyHint ? `${baseTitle} | ${copyHint}` : baseTitle;
        if (uptimeContainer) uptimeContainer.classList.add('pid-error');
      } else if (serverHealthUiState === 'waiting') {
        uptimeSpan.textContent = 'waiting for server';
        const baseTitle = 'Waiting for initial server health response';
        uptimeSpan.title = copyHint ? `${baseTitle} | ${copyHint}` : baseTitle;
        if (uptimeContainer) uptimeContainer.classList.remove('pid-error');
      } else if (serverHealthUiState === 'degraded') {
        const failureCountTitle = Number.isFinite(latestHealthDiagnostics?.failureCount)
          ? `consecutive failures=${latestHealthDiagnostics.failureCount}`
          : null;
        const failureWindowTitle = Number.isFinite(latestHealthDiagnostics?.failureWindowMs)
          ? `failure window=${Math.round(latestHealthDiagnostics.failureWindowMs / 1000)}s`
          : null;
        const detail = [failureCountTitle, failureWindowTitle].filter(Boolean).join(' | ');
        uptimeSpan.textContent = 'degraded (retrying)';
        const baseTitle = detail
          ? `Health probes are failing but below down threshold (${detail})`
          : 'Health probes are failing but below down threshold';
        uptimeSpan.title = copyHint ? `${baseTitle} | ${copyHint}` : baseTitle;
        if (uptimeContainer) uptimeContainer.classList.remove('pid-error');
      } else if (startTimeIso) {
        const started = Date.parse(startTimeIso);
        if (!isNaN(started)) {
          const diff = Date.now() - started;
          uptimeSpan.textContent = formatUptime(diff);
          uptimeSpan.title = 'Process uptime';
          if (uptimeContainer) uptimeContainer.classList.remove('pid-error');
        }
      } else {
        const estimate = Date.now() - healthLoopStartedAtMs;
        uptimeSpan.textContent = `~${formatUptime(estimate)}`;
        uptimeSpan.title = 'Awaiting server health response; showing local session age estimate';
        if (uptimeContainer) uptimeContainer.classList.remove('pid-error');
      }
    }
    requestAnimationFrame(() => setTimeout(updateUptimeLoop, 1000));
  }

  function updateBackgroundTaskFooter(snapshot) {
    if (!backgroundTaskEl) return;
    const activeTasks = Array.isArray(snapshot?.active) ? snapshot.active : [];
    if (!activeTasks.length) {
      backgroundTaskEl.classList.remove('active');
      backgroundTaskEl.textContent = '';
      backgroundTaskEl.title = '';
      return;
    }
    backgroundTaskEl.textContent = formatBackgroundTaskSummary(activeTasks, { maxLabels: 1 });
    backgroundTaskEl.title = formatBackgroundTaskTooltip(activeTasks);
    backgroundTaskEl.classList.add('active');
  }

  function updateBusyIndicator() {
    if (!busyEl) return;
    try {
      const busy = isVontologyBusy();
      if (busy) {
        busyEl.classList.add('active');
        busyEl.style.display = 'inline';
        if (busySr) busySr.textContent = 'Vontology operations in progress';
      } else {
        busyEl.classList.remove('active');
        busyEl.style.display = 'none';
        if (busySr) busySr.textContent = '';
      }
      if (lastBusyState !== busy) {
        lastBusyState = busy;
        document.dispatchEvent(new CustomEvent('von:vontologyBusyChange', { detail: { busy } }));
        publishHealthTelemetry('vontology_busy_changed');
      }
    } catch (_) { }
  }

  if (backgroundTaskEl) {
    try {
      unsubscribeBackgroundTaskUpdates = subscribeBackgroundTaskUpdates((snapshot) => {
        updateBackgroundTaskFooter(snapshot);
      });
    } catch (_) {
      updateBackgroundTaskFooter(null);
    }
  }

  try {
    window.addEventListener('beforeunload', () => {
      try {
        if (unsubscribeBackgroundTaskUpdates) {
          unsubscribeBackgroundTaskUpdates();
          unsubscribeBackgroundTaskUpdates = null;
        }
      } catch (_) { }
    }, { once: true });
  } catch (_) { }

  function scheduleHealthPoll(delayMs) {
    const safeDelayMs = Number.isFinite(delayMs) ? Math.max(0, Math.trunc(delayMs)) : 0;
    if (healthPollTimerId) {
      clearTimeout(healthPollTimerId);
    }
    nextScheduledHealthPollAtMs = Date.now() + safeDelayMs;
    publishHealthTelemetry('health_poll_scheduled');
    healthPollTimerId = setTimeout(() => {
      healthPollTimerId = null;
      nextScheduledHealthPollAtMs = null;
      publishHealthTelemetry('health_poll_schedule_fired');
      void poll();
    }, safeDelayMs);
  }

  async function poll() {
    if (healthPollInFlight) {
      healthPollQueuedImmediate = true;
      publishHealthTelemetry('health_poll_queued_immediate');
      return;
    }

    healthPollInFlight = true;
    nextScheduledHealthPollAtMs = null;
    publishHealthTelemetry('health_poll_started');
    let busy = false;
    try {
      updateBusyIndicator();
      busy = isVontologyBusy();
    } catch (_) {
      // Ignore best-effort busy indicator failures.
    }
    let nextDelay = 5000; // base
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 8000);
      const res = await fetch('/health', { cache: 'no-store', signal: controller.signal });
      clearTimeout(timeout);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      lastHealthCheckCompletedAtMs = Date.now();
      hasSeenSuccessfulHealthPoll = true;
      firstFailureAtMs = null;
      lastHealthSuccessAtMs = Date.now();
      lastHealthErrorKind = null;
      lastHealthErrorDetail = null;
      const successState = evaluateServerHealthState({
        hasSeenSuccessfulHealthPoll,
        failureCount: 0,
        firstFailureAtMs: null,
        nowMs: lastHealthSuccessAtMs,
        isThinkingActive: isThinkingActive(),
      });
      setServerHealthUiState('healthy', {
        ...successState.diagnostics,
        source: 'health_poll_success',
        lastSuccessAgeMs: 0,
        lastErrorKind: null,
        lastErrorDetail: null,
      });
      const newPid = (typeof data.pid !== 'undefined') ? data.pid : null;
      lastHealthSuccessPid = Number.isFinite(Number(newPid)) ? Number(newPid) : null;
      const newStart = data.start_time || null;
      try {
        document.dispatchEvent(new CustomEvent('von:runtimeIdentityUpdated', {
          detail: {
            pid: lastHealthSuccessPid,
            start_time: typeof newStart === 'string' && newStart.trim() ? newStart.trim() : null
          }
        }));
      } catch (_) {
        // Runtime identity is a support signal; health polling remains authoritative.
      }
      const newLocalIp = data.local_ip || null;
      const newPublicIp = data.public_ip || null;
      const ragPending = (typeof data.rag_pending_count !== 'undefined') ? data.rag_pending_count : null;
      const versionDetails = (data.version_details && typeof data.version_details === 'object')
        ? data.version_details
        : {};
      const version = (typeof data.version === 'string' && data.version.trim())
        ? data.version.trim()
        : '';
      const shortCommit = (typeof versionDetails.git_short_commit === 'string')
        ? versionDetails.git_short_commit.trim()
        : '';
      const commitTimestamp = (typeof versionDetails.git_commit_timestamp === 'string')
        ? versionDetails.git_commit_timestamp.trim()
        : '';
      const formatCompactBuildTime = (timestamp) => {
        const parsedTime = timestamp ? new Date(timestamp) : null;
        return parsedTime && !Number.isNaN(parsedTime.getTime())
          ? new Intl.DateTimeFormat('en-NZ', {
            day: '2-digit', month: 'short', year: '2-digit',
            hour: '2-digit', minute: '2-digit', hour12: false
          }).format(parsedTime).replace(',', '')
          : '';
      };
      const compactCommitTime = formatCompactBuildTime(commitTimestamp);
      const compactRuntimeStart = formatCompactBuildTime(newStart);
      const isExperimentalBuild = versionDetails.git_dirty === true;
      const compactBuildId = shortCommit ? shortCommit.slice(0, 8) : version;
      const compactBaseBuildId = shortCommit ? `base ${shortCommit.slice(0, 8)}` : '';

      if (buildInfo && buildSpan) {
        const buildText = isExperimentalBuild
          ? ['Experimental', compactRuntimeStart, compactBaseBuildId].filter(Boolean).join(' · ')
          : [compactBuildId, compactCommitTime].filter(Boolean).join(' · ');
        buildSpan.textContent = buildText;
        buildInfo.hidden = !buildText;
        buildInfo.title = (isExperimentalBuild
          ? [
            'Experimental runtime',
            newStart ? `Runtime start ${newStart}` : null,
            version ? `Build version ${version}` : null,
            versionDetails.git_branch ? `Branch ${versionDetails.git_branch}` : null,
            shortCommit ? `Base commit ${shortCommit}` : null,
            commitTimestamp ? `Base commit time ${commitTimestamp}` : null,
            'Working tree dirty'
          ]
          : [
            version ? `Build ${version}` : null,
            shortCommit ? `Commit ${shortCommit}` : null,
            commitTimestamp ? `Commit time ${commitTimestamp}` : null
          ]).filter(Boolean).join(' | ');
      }

      if (localIpSpan) {
        localIpSpan.textContent = newLocalIp || '?';
      }
      if (publicIpSpan) {
        publicIpSpan.textContent = newPublicIp || '—';
      }
      if (ragSpan) {
        // Prefer detailed counts from /admin/rag_status; fallback to /health pending only
        try {
          const controller2 = new AbortController();
          const timeout2 = setTimeout(() => controller2.abort(), 5000);
          const ns = getCurrentNamespace();
          const res2 = await fetch(ns ? (`/admin/rag_status?namespace=${encodeURIComponent(ns)}`) : '/admin/rag_status', { cache: 'no-store', signal: controller2.signal });
          clearTimeout(timeout2);
          if (res2.ok) {
            const rs = await res2.json();
            if (typeof rs.pending === 'number' && typeof rs.indexed === 'number') {
              const p = rs.pending;
              const i = rs.indexed;
              const f = (typeof rs.failed === 'number') ? rs.failed : 0;
              const sessNs = rs.session_namespace || null;
              const chSessions = (typeof rs.chat_history_sessions === 'number') ? rs.chat_history_sessions : 0;
              const chMessages = (typeof rs.chat_history_messages === 'number') ? rs.chat_history_messages : 0;
              const chOk = (typeof rs.chat_history_rag_success === 'number') ? rs.chat_history_rag_success : 0;
              const chFail = (typeof rs.chat_history_rag_failed === 'number') ? rs.chat_history_rag_failed : 0;
              const titleParts = [
                `KA sessions indexed=${i}`,
                `pending=${p}`,
                `failed=${f}`
              ];
              if (sessNs) {
                titleParts.push(`session_ns=${sessNs}`);
              }
              if (chSessions || chMessages || chOk || chFail) {
                titleParts.push(`Conversation sessions=${chSessions}`);
                titleParts.push(`messages=${chMessages}`);
                titleParts.push(`indexed=${chOk}`);
                titleParts.push(`failed=${chFail}`);
              }
              const title = titleParts.join(' | ');
              if (p === 0) {
                if (chSessions || chMessages || chOk || chFail) {
                  if (chFail > 0) {
                    ragSpan.textContent = `KA ${i} • Conversations ${chOk}/${chFail} failed`;
                  } else {
                    ragSpan.textContent = `KA ${i} • Conversations ${chOk}`;
                  }
                } else {
                  ragSpan.textContent = `KA ${i}`;
                }
                ragSpan.title = title;
                ragSpan.classList.remove('rag-active');
              } else {
                if (chFail > 0) {
                  ragSpan.textContent = `KA ${i} • ${p} pending • Conversations ${chFail} failed`;
                } else {
                  ragSpan.textContent = `KA ${i} • ${p} pending`;
                }
                ragSpan.title = title;
                ragSpan.classList.add('rag-active');
              }

              // If chat has failures, surface as active even when pending is zero
              if (chFail > 0) {
                ragSpan.classList.add('rag-active');
              }
            }
          } else {
            // Fallback to pending-only
            if (ragPending === null || ragPending < 0) {
              ragSpan.textContent = '?';
              ragSpan.title = 'RAG status unavailable';
              ragSpan.classList.remove('rag-active');
            } else if (ragPending === 0) {
              ragSpan.textContent = 'Idle';
              ragSpan.title = 'No pending items to index';
              ragSpan.classList.remove('rag-active');
            } else {
              ragSpan.textContent = `${ragPending} pending`;
              ragSpan.title = `${ragPending} items waiting for indexing`;
              ragSpan.classList.add('rag-active');
            }
          }
        } catch (_) {
          // Fallback to pending-only
          if (ragPending === null || ragPending < 0) {
            ragSpan.textContent = '?';
            ragSpan.title = 'RAG status unavailable';
            ragSpan.classList.remove('rag-active');
          } else if (ragPending === 0) {
            ragSpan.textContent = 'Idle';
            ragSpan.title = 'No pending items to index';
            ragSpan.classList.remove('rag-active');
          } else {
            ragSpan.textContent = `${ragPending} pending`;
            ragSpan.title = `${ragPending} items waiting for indexing`;
            ragSpan.classList.add('rag-active');
          }
        }
      }

      // Hook up View details button once (idempotent)
      if (ragDetailsBtn && ragModal && ragModalBody && !ragDetailsBtn._wired) {
        ragDetailsBtn._wired = true;
        ragDetailsBtn.addEventListener('click', async () => {
          try {
            ragModal.classList.add('open');
            ragModal.setAttribute('aria-hidden', 'false');
            ragModalBody.innerHTML = '<p>Loading…</p>';
            const existingActions = ragModal.querySelector('.modal-actions');
            if (existingActions && existingActions._ragCopyBtn instanceof HTMLButtonElement) {
              resetCopyJsonButtonPreCopyState(existingActions._ragCopyBtn);
            }

            if (ragRuntimeHint) {
              ragRuntimeHint.textContent = 'Runtime: loading…';
              // Do not block status rendering on this diagnostic call.
              void (async () => {
                try {
                  const controllerRt = new AbortController();
                  const timeoutRt = setTimeout(() => controllerRt.abort(), 8000);
                  const nsRt = getCurrentNamespace();
                  const urlRt = nsRt
                    ? (`/admin/rag_runtime?namespace=${encodeURIComponent(nsRt)}`)
                    : '/admin/rag_runtime';
                  const resRt = await fetch(urlRt, { cache: 'no-store', signal: controllerRt.signal });
                  clearTimeout(timeoutRt);

                  if (resRt.ok) {
                    const rt = await resRt.json();
                    try { window.__vonLastRagRuntimeJson = rt; } catch (_) { /* ignore */ }

                    const serviceInitialised = (rt && typeof rt.service_initialised === 'boolean') ? rt.service_initialised : null;
                    const backend = rt?.backend?.class_name || (serviceInitialised === false ? 'not initialised' : '—');
                    const embedder = rt?.embedder?.class_name || (serviceInitialised === false ? '—' : '—');
                    const embedModel = rt?.embedder?.model_name || rt?.embedder?.model || null;
                    const lastMs = (typeof rt?.last_query?.elapsed_ms === 'number') ? rt.last_query.elapsed_ms : null;
                    const lastAt = (typeof rt?.last_query?.timestamp === 'string') ? rt.last_query.timestamp : null;
                    const openaiKey = (typeof rt?.openai_key_present === 'boolean') ? rt.openai_key_present : null;

                    const parts = [];
                    parts.push(`Backend: ${backend}`);
                    if (serviceInitialised !== false) {
                      parts.push(`Embedder: ${embedder}${embedModel ? ` (${embedModel})` : ''}`);
                    }
                    if (lastMs !== null) {
                      parts.push(`Last search: ${lastMs}ms${lastAt ? ` @ ${lastAt}` : ''}`);
                    }
                    if (openaiKey === true) {
                      parts.push('OpenAI key: present');
                    } else if (openaiKey === false) {
                      parts.push('OpenAI key: not set');
                    }

                    ragRuntimeHint.textContent = parts.join(' • ');
                  } else {
                    ragRuntimeHint.textContent = 'Runtime: unavailable';
                  }
                } catch (e) {
                  const name = e && e.name ? e.name : '';
                  ragRuntimeHint.textContent = (name === 'AbortError')
                    ? 'Runtime: timed out'
                    : 'Runtime: unavailable';
                }
              })();
            }

            // Prepare modal action buttons (idempotent)
            const modalActions = ragModal.querySelector('.modal-actions');
            if (modalActions && !modalActions._ragExtrasWired) {
              modalActions._ragExtrasWired = true;

              function formatDuration(ms) {
                const safeMs = (typeof ms === 'number' && ms >= 0) ? ms : 0;
                const totalSeconds = Math.round(safeMs / 1000);
                const s = totalSeconds % 60;
                const totalMinutes = Math.floor(totalSeconds / 60);
                const m = totalMinutes % 60;
                const h = Math.floor(totalMinutes / 60);
                if (h > 0) return `${h}h ${m}m ${s}s`;
                if (m > 0) return `${m}m ${s}s`;
                return `${s}s`;
              }

              async function runChatHistoryReindex({ forcedSessionId = null, forcedChunkStart = 0, forceSkipConfirm = false } = {}) {
                const modalActions = ragModal.querySelector('.modal-actions');
                const activeNs = (modalActions && modalActions._ragActiveNamespace) ? modalActions._ragActiveNamespace : '';
                const ns = activeNs || getCurrentNamespace();

                const confirmMsg = forcedSessionId
                  ? `Resume conversation history reindex for namespace:\n\n${ns || '(no namespace)'}\n\nSession:\n${forcedSessionId}\n\nThis may take a few minutes.`
                  : `Reindex conversation history for namespace:\n\n${ns || '(no namespace)'}\n\nThis may take a few minutes.`;
                if (!forceSkipConfirm) {
                  const ok = window.confirm(confirmMsg);
                  if (!ok) return;
                }

                // Build a target list from the last-loaded detailed status so we can show progress and
                // identify exactly which session fails.
                const status = window.__vonLastRagStatusJson || null;
                const details = Array.isArray(status?.chat_history_session_details)
                  ? status.chat_history_session_details
                  : [];

                let targets = details
                  .filter(d => (d && typeof d.session_id === 'string' && d.session_id) && (typeof d.messages_missing_index === 'number') && d.messages_missing_index > 0)
                  .sort((a, b) => (b.messages_missing_index || 0) - (a.messages_missing_index || 0));

                if (forcedSessionId) {
                  targets = targets.filter(t => t && t.session_id === forcedSessionId);
                }

                if (!targets.length) {
                  ragModalBody.innerHTML = ragModalBody.innerHTML + '<hr/><p><em>No missing conversation history messages detected to reindex.</em></p>';
                  return;
                }

                const totalMissingPlanned = targets.reduce((sum, t) => sum + (t?.messages_missing_index || 0), 0);
                let elapsedMsTotal = 0;
                let processedTotal = 0;

                const progressToken = Date.now();
                const progressId = `ragReindexProgress_${progressToken}`;
                const progressTextId = `ragReindexProgressText_${progressToken}`;
                const msgProgressId = `ragReindexMsgProgress_${progressToken}`;
                const msgProgressTextId = `ragReindexMsgProgressText_${progressToken}`;
                ragModalBody.innerHTML = ragModalBody.innerHTML + [
                  '<hr/>',
                  '<h3>Conversation history reindex</h3>',
                  `<p><em>Reindex running…</em></p>`,
                  `<p><strong>Namespace:</strong> ${escapeHtml(ns || '(no namespace)')}</p>`,
                  `<p><strong>Sessions to reindex:</strong> ${targets.length}</p>`,
                  `<progress id="${progressId}" max="${targets.length}" value="0" style="width:100%;"></progress>`,
                  `<div id="${progressTextId}" style="margin-top:6px;"></div>`,
                  `<progress id="${msgProgressId}" max="1" value="0" style="width:100%;margin-top:10px;"></progress>`,
                  `<div id="${msgProgressTextId}" style="margin-top:6px;"></div>`
                ].join('');

                const progressEl = document.getElementById(progressId);
                const progressTextEl = document.getElementById(progressTextId);
                const msgProgressEl = document.getElementById(msgProgressId);
                const msgProgressTextEl = document.getElementById(msgProgressTextId);

                const startedAt = new Date().toISOString();
                const perSessionResults = [];
                const aggregate = {
                  sessions_targeted: targets.length,
                  sessions_completed: 0,
                  messages_indexed_attempted: 0,
                  messages_indexed_success: 0,
                  messages_indexed_failed: 0,
                  errors: []
                };

                for (let i = 0; i < targets.length; i++) {
                  const t = targets[i];
                  const sid = t.session_id;
                  const missing = t.messages_missing_index;

                  if (progressEl) progressEl.value = i;
                  if (progressTextEl) {
                    const etaTxt = (processedTotal > 0 && totalMissingPlanned > 0)
                      ? (() => {
                        const remaining = Math.max(0, totalMissingPlanned - processedTotal);
                        const avgMsPerMsg = elapsedMsTotal / Math.max(1, processedTotal);
                        return ` • ETA ~${formatDuration(remaining * avgMsPerMsg)}`;
                      })()
                      : '';
                    progressTextEl.textContent = `Reindexing session ${i + 1}/${targets.length}: ${sid} (missing ${missing})${etaTxt}`;
                  }

                  // Chunked mode: avoids long requests and gives per-message progress.
                  const url = ns ? (`/admin/chat_history_reindex?namespace=${encodeURIComponent(ns)}`) : '/admin/chat_history_reindex';
                  // Larger sessions can take a long time to embed/index; use smaller chunks.
                  const chunkSize = (missing && missing >= 200) ? 10 : 25;
                  let chunkStart = (forcedSessionId && sid === forcedSessionId && typeof forcedChunkStart === 'number' && forcedChunkStart > 0)
                    ? forcedChunkStart
                    : 0;
                  let chunkNo = 0;
                  let sessionIndexableTotal = null;
                  let sessionProcessed = 0;
                  const sessionChunks = [];

                  while (true) {
                    chunkNo += 1;

                    let res;
                    const chunkAttemptStarted = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
                    const observedMsPerMessage = processedTotal > 0
                      ? (elapsedMsTotal / processedTotal)
                      : null;
                    // This crossing only updates the display. It never cancels or
                    // resubmits a mutation whose outcome is not yet known.
                    const requestAdvisoryMs = observedMsPerMessage === null
                      ? 120000
                      : Math.max(120000, Math.min(15 * 60 * 1000, Math.round(observedMsPerMessage * chunkSize * 2)));
                    let requestAdvisoryExceeded = false;
                    const advisoryR = setTimeout(() => {
                      requestAdvisoryExceeded = true;
                      if (msgProgressTextEl) {
                        const current = String(msgProgressTextEl.textContent || '');
                        const note = 'Still working; this request has not been cancelled or retried.';
                        if (!current.includes(note)) {
                          msgProgressTextEl.textContent = current ? `${current} • ${note}` : note;
                        }
                      }
                    }, requestAdvisoryMs);

                    try {
                      res = await fetch(url, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                          reset_counters: (chunkNo === 1),
                          dry_run: false,
                          session_ids: [sid],
                          chunk_start: chunkStart,
                          chunk_size: chunkSize
                        }),
                        cache: 'no-store'
                      });
                    } catch (err) {
                      const name = err && err.name ? err.name : 'Error';
                      const message = err && err.message ? err.message : '';
                      const failure = {
                        type: 'network_outcome_indeterminate',
                        session_id: sid,
                        chunk_start: chunkStart,
                        chunk_no: chunkNo,
                        name,
                        message,
                        advisory_exceeded: requestAdvisoryExceeded
                      };
                      aggregate.errors.push(failure);
                      perSessionResults.push({ session_id: sid, ok: false, error: failure, chunks: sessionChunks });

                      window.__vonLastRagReindexJson = {
                        status: 'outcome_indeterminate',
                        namespace: ns,
                        started_at: startedAt,
                        indeterminate_session_id: sid,
                        indeterminate_at_session_index: i,
                        indeterminate_chunk_no: chunkNo,
                        indeterminate_chunk_start: chunkStart,
                        per_session_results: perSessionResults,
                        aggregate
                      };

                      ragModalBody.innerHTML = ragModalBody.innerHTML + `<hr/><p><strong>Reindex response unavailable.</strong> Session ${escapeHtml(sid)} (chunk ${chunkNo}, start ${chunkStart}).</p><p><em>The request may or may not have completed. Reopen this status to inspect canonical progress before starting another reindex.</em></p>`;
                      return;
                    } finally {
                      clearTimeout(advisoryR);
                    }

                    if (!res.ok) {
                      const txt = await res.text();
                      const outcomeIndeterminate = res.status >= 500;
                      const err = {
                        type: outcomeIndeterminate ? 'http_outcome_indeterminate' : 'http_error',
                        session_id: sid,
                        chunk_start: chunkStart,
                        chunk_no: chunkNo,
                        http_status: res.status,
                        response_text: txt || ''
                      };
                      aggregate.errors.push(err);
                      perSessionResults.push({ session_id: sid, ok: false, error: err, chunks: sessionChunks });

                      window.__vonLastRagReindexJson = {
                        status: outcomeIndeterminate ? 'outcome_indeterminate' : 'error',
                        namespace: ns,
                        started_at: startedAt,
                        ...(outcomeIndeterminate ? {
                          indeterminate_session_id: sid,
                          indeterminate_at_session_index: i,
                          indeterminate_chunk_no: chunkNo,
                          indeterminate_chunk_start: chunkStart
                        } : {
                          failed_session_id: sid,
                          failed_at_session_index: i,
                          failed_chunk_no: chunkNo,
                          failed_chunk_start: chunkStart
                        }),
                        per_session_results: perSessionResults,
                        aggregate
                      };

                      const heading = outcomeIndeterminate ? 'Reindex result not confirmed.' : 'Reindex failed.';
                      const followUp = outcomeIndeterminate
                        ? '<p><em>The server returned an upstream error, so inspect canonical progress before starting another reindex.</em></p>'
                        : '';
                      ragModalBody.innerHTML = ragModalBody.innerHTML + `<hr/><p><strong>${heading}</strong> Session ${escapeHtml(sid)} (chunk ${chunkNo}, start ${chunkStart}) returned HTTP ${res.status}.</p>${followUp}<pre style="white-space:pre-wrap;max-height:220px;overflow:auto;">${escapeHtml(txt || '')}</pre>`;
                      return;
                    }

                    const js = await res.json();
                    const chunkFinished = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
                    const chunkElapsedMs = Math.max(0, Math.round(chunkFinished - chunkAttemptStarted));
                    sessionChunks.push({
                      ...js,
                      client_elapsed_ms: chunkElapsedMs,
                      client_advisory_ms: requestAdvisoryMs,
                      client_advisory_exceeded: requestAdvisoryExceeded,
                      client_chunk_no: chunkNo
                    });

                    if (typeof js?.indexable_total === 'number') {
                      sessionIndexableTotal = js.indexable_total;
                    }

                    const attempted = (js?.messages_indexed_attempted || 0);
                    const ok = (js?.messages_indexed_success || 0);
                    const fail = (js?.messages_indexed_failed || 0);
                    const errors = Array.isArray(js?.errors) ? js.errors : [];

                    aggregate.messages_indexed_attempted += attempted;
                    aggregate.messages_indexed_success += ok;
                    aggregate.messages_indexed_failed += fail;
                    if (errors.length) {
                      aggregate.errors.push(...errors.map(e => ({ ...e, session_id: e.session_id || sid })));
                    }

                    const chunkProcessed = (ok + fail);
                    sessionProcessed += chunkProcessed;
                    if (chunkProcessed > 0) {
                      processedTotal += chunkProcessed;
                      elapsedMsTotal += chunkElapsedMs;
                    }

                    if (msgProgressEl) {
                      msgProgressEl.max = (sessionIndexableTotal || Math.max(1, (missing || 1)));
                      msgProgressEl.value = Math.min(msgProgressEl.max, sessionProcessed);
                    }
                    if (msgProgressTextEl) {
                      const totalTxt = sessionIndexableTotal ? String(sessionIndexableTotal) : String(missing || '?');
                      const etaTxt = (processedTotal > 0 && totalMissingPlanned > 0)
                        ? (() => {
                          const remaining = Math.max(0, totalMissingPlanned - processedTotal);
                          const avgMsPerMsg = elapsedMsTotal / Math.max(1, processedTotal);
                          return ` • ETA ~${formatDuration(remaining * avgMsPerMsg)}`;
                        })()
                        : '';
                      msgProgressTextEl.textContent = `Session progress: ${sessionProcessed}/${totalTxt} messages (chunk ${chunkNo})${etaTxt}`;
                    }

                    const next = (typeof js?.next_chunk_start === 'number') ? js.next_chunk_start : null;
                    const done = !!js?.done;
                    if (done || next === null || next === chunkStart) {
                      break;
                    }
                    chunkStart = next;
                  }

                  aggregate.sessions_completed += 1;
                  perSessionResults.push({ session_id: sid, ok: true, chunks: sessionChunks });
                }

                if (progressEl) progressEl.value = targets.length;
                if (progressTextEl) progressTextEl.textContent = `Completed ${targets.length}/${targets.length} sessions.`;
                if (msgProgressEl) {
                  msgProgressEl.value = msgProgressEl.max;
                }
                if (msgProgressTextEl) {
                  msgProgressTextEl.textContent = 'Session progress: complete.';
                }

                const finishedAt = new Date().toISOString();
                window.__vonLastRagReindexJson = {
                  status: 'ok',
                  namespace: ns,
                  started_at: startedAt,
                  finished_at: finishedAt,
                  per_session_results: perSessionResults,
                  aggregate
                };

                const summary = [
                  '<hr/>',
                  '<h3>Conversation history reindex result</h3>',
                  '<ul>',
                  `<li><strong>Sessions targeted:</strong> ${aggregate.sessions_targeted}</li>`,
                  `<li><strong>Sessions completed:</strong> ${aggregate.sessions_completed}</li>`,
                  `<li><strong>Messages attempted:</strong> ${aggregate.messages_indexed_attempted}</li>`,
                  `<li><strong>Messages indexed:</strong> ${aggregate.messages_indexed_success}</li>`,
                  `<li><strong>Messages failed:</strong> ${aggregate.messages_indexed_failed}</li>`,
                  `<li><strong>Total errors recorded:</strong> ${aggregate.errors.length}</li>`,
                  '</ul>',
                  '<p><em>Tip: reopen this modal to refresh the status table.</em></p>'
                ].join('');
                ragModalBody.innerHTML = ragModalBody.innerHTML + summary;
              }

              const copyBtn = document.createElement('button');
              copyBtn.className = 'btn-mini';
              copyBtn.textContent = 'Copy JSON';
              copyBtn.title = 'Copy the raw RAG status JSON payload (and last reindex/backfill action, if any)';
              resetCopyJsonButtonPreCopyState(copyBtn);
              copyBtn.addEventListener('click', async () => {
                try {
                  const payload = {
                    rag_status: window.__vonLastRagStatusJson || null,
                    last_chat_history_backfill: window.__vonLastRagBackfillJson || null,
                    last_chat_history_reindex: window.__vonLastRagReindexJson || null,
                  };
                  const txt = JSON.stringify(payload, null, 2);
                  if (!txt) {
                    ragModalBody.innerHTML = ragModalBody.innerHTML + '<hr/><p><em>No JSON payload available yet. Open the modal again after it loads.</em></p>';
                    return;
                  }
                  const copied = await copyJsonTextWithButtonFeedback(copyBtn, txt);
                  if (copied) {
                    ragModalBody.innerHTML = ragModalBody.innerHTML + '<hr/><p><em>Copied JSON to clipboard.</em></p>';
                  } else {
                    ragModalBody.innerHTML = ragModalBody.innerHTML + '<hr/><p><em>Copy failed.</em></p>';
                  }
                } catch (_) {
                  ragModalBody.innerHTML = ragModalBody.innerHTML + '<hr/><p><em>Copy failed.</em></p>';
                }
              });

              const reindexBtn = document.createElement('button');
              reindexBtn.className = 'btn-mini';
              reindexBtn.textContent = 'Reindex conversation history';
              reindexBtn.title = 'Reindex conversation history messages into RAG for your current namespace';
              reindexBtn.hidden = true;
              reindexBtn.addEventListener('click', async () => {
                try {
                  reindexBtn.disabled = true;
                  if (modalActions) {
                    modalActions._ragActionInProgress = true;
                  }
                  await runChatHistoryReindex();

                } catch (e) {
                  const name = e && e.name ? e.name : 'Error';
                  const msg = e && e.message ? e.message : '';
                  const errObj = { type: 'exception_outcome_indeterminate', name, message: msg };
                  window.__vonLastRagReindexJson = {
                    status: 'outcome_indeterminate',
                    namespace: getCurrentNamespace() || null,
                    error: errObj
                  };
                  ragModalBody.innerHTML = ragModalBody.innerHTML + `<hr/><p><strong>Reindex result not confirmed.</strong> ${escapeHtml(name)}${msg ? `: ${escapeHtml(msg)}` : ''}</p><p><em>The request may have completed. Inspect canonical progress before starting another reindex; “Copy JSON” captures this indeterminate outcome.</em></p>`;
                } finally {
                  reindexBtn.disabled = false;
                  try {
                    const modalActions = ragModal.querySelector('.modal-actions');
                    if (modalActions) {
                      modalActions._ragActionInProgress = false;
                    }
                  } catch (_) { /* ignore */ }
                }
              });

              const resumeBtn = document.createElement('button');
              resumeBtn.className = 'btn-mini';
              resumeBtn.textContent = 'Resume reindex';
              resumeBtn.title = 'Resume conversation history reindex from the last recorded failure (session + chunk start)';
              resumeBtn.hidden = true;
              resumeBtn.addEventListener('click', async () => {
                try {
                  const last = window.__vonLastRagReindexJson || null;
                  const sid = (last && last.status === 'error') ? (last.failed_session_id || null) : null;
                  const chunkStart = (last && last.status === 'error' && typeof last.failed_chunk_start === 'number') ? last.failed_chunk_start : 0;
                  if (!sid) {
                    ragModalBody.innerHTML = ragModalBody.innerHTML + '<hr/><p><em>No resumable reindex failure recorded yet.</em></p>';
                    return;
                  }

                  resumeBtn.disabled = true;
                  if (modalActions) {
                    modalActions._ragActionInProgress = true;
                  }
                  await runChatHistoryReindex({ forcedSessionId: sid, forcedChunkStart: chunkStart });
                } catch (e) {
                  const name = e && e.name ? e.name : 'Error';
                  const msg = e && e.message ? e.message : '';
                  window.__vonLastRagReindexJson = {
                    status: 'outcome_indeterminate',
                    namespace: getCurrentNamespace() || null,
                    error: { type: 'exception_outcome_indeterminate', name, message: msg }
                  };
                  ragModalBody.innerHTML = ragModalBody.innerHTML + `<hr/><p><strong>Resume result not confirmed.</strong> ${escapeHtml(name)}${msg ? `: ${escapeHtml(msg)}` : ''}</p><p><em>The request may have completed. Inspect canonical progress before trying again.</em></p>`;
                } finally {
                  resumeBtn.disabled = false;
                  try {
                    const modalActions = ragModal.querySelector('.modal-actions');
                    if (modalActions) {
                      modalActions._ragActionInProgress = false;
                    }
                  } catch (_) { /* ignore */ }
                }
              });

              modalActions.appendChild(copyBtn);
              modalActions.appendChild(reindexBtn);
              modalActions.appendChild(resumeBtn);
              modalActions._ragCopyBtn = copyBtn;
              modalActions._ragReindexBtn = reindexBtn;
              modalActions._ragResumeBtn = resumeBtn;
            }

            if (ragChatBackfillBtn) {
              ragChatBackfillBtn.hidden = true;
              ragChatBackfillBtn.disabled = false;
              ragChatBackfillBtn.title = 'Associate legacy conversation history with your current namespace and re-index to RAG';
            }

            const controller3 = new AbortController();
            const timeout3 = setTimeout(() => controller3.abort(), 15000);
            const ns2 = getCurrentNamespace();
            const url3 = ns2
              ? (`/admin/rag_status?namespace=${encodeURIComponent(ns2)}&detail=1`)
              : '/admin/rag_status';
            let res3 = await fetch(url3, { cache: 'no-store', signal: controller3.signal });
            clearTimeout(timeout3);

            // If a namespaced call fails (e.g., bad localStorage value), retry once without namespace.
            if (!res3.ok && ns2) {
              try {
                res3 = await fetch('/admin/rag_status', { cache: 'no-store' });
              } catch (_) { /* ignore */ }
            }

            if (res3.ok) {
              let rs;
              try {
                rs = await res3.json();
              } catch (_) {
                const txt = await res3.text();
                ragModalBody.innerHTML = `<p>Unable to load detailed status.</p><p><strong>Parse error.</strong> Response was not JSON.</p><pre style="white-space:pre-wrap;max-height:220px;overflow:auto;">${escapeHtml(txt || '')}</pre>`;
                return;
              }

              // Save last JSON for Copy JSON.
              try { window.__vonLastRagStatusJson = rs; } catch (_) { /* ignore */ }
              const backfillReason0 = rs?.chat_history_backfill_reason || null;
              const backfillSessionNs = rs?.chat_history_backfill_session_namespace || null;
              if (backfillReason0 === 'namespace_mismatch' && backfillSessionNs && backfillSessionNs !== ns2) {
                try {
                  localStorage.setItem('current_user_namespace', backfillSessionNs);
                } catch (_) { /* ignore */ }

                try {
                  const controller3b = new AbortController();
                  const timeout3b = setTimeout(() => controller3b.abort(), 8000);
                  const res3b = await fetch(`/admin/rag_status?namespace=${encodeURIComponent(backfillSessionNs)}&detail=1`, { cache: 'no-store', signal: controller3b.signal });
                  clearTimeout(timeout3b);
                  if (res3b.ok) {
                    rs = await res3b.json();
                  }
                } catch (_) { /* ignore */ }
              }
              const scopedSessions = (typeof rs.scoped_sessions === 'number') ? rs.scoped_sessions : null;
              const indexed = (typeof rs.indexed === 'number') ? rs.indexed : 0;
              const pending = (typeof rs.pending === 'number') ? rs.pending : 0;
              const failed = (typeof rs.failed === 'number') ? rs.failed : 0;
              const skipped = (typeof rs.skipped === 'number') ? rs.skipped : 0;
              const sessions = (typeof rs.sessions === 'number') ? rs.sessions : null;
              const interactions = (typeof rs.interactions === 'number') ? rs.interactions : null;
              const eligS = (typeof rs.eligible_sessions === 'number') ? rs.eligible_sessions : null;
              const eligI = (typeof rs.eligible_interactions === 'number') ? rs.eligible_interactions : null;
              const sessionNs = rs.session_namespace || null;
              const requestedNs = (typeof rs.namespace === 'string') ? rs.namespace : (ns2 || null);

              // Keep the modal action namespace aligned with what the server reports.
              // This prevents actions (like reindex) from accidentally using a stale
              // user-only namespace from localStorage.
              try {
                const modalActions = ragModal.querySelector('.modal-actions');
                if (modalActions) {
                  modalActions._ragActiveNamespace = requestedNs || sessionNs || '';
                }
              } catch (_) { /* ignore */ }

              // If we have a composite namespace, prefer it as the persisted current namespace.
              try {
                const prefer = requestedNs || sessionNs;
                if (typeof prefer === 'string' && prefer.includes('@')) {
                  localStorage.setItem('current_user_namespace', prefer);
                }
              } catch (_) { /* ignore */ }
              const sessMissing = (typeof rs.sessions_missing_namespace === 'number') ? rs.sessions_missing_namespace : null;
              const sessOther = (typeof rs.sessions_other_namespace === 'number') ? rs.sessions_other_namespace : null;
              const sessBreakdown = Array.isArray(rs.sessions_namespace_breakdown) ? rs.sessions_namespace_breakdown : [];
              const chSessions = (typeof rs.chat_history_sessions === 'number') ? rs.chat_history_sessions : 0;
              const chMessages = (typeof rs.chat_history_messages === 'number') ? rs.chat_history_messages : 0;
              const chOk = (typeof rs.chat_history_rag_success === 'number') ? rs.chat_history_rag_success : 0;
              const chFail = (typeof rs.chat_history_rag_failed === 'number') ? rs.chat_history_rag_failed : 0;
              const chInNs = (typeof rs.chat_history_sessions_in_namespace === 'number') ? rs.chat_history_sessions_in_namespace : null;
              const chMissingNs = (typeof rs.chat_history_sessions_missing_namespace === 'number') ? rs.chat_history_sessions_missing_namespace : null;
              const chOtherNs = (typeof rs.chat_history_sessions_other_namespace === 'number') ? rs.chat_history_sessions_other_namespace : null;
              const chDetails = Array.isArray(rs.chat_history_session_details) ? rs.chat_history_session_details : null;
              const backfillAvailable = !!rs.chat_history_backfill_available;
              const backfillReason = rs.chat_history_backfill_reason || null;

              const renderBreakdown = () => {
                if (!sessBreakdown.length) {
                  return '';
                }

                const items = sessBreakdown.map((row) => {
                  const nsRaw = (typeof row?.namespace === 'string') ? row.namespace : null;
                  const nsLabel = nsRaw ? nsRaw : '(missing namespace)';
                  const total2 = (typeof row?.total === 'number') ? row.total : 0;
                  const indexed2 = (typeof row?.indexed === 'number') ? row.indexed : 0;
                  const pending2 = (typeof row?.pending === 'number') ? row.pending : 0;
                  const failed2 = (typeof row?.failed === 'number') ? row.failed : 0;
                  const skipped2 = (typeof row?.skipped === 'number') ? row.skipped : 0;
                  const none2 = (typeof row?.none === 'number') ? row.none : 0;
                  const isCurrent = sessionNs && nsRaw === sessionNs;
                  const suffix = isCurrent ? ' (in scope)' : '';
                  return `<li>${escapeHtml(nsLabel)}${escapeHtml(suffix)} — total ${total2} (indexed ${indexed2}, pending ${pending2}, failed ${failed2}, skipped ${skipped2}, none ${none2})</li>`;
                });

                return [
                  '<details>',
                  '<summary>Interaction sessions by namespace</summary>',
                  '<ul>',
                  ...items,
                  '</ul>',
                  '</details>'
                ].join('');
              };

              const html = [
                requestedNs ? `<p><strong>Requested namespace:</strong> ${escapeHtml(requestedNs)}</p>` : '',
                sessionNs ? `<p><strong>Session namespace:</strong> ${escapeHtml(sessionNs)}</p>` : '',
                '<p><em>Note:</em> interaction sessions may be stored under either a user-only namespace (e.g. <code>#V#user</code>) or a composite user@organisation namespace (e.g. <code>#V#user@org</code>). This status view scopes to the requested namespace and may include the user-only namespace for compatibility. Conversation history is scoped by a composite user@organisation namespace.</p>',
                '<ul>',
                `<li><strong>Indexed:</strong> ${indexed}</li>`,
                `<li><strong>Pending:</strong> ${pending}</li>`,
                `<li><strong>Failed:</strong> ${failed}</li>`,
                `<li><strong>Skipped:</strong> ${skipped}</li>`,
                '</ul>',
                '<hr/>',
                '<p>',
                `Sessions: ${sessions ?? '—'}`,
                (scopedSessions !== null ? ` • Sessions in scope: ${scopedSessions}` : ''),
                (sessMissing !== null ? ` • Missing namespace: ${sessMissing}` : ''),
                (sessOther !== null ? ` • Other namespace: ${sessOther}` : ''),
                ` • Interactions: ${interactions ?? '—'} • Eligible sessions: ${eligS ?? '—'} • Eligible interactions: ${eligI ?? '—'}`,
                '</p>'
              ].join('');
              const chatHtml = [
                '<hr/>',
                '<h3>Conversation history</h3>',
                '<ul>',
                (() => {
                  if (chInNs === null && chMissingNs === null && chOtherNs === null) {
                    return `<li><strong>Sessions:</strong> ${chSessions}</li>`;
                  }
                  const parts = [];
                  if (chInNs !== null) parts.push(`${chInNs} in namespace`);
                  if (chMissingNs !== null) parts.push(`${chMissingNs} missing namespace (legacy)`);
                  if (chOtherNs !== null) parts.push(`${chOtherNs} other namespace`);
                  const suffix = parts.length ? ` (${parts.join(' • ')})` : '';
                  return `<li><strong>Sessions:</strong> ${chSessions}${suffix}</li>`;
                })(),
                `<li><strong>Messages (stored):</strong> ${chMessages}</li>`,
                (() => {
                  // Reset markers are stored in history but intentionally excluded from indexing.
                  // Only available when we have per-session details.
                  const details = Array.isArray(chDetails) ? chDetails : null;
                  if (!details || !details.length) return '';
                  const resetMarkers = details.reduce((acc, row) => {
                    const stored = (typeof row?.messages_stored === 'number') ? row.messages_stored : 0;
                    const nonReset = (typeof row?.messages_non_reset === 'number') ? row.messages_non_reset : 0;
                    return acc + Math.max(0, stored - nonReset);
                  }, 0);
                  return `<li><strong>Reset markers:</strong> ${resetMarkers}</li>`;
                })(),
                `<li><strong>Messages indexed:</strong> ${chOk}</li>`,
                `<li><strong>Messages failed:</strong> ${chFail}</li>`,
                '</ul>'
              ].join('');

              const renderChatSessionDetails = () => {
                if (!chDetails || !chDetails.length) return '';

                const rows = chDetails.slice(0, 60).map((row) => {
                  const sid = (typeof row?.session_id === 'string') ? row.session_id : '(unknown session)';
                  const nsRaw = (typeof row?.namespace === 'string') ? row.namespace : '(missing namespace)';
                  const stored = (typeof row?.messages_stored === 'number') ? row.messages_stored : 0;
                  const nonReset = (typeof row?.messages_non_reset === 'number') ? row.messages_non_reset : 0;
                  const resetMarkers = Math.max(0, stored - nonReset);
                  const indexable = (typeof row?.messages_indexable === 'number') ? row.messages_indexable : nonReset;
                  const ok = (typeof row?.rag_indexed_success === 'number') ? row.rag_indexed_success : 0;
                  const fail = (typeof row?.rag_indexed_failed === 'number') ? row.rag_indexed_failed : 0;
                  const indexedTotal = (typeof row?.messages_indexed_total === 'number') ? row.messages_indexed_total : (ok + fail);
                  const missing = (typeof row?.messages_missing_index === 'number') ? row.messages_missing_index : Math.max(0, indexable - indexedTotal);
                  const inScope = (row?.in_namespace === true);
                  const suffix = inScope ? ' (in requested namespace)' : '';
                  return `<li><code>${escapeHtml(sid)}</code> — ${escapeHtml(nsRaw)}${escapeHtml(suffix)}: stored ${stored}, reset markers ${resetMarkers}, non-reset ${nonReset}, indexable ${indexable}, indexed ${indexedTotal} (ok ${ok}, failed ${fail}), missing ${missing}</li>`;
                });

                const truncated = chDetails.length > 60
                  ? `<p><em>Showing 60 of ${chDetails.length} sessions (sorted by missing count).</em></p>`
                  : '';

                return [
                  '<details>',
                  '<summary>Conversation sessions indexing breakdown</summary>',
                  truncated,
                  '<ul>',
                  ...rows,
                  '</ul>',
                  '</details>'
                ].join('');
              };
              const needsBackfill = (chMissingNs && chMissingNs > 0) || (chOtherNs && chOtherNs > 0);
              const backfillNote = (!backfillAvailable && needsBackfill && backfillReason)
                ? `<p><em>Backfill unavailable: ${backfillReason}</em></p>`
                : '';
              ragModalBody.innerHTML = html + renderBreakdown() + chatHtml + renderChatSessionDetails() + backfillNote;

              // Show reindex button when there are missing indexable messages and the
              // user is allowed to run actions for this namespace.
              try {
                const modalActions = ragModal.querySelector('.modal-actions');
                const reindexBtn = modalActions ? modalActions._ragReindexBtn : null;
                const resumeBtn = modalActions ? modalActions._ragResumeBtn : null;
                if (reindexBtn) {
                  const hasMissing = !!(chDetails && chDetails.some(r => (r && typeof r.messages_missing_index === 'number' && r.messages_missing_index > 0)));
                  reindexBtn.hidden = !(backfillAvailable && hasMissing);
                }
                if (resumeBtn) {
                  const last = window.__vonLastRagReindexJson || null;
                  const canResume = !!(
                    backfillAvailable
                    && last
                    && last.status === 'error'
                    && typeof last.failed_session_id === 'string'
                    && last.failed_session_id
                    && typeof last.failed_chunk_start === 'number'
                    && last.failed_chunk_start >= 0
                  );
                  resumeBtn.hidden = !canResume;
                }
              } catch (_) { /* ignore */ }

              if (ragChatBackfillBtn) {
                ragChatBackfillBtn.hidden = !(backfillAvailable && needsBackfill);
                if (!backfillAvailable && backfillReason === 'namespace_mismatch') {
                  ragChatBackfillBtn.title = 'Backfill unavailable: namespace mismatch';
                }
              }
            } else {
              let details = '';
              try {
                const txt = await res3.text();
                details = txt ? `<pre style="white-space:pre-wrap;max-height:220px;overflow:auto;">${escapeHtml(txt)}</pre>` : '';
              } catch (_) { /* ignore */ }

              const attempted = ns2 ? (`/admin/rag_status?namespace=${encodeURIComponent(ns2)}`) : '/admin/rag_status';
              ragModalBody.innerHTML = `<p>Unable to load detailed status.</p><p><strong>HTTP ${res3.status}</strong> while fetching <code>${escapeHtml(attempted)}</code>.</p>${details}`;
            }
          } catch (_) {
            const attempted = (() => {
              try {
                const ns2 = getCurrentNamespace();
                return ns2 ? (`/admin/rag_status?namespace=${encodeURIComponent(ns2)}`) : '/admin/rag_status';
              } catch (_) {
                return '/admin/rag_status';
              }
            })();
            ragModalBody.innerHTML = `<p>Unable to load detailed status.</p><p><em>Request failed or timed out.</em> Attempted <code>${escapeHtml(attempted)}</code>.</p>`;
          }
        });

        if (ragChatBackfillBtn && !ragChatBackfillBtn._wired) {
          ragChatBackfillBtn._wired = true;
          ragChatBackfillBtn.addEventListener('click', async () => {
            let advisoryB = null;
            let advisoryExceededB = false;
            const previousBackfillElapsedMs = Number(window.__vonLastRagBackfillJson?.client_elapsed_ms);
            const backfillAdvisoryMs = Number.isFinite(previousBackfillElapsedMs) && previousBackfillElapsedMs > 0
              ? Math.max(60000, Math.min(15 * 60 * 1000, Math.round(previousBackfillElapsedMs * 1.5)))
              : 180000;
            const backfillStartedAt = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
            try {
              const ok = window.confirm('Backfill legacy conversation history into your current namespace and re-index to RAG? This may take a minute.');
              if (!ok) return;
              ragChatBackfillBtn.disabled = true;

              try {
                const modalActions = ragModal.querySelector('.modal-actions');
                if (modalActions) {
                  modalActions._ragActionInProgress = true;
                }
              } catch (_) { /* ignore */ }

              ragModalBody.innerHTML = ragModalBody.innerHTML + '<hr/><p><em>Backfill running…</em></p>';

              // Backfill is a mutation. Elapsed time is advisory only because
              // aborting the client request cannot prove that the server stopped.
              advisoryB = setTimeout(() => {
                advisoryExceededB = true;
                ragModalBody.innerHTML = ragModalBody.innerHTML + '<p><em>Backfill is taking longer than usual; it is still running and has not been cancelled or retried.</em></p>';
              }, backfillAdvisoryMs);
              const resB = await fetch('/admin/chat_history_backfill', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ max_sessions: 25, max_messages: 2000, dry_run: false }),
                cache: 'no-store'
              });

              if (!resB.ok) {
                const txt = await resB.text();
                const outcomeIndeterminate = resB.status >= 500;
                window.__vonLastRagBackfillJson = {
                  status: outcomeIndeterminate ? 'outcome_indeterminate' : 'error',
                  http_status: resB.status,
                  response_text: txt || '',
                  advisory_ms: backfillAdvisoryMs,
                  advisory_exceeded: advisoryExceededB
                };
                const heading = outcomeIndeterminate ? 'Backfill result not confirmed.' : 'Backfill failed.';
                const followUp = outcomeIndeterminate
                  ? '<p><em>The server returned an upstream error, so inspect canonical progress before starting another backfill.</em></p>'
                  : '';
                ragModalBody.innerHTML = ragModalBody.innerHTML + `<hr/><p><strong>${heading}</strong> ${txt}</p>${followUp}`;
                return;
              }
              const js = await resB.json();
              const backfillFinishedAt = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
              window.__vonLastRagBackfillJson = {
                ...js,
                client_elapsed_ms: Math.max(0, Math.round(backfillFinishedAt - backfillStartedAt)),
                client_advisory_ms: backfillAdvisoryMs,
                client_advisory_exceeded: advisoryExceededB
              };
              const statusLine = js?.status ? `<p><strong>Status:</strong> ${js.status}</p>` : '';
              const errorLine = js?.error ? `<p><strong>Error:</strong> ${js.error}</p>` : '';
              const errors = Array.isArray(js?.errors) ? js.errors : [];
              const errorsHtml = errors.length
                ? [
                  '<details>',
                  `<summary>Errors (${errors.length})</summary>`,
                  '<ul>',
                  ...errors.slice(0, 20).map(e => `<li>${escapeHtml((e.session || 'session') + '')} — ${escapeHtml((e.error || 'error') + '')}</li>`),
                  '</ul>',
                  '</details>'
                ].join('')
                : '';
              const summary = [
                '<hr/>',
                '<h3>Conversation history backfill</h3>',
                statusLine,
                errorLine,
                '<ul>',
                `<li><strong>Sessions updated:</strong> ${js.sessions_updated ?? '—'}</li>`,
                `<li><strong>Messages attempted:</strong> ${js.messages_indexed_attempted ?? '—'}</li>`,
                `<li><strong>Messages indexed:</strong> ${js.messages_indexed_success ?? '—'}</li>`,
                `<li><strong>Messages failed:</strong> ${js.messages_indexed_failed ?? '—'}</li>`,
                `<li><strong>Messages skipped:</strong> ${js.messages_skipped ?? '—'}</li>`,
                '</ul>'
              ].join('');
              ragModalBody.innerHTML = ragModalBody.innerHTML + summary + errorsHtml;

              ragChatBackfillBtn.hidden = true;
            } catch (e) {
              const name = e && e.name ? e.name : 'Error';
              const msg = e && e.message ? e.message : '';
              window.__vonLastRagBackfillJson = {
                status: 'outcome_indeterminate',
                error: {
                  type: 'network_outcome_indeterminate',
                  name,
                  message: msg,
                  advisory_ms: backfillAdvisoryMs,
                  advisory_exceeded: advisoryExceededB
                }
              };
              ragModalBody.innerHTML = ragModalBody.innerHTML + `<hr/><p><strong>Backfill response unavailable.</strong> ${name}${msg ? `: ${msg}` : ''}</p><p><em>The request may or may not have completed. Reopen this status to inspect canonical progress before starting another backfill.</em></p>`;
            } finally {
              if (advisoryB !== null) clearTimeout(advisoryB);
              ragChatBackfillBtn.disabled = false;
              try {
                const modalActions = ragModal.querySelector('.modal-actions');
                if (modalActions) {
                  modalActions._ragActionInProgress = false;
                }
              } catch (_) { /* ignore */ }
            }
          });
        }
        if (ragModalCheck) {
          ragModalCheck.addEventListener('click', async () => {
            try {
              ragModalCheck.disabled = true;
              const controller4 = new AbortController();
              const timeout4 = setTimeout(() => controller4.abort(), 15000);
              const ns3 = getCurrentNamespace();
              const url4 = ns3 ? (`/admin/rag_integrity?namespace=${encodeURIComponent(ns3)}`) : '/admin/rag_integrity';
              const res4 = await fetch(url4, { method: 'POST', cache: 'no-store', signal: controller4.signal });
              clearTimeout(timeout4);
              if (res4.ok) {
                const rr = await res4.json();
                const anomalies = Array.isArray(rr.anomalies) ? rr.anomalies : [];
                const details = [
                  '<h3>Integrity Check</h3>',
                  '<ul>',
                  `<li><strong>Sessions:</strong> ${rr.sessions}</li>`,
                  `<li><strong>Interactions:</strong> ${rr.interactions}</li>`,
                  `<li><strong>Indexed:</strong> ${rr.indexed}</li>`,
                  `<li><strong>Pending:</strong> ${rr.pending}</li>`,
                  `<li><strong>Failed:</strong> ${rr.failed}</li>`,
                  `<li><strong>Skipped:</strong> ${rr.skipped}</li>`,
                  `<li><strong>Eligible sessions:</strong> ${rr.eligible_sessions}</li>`,
                  `<li><strong>Eligible interactions:</strong> ${rr.eligible_interactions}</li>`,
                  '</ul>'
                ];
                if (anomalies.length) {
                  details.push('<h4>Anomalies</h4>');
                  details.push('<ul>');
                  anomalies.forEach(a => {
                    if (a.type === 'orphan_text_interactions') {
                      details.push(`<li>Text interactions without session indexing status. Sessions: ${a.session_ids.join(', ')}</li>`);
                    } else {
                      details.push(`<li>${a.type}</li>`);
                    }
                  });
                  details.push('</ul>');
                }
                // Append below existing content
                ragModalBody.innerHTML = ragModalBody.innerHTML + '<hr/>' + details.join('');
              } else {
                ragModalBody.innerHTML = ragModalBody.innerHTML + '<hr/><p>Integrity check failed.</p>';
              }
            } catch (_) {
              ragModalBody.innerHTML = ragModalBody.innerHTML + '<hr/><p>Integrity check error.</p>';
            } finally {
              ragModalCheck.disabled = false;
            }
          });
        }
        // Optional: offer sync to chat RAG store (admin-only)
        // Uncomment below if you want a UI button:
        // const syncBtn = document.createElement('button');
        // syncBtn.textContent = 'Sync to chat RAG store';
        // syncBtn.className = 'btn-mini';
        // syncBtn.addEventListener('click', async () => {
        //   try {
        //     syncBtn.disabled = true;
        //     const res = await fetch('/admin/rag_sync', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
        //     const js = await res.json();
        //     ragModalBody.innerHTML = ragModalBody.innerHTML + '<hr/>' + `<p>Sync result: added ${js.added} of ${js.total_indexed}</p>`;
        //   } catch (e) { ragModalBody.innerHTML += '<hr/><p>Sync error.</p>'; }
        //   finally { syncBtn.disabled = false; }
        // });
        // ragModalBody.parentElement.querySelector('.modal-actions').appendChild(syncBtn);
        if (ragModalClose) {
          ragModalClose.addEventListener('click', () => {
            try {
              const modalActions = ragModal.querySelector('.modal-actions');
              if (modalActions && modalActions._ragActionInProgress) {
                const ok = window.confirm('An admin action is still running. Close anyway?');
                if (!ok) return;
              }
            } catch (_) { /* ignore */ }
            ragModal.classList.remove('open');
            ragModal.setAttribute('aria-hidden', 'true');
          });
        }
        // Close on backdrop click (but do not allow accidental dismissal while a long-running
        // admin action is in progress, because it makes errors hard to capture).
        ragModal.addEventListener('click', (ev) => {
          if (ev.target === ragModal) {
            try {
              const modalActions = ragModal.querySelector('.modal-actions');
              if (modalActions && modalActions._ragActionInProgress) {
                return;
              }
            } catch (_) { /* ignore */ }
            ragModal.classList.remove('open');
            ragModal.setAttribute('aria-hidden', 'true');
          }
        });
      }
      if (pidSpan) {
        if (newPid !== null) {
          pidSpan.textContent = newPid;
          pidSpan.parentElement.classList.remove('pid-error');
        } else {
          pidSpan.textContent = '?';
        }
      }
      if (newStart) {
        startTimeIso = newStart;
        writeCachedStartTimeIso(newStart);
      }

      if (autoReloadEnabled() && !reloadTriggered && lastIdentity.pid !== null && lastIdentity.start !== null) {
        if (newPid !== null && newStart !== null && (newPid !== lastIdentity.pid || newStart !== lastIdentity.start)) {
          reloadTriggered = true;
          setTimeout(() => { try { window.location.reload(); } catch (_) { /* no-op */ } }, 300);
        }
      }

      if (newPid !== null) lastIdentity.pid = newPid;
      if (newStart !== null) lastIdentity.start = newStart;
      failureCount = 0; // reset on success
    } catch (e) {
      lastHealthCheckCompletedAtMs = Date.now();
      failureCount++;
      if (!Number.isFinite(firstFailureAtMs)) {
        firstFailureAtMs = Date.now();
      }
      const nowMs = Date.now();
      const isHttpError = typeof e?.message === 'string' && e.message.startsWith('HTTP ');
      const errorKind = e?.name === 'AbortError'
        ? 'timeout'
        : (isHttpError ? 'http' : 'network_or_unknown');
      const errorDetail = typeof e?.message === 'string'
        ? e.message
        : String(e || '');
      lastHealthErrorKind = errorKind;
      lastHealthErrorDetail = errorDetail;
      const activeThinking = isThinkingActive();
      const evaluation = evaluateServerHealthState({
        hasSeenSuccessfulHealthPoll,
        failureCount,
        firstFailureAtMs,
        nowMs,
        isThinkingActive: activeThinking,
        latestErrorKind: lastHealthErrorKind,
      });
      const markDown = evaluation.markDown;
      const lastSuccessAgeMs = Number.isFinite(lastHealthSuccessAtMs)
        ? Math.max(0, nowMs - lastHealthSuccessAtMs)
        : null;
      setServerHealthUiState(evaluation.state, {
        ...evaluation.diagnostics,
        source: 'health_poll_failure',
        lastSuccessAgeMs,
        lastErrorKind: lastHealthErrorKind,
        lastErrorDetail: lastHealthErrorDetail,
      });
      if (pidSpan) {
        if (markDown) {
          pidSpan.textContent = '-';
          pidSpan.parentElement.classList.add('pid-error');
        } else {
          pidSpan.textContent = '?';
          pidSpan.parentElement.classList.remove('pid-error');
        }
      }
      // Exponential backoff on failures. Timeouts retry sooner so status can recover quickly
      // after transient saturation (while still backing off to avoid request pileups).
      const failureBackoffCapMs = (lastHealthErrorKind === 'timeout') ? 15000 : 30000;
      const effectiveBackoffCapMs = (lastHealthErrorKind === 'network_or_unknown')
        ? 15000
        : failureBackoffCapMs;
      nextDelay = Math.min(effectiveBackoffCapMs, 5000 * Math.pow(2, Math.min(failureCount - 1, 3)));
    }
    // If ontology is busy, stretch the delay (but keep success shorter than failure backoff)
    if (busy) {
      nextDelay = Math.min(15000, Math.max(nextDelay, 10000));
    }
    healthPollInFlight = false;
    publishHealthTelemetry('health_poll_finished');
    if (healthPollQueuedImmediate) {
      healthPollQueuedImmediate = false;
      scheduleHealthPoll(250);
      return;
    }
    scheduleHealthPoll(nextDelay);
  }
  poll();
  // Also update busy indicator more responsively
  setInterval(updateBusyIndicator, 1500);
  updateUptimeLoop();

  // Listen for context reset events to trigger immediate RAG status refresh
  document.addEventListener('von:contextReset', (event) => {
    const detail = event?.detail || {};
    if (detail?.trigger !== 'chat_reset') {
      console.log('[health_poll] Context reset ignored (trigger not chat_reset)', detail);
      return;
    }
    console.log('[health_poll] Context reset detected, triggering immediate RAG status refresh', detail);
    // Force an immediate poll (will use current localStorage namespace)
    if (healthPollInFlight) {
      healthPollQueuedImmediate = true;
      scheduleHealthPoll(250);
      return;
    }
    poll().catch(err => console.warn('[health_poll] Immediate poll failed:', err));
  });

  // Concept link handlers
  document.querySelectorAll('.concept-link').forEach(a => {
    a.addEventListener('click', (ev) => {
      ev.preventDefault();
      const name = a.getAttribute('data-concept-name');
      if (!name) return;
      try {
        activateTab('vontologyTab');
        // Dispatch a custom event others can listen to for name-based selection/search
        document.dispatchEvent(new CustomEvent('von:selectConceptByName', { detail: { name } }));
      } catch (err) {
        console.warn('Concept link navigation failed', err);
      }
    });
  });
}
