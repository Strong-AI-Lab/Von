import { initializeDomElements, initializeInfoPopup, loadAndDisplayGlobalModelInFooter } from './domUtils.js';
import { createOrActivateConceptTab } from './dynamicTabs.js';
import { getLanguageDisplayName } from './languageConfig.js';
import './suppressTooltips.js';
import { activateTab, loadTabData, setupTabNavigation } from './tabNavigation.js';
import { handleSelectConceptByIdDetail } from './utils/selectConceptByIdHandler.js';
import { isVontologyBusy, loadKeyConceptsForUser, preloadVontologyData, selectVontologyNodeByIdentifier, setupVontologySearchUI } from './vontology.js';

document.addEventListener('DOMContentLoaded', async () => {
  console.log("DOM fully loaded and parsed.");

  initializeDomElements();
  initializeInfoPopup();
  setupTabNavigation();
  setupSettingsFrameResizing();

  // Initialize global search UI (JVNAUTOSCI-550)
  setupVontologySearchUI();

  // Dynamic positioning: calculate header height and position tabs accordingly
  setupDynamicLayout();

  // Ensure user context is loaded BEFORE initializing chat to prevent race condition
  // where chat history loads with null user_id
  await ensureUserContext();

  // Initialize chat tab since it's embedded and active by default
  console.log("Initializing chat tab...");
  import('./chatTab.js').then(module => {
    if (module.initializeChatTab) {
      module.initializeChatTab();
    }
  }).catch(err => console.error('Error loading chat tab module:', err));

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
        activateTab,
        selectVontologyNodeByIdentifier
      });
    } catch (err) {
      console.warn('Failed to handle von:selectConceptById:', err);
    }
  });

  // Check URL hash for initial tab
  const hash = window.location.hash.substring(1);
  let initialTabId = 'chatTab'; // Default to chatTab

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
      const vpH = window.innerHeight;
      const tabBarH = 60; // fixed tab bar height
      const footerH = 60; // approximate footer height + padding
      const margins = 20; // extra spacing
      const max = vpH - tabBarH - footerH - margins;
      let target = Math.min(requested || max, max);
      target = Math.max(target, 400); // minimum usable
      return target;
    } catch { return requested || 600; }
  }

  window.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'settings-frame-height') {
      if (userResized) return; // do not override manual resize
      const rawHeight = event.data.height;
      if (rawHeight > 10000) return;
      const adjusted = computeAvailableHeight(rawHeight);
      if (Math.abs(adjusted - lastAutoHeight) < 5) return;
      settingsFrame.style.height = `${adjusted}px`;
      settingsFrame.style.minHeight = `${adjusted}px`;
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
    settingsFrame.style.minHeight = `${recomputed}px`;
    lastAutoHeight = recomputed;
  });
}

/**
 * Ensure user context is available in localStorage before app initialization.
 * Fetches settings if localStorage is empty.
 */
async function ensureUserContext() {
  try {
    // If we already have user context, we don't need to block
    if (localStorage.getItem('von_current_user')) {
      return;
    }

    console.log('[main] Fetching settings to populate user context...');
    const res = await fetch('/api/settings/');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const settings = await res.json();

    if (settings.current_user_person_id) {
      const user = {
        id: settings.current_user_person_id,
        concept_id: settings.current_user_person_concept_id,
        name: settings.current_user_person_name
      };
      localStorage.setItem('von_current_user', JSON.stringify(user));
      console.log('[main] Populated von_current_user from settings');
    }

    // When logged in, derive and persist the user/org namespace used by RAG status calls.
    // Only set it if not already present (do not override manual/advanced workflows).
    if (!localStorage.getItem('current_user_namespace') && settings.current_user_person_concept_id) {
      try {
        const userSlug = String(settings.current_user_person_concept_id).replace(/^#V#/, '');
        const orgSlug = settings.current_organisation_concept_id
          ? String(settings.current_organisation_concept_id).replace(/^#V#/, '')
          : null;
        const ns = orgSlug ? `#V#${userSlug}@${orgSlug}` : `#V#${userSlug}`;
        localStorage.setItem('current_user_namespace', ns);
        console.log('[main] Derived current_user_namespace from settings:', ns);
      } catch (e) {
        console.warn('[main] Failed to derive current_user_namespace from settings:', e);
      }
    }

    // Namespace fallback: if server does not provide user info, but a namespace is already
    // set locally (e.g., via manual selection), propagate it so downstream calls use it.
    if (!settings.current_user_person_id) {
      const ns = localStorage.getItem('von_namespace');
      if (ns && !localStorage.getItem('current_user_namespace')) {
        localStorage.setItem('current_user_namespace', ns);
        console.log('[main] Using existing von_namespace as current_user_namespace:', ns);
      }
    }

    if (settings.current_organisation_id) {
      const org = {
        id: settings.current_organisation_id,
        concept_id: settings.current_organisation_concept_id,
        name: settings.current_organisation_name
      };
      localStorage.setItem('von_current_org', JSON.stringify(org));
      console.log('[main] Populated von_current_org from settings');
    }
  } catch (e) {
    console.warn('[main] Failed to ensure user context:', e);
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

/**
 * Dynamically calculate and set positions for tabs and content based on actual header height
 * JVNAUTOSCI-550: Replace hard-coded CSS positions with JavaScript calculation
 */
function setupDynamicLayout() {
  function updateLayout() {
    const header = document.getElementById('globalHeader');
    const tabContainer = document.querySelector('.tab-container');
    const contentAreas = document.querySelectorAll('.tab-content');
    const tabContentArea = document.querySelector('.tab-content-area');

    if (!header || !tabContainer) {
      console.warn('Dynamic layout: Required elements not found');
      return;
    }

    // Get actual header height including search box
    const headerHeight = header.getBoundingClientRect().height;
    const tabHeight = 48; // Standard tab height
    const contentTop = headerHeight + tabHeight + 8; // 8px margin

    console.log(`Dynamic layout: Header ${headerHeight}px, positioning tabs at ${headerHeight}px, content at ${contentTop}px`);

    // Position tabs right below header
    tabContainer.style.top = `${headerHeight}px`;

    // Position main tab content area below tabs
    if (tabContentArea) {
      tabContentArea.style.marginTop = `${contentTop}px`;
      // Also update the minimum height calculation to account for dynamic header
      const footerSpace = 64; // Footer overlap space
      const totalTopSpace = contentTop + 20; // content margin + padding
      tabContentArea.style.minHeight = `calc(100vh - ${totalTopSpace + footerSpace}px)`;
      tabContentArea.style.removeProperty('height');
      tabContentArea.style.overflowY = 'visible';
    }

    // Position individual content areas below tabs (fallback for any not in main container)
    contentAreas.forEach(area => {
      area.style.marginTop = `${contentTop}px`;
    });
  }

  // Initial layout
  updateLayout();

  // Re-calculate on window resize
  window.addEventListener('resize', updateLayout);

  // Re-calculate when search UI changes (in case it affects header height)
  const searchInput = document.getElementById('vontologySearchInput');
  // Re-select header here (local inside updateLayout previously) to avoid scope errors
  const headerEl = document.getElementById('globalHeader');
  if (searchInput && headerEl && !headerEl._dynamicLayoutObserved) {
    try {
      const resizeObserver = new ResizeObserver(() => {
        requestAnimationFrame(updateLayout);
      });
      resizeObserver.observe(headerEl);
      headerEl._dynamicLayoutObserved = true; // flag to prevent duplicate observers
    } catch (e) {
      console.warn('Dynamic layout: ResizeObserver setup failed', e);
    }
  }
}

function startHealthPolling() {
  const localIpSpan = document.getElementById('serverLocalIpValue');
  const publicIpSpan = document.getElementById('serverPublicIpValue');
  const pidSpan = document.getElementById('serverPidValue');
  const uptimeSpan = document.getElementById('serverUptimeValue');
  const ragSpan = document.getElementById('ragIndexingValue');
  const ragDetailsBtn = document.getElementById('ragIndexingValue');
  const ragModal = document.getElementById('ragStatusModal');
  const ragModalBody = ragModal ? document.getElementById('ragStatusBody') : null;
  const ragModalClose = ragModal ? document.getElementById('ragStatusClose') : null;
  const ragModalCheck = ragModal ? document.getElementById('ragStatusCheck') : null;
  const ragChatBackfillBtn = ragModal ? document.getElementById('ragChatBackfill') : null;
  if (!pidSpan) return;
  // Copy-to-clipboard behavior for local IP address
  if (localIpSpan) {
    localIpSpan.addEventListener('click', async (e) => {
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
    publicIpSpan.addEventListener('click', async (e) => {
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
  pidSpan.addEventListener('click', async (e) => {
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
  let startTimeIso = null;
  let lastIdentity = { pid: null, start: null };
  let reloadTriggered = false;
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
  function updateUptimeLoop() {
    if (startTimeIso && uptimeSpan) {
      const started = Date.parse(startTimeIso);
      if (!isNaN(started)) {
        const diff = Date.now() - started;
        uptimeSpan.textContent = formatUptime(diff);
      }
    }
    requestAnimationFrame(() => setTimeout(updateUptimeLoop, 1000));
  }
  let failureCount = 0;
  const busyEl = document.getElementById('vontologyBusyIndicator');
  const busySr = document.getElementById('vontologyBusySrStatus');
  let lastBusyState = null;
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
      }
    } catch (_) { }
  }
  async function poll() {
    updateBusyIndicator();
    const busy = isVontologyBusy();
    let nextDelay = 5000; // base
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 8000);
      const res = await fetch('/health', { cache: 'no-store', signal: controller.signal });
      clearTimeout(timeout);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const newPid = (typeof data.pid !== 'undefined') ? data.pid : null;
      const newStart = data.start_time || null;
      const newLocalIp = data.local_ip || null;
      const newPublicIp = data.public_ip || null;
      const ragPending = (typeof data.rag_pending_count !== 'undefined') ? data.rag_pending_count : null;

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
          const ns = (localStorage.getItem('von_namespace') || localStorage.getItem('current_user_namespace')) || '';
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
              const chInNs = (typeof rs.chat_history_sessions_in_namespace === 'number') ? rs.chat_history_sessions_in_namespace : null;
              const chMissingNs = (typeof rs.chat_history_sessions_missing_namespace === 'number') ? rs.chat_history_sessions_missing_namespace : null;
              const chOtherNs = (typeof rs.chat_history_sessions_other_namespace === 'number') ? rs.chat_history_sessions_other_namespace : null;

              const titleParts = [
                `Sessions indexed=${i}`,
                `pending=${p}`,
                `failed=${f}`
              ];
              if (sessNs) {
                titleParts.push(`session_ns=${sessNs}`);
              }
              if (chSessions || chMessages || chOk || chFail) {
                titleParts.push(`Chat sessions=${chSessions}`);
                titleParts.push(`messages=${chMessages}`);
                titleParts.push(`indexed=${chOk}`);
                titleParts.push(`failed=${chFail}`);
              }
              const title = titleParts.join(' | ');
              if (p === 0) {
                if (chSessions || chMessages || chOk || chFail) {
                  if (chFail > 0) {
                    ragSpan.textContent = `Indexed ${i} • Chat ${chOk}/${chFail} failed`;
                  } else {
                    ragSpan.textContent = `Indexed ${i} • Chat ${chOk}`;
                  }
                } else {
                  ragSpan.textContent = `Indexed ${i}`;
                }
                ragSpan.title = title;
                ragSpan.classList.remove('rag-active');
              } else {
                if (chFail > 0) {
                  ragSpan.textContent = `Indexed ${i} • ${p} pending • Chat ${chFail} failed`;
                } else {
                  ragSpan.textContent = `Indexed ${i} • ${p} pending`;
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

            if (ragChatBackfillBtn) {
              ragChatBackfillBtn.hidden = true;
              ragChatBackfillBtn.disabled = false;
              ragChatBackfillBtn.title = 'Associate legacy chat history with your current namespace and re-index to RAG';
            }

            const controller3 = new AbortController();
            const timeout3 = setTimeout(() => controller3.abort(), 8000);
            const ns2 = (localStorage.getItem('von_namespace') || localStorage.getItem('current_user_namespace')) || '';
            const res3 = await fetch(ns2 ? (`/admin/rag_status?namespace=${encodeURIComponent(ns2)}`) : '/admin/rag_status', { cache: 'no-store', signal: controller3.signal });
            clearTimeout(timeout3);
            if (res3.ok) {
              let rs = await res3.json();
              const backfillReason0 = rs?.chat_history_backfill_reason || null;
              const backfillSessionNs = rs?.chat_history_backfill_session_namespace || null;
              if (backfillReason0 === 'namespace_mismatch' && backfillSessionNs && backfillSessionNs !== ns2) {
                try {
                  localStorage.setItem('current_user_namespace', backfillSessionNs);
                } catch (_) { /* ignore */ }

                try {
                  const controller3b = new AbortController();
                  const timeout3b = setTimeout(() => controller3b.abort(), 8000);
                  const res3b = await fetch(`/admin/rag_status?namespace=${encodeURIComponent(backfillSessionNs)}`, { cache: 'no-store', signal: controller3b.signal });
                  clearTimeout(timeout3b);
                  if (res3b.ok) {
                    rs = await res3b.json();
                  }
                } catch (_) { /* ignore */ }
              }
              const total = (typeof rs.total === 'number') ? rs.total : null;
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
                '<p><em>Note:</em> interaction sessions are scoped by a user-only namespace (e.g. <code>#V#user</code>). Chat history is scoped by a composite user@organisation namespace (e.g. <code>#V#user@org</code>).</p>',
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
                '<h3>Chat history</h3>',
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
                `<li><strong>Messages indexed:</strong> ${chOk}</li>`,
                `<li><strong>Messages failed:</strong> ${chFail}</li>`,
                '</ul>'
              ].join('');
              const needsBackfill = (chMissingNs && chMissingNs > 0) || (chOtherNs && chOtherNs > 0);
              const backfillNote = (!backfillAvailable && needsBackfill && backfillReason)
                ? `<p><em>Backfill unavailable: ${backfillReason}</em></p>`
                : '';
              ragModalBody.innerHTML = html + renderBreakdown() + chatHtml + backfillNote;

              if (ragChatBackfillBtn) {
                ragChatBackfillBtn.hidden = !(backfillAvailable && needsBackfill);
                if (!backfillAvailable && backfillReason === 'namespace_mismatch') {
                  ragChatBackfillBtn.title = 'Backfill unavailable: namespace mismatch';
                }
              }
            } else {
              ragModalBody.innerHTML = '<p>Unable to load detailed status.</p>';
            }
          } catch (_) {
            ragModalBody.innerHTML = '<p>Unable to load detailed status.</p>';
          }
        });

        if (ragChatBackfillBtn && !ragChatBackfillBtn._wired) {
          ragChatBackfillBtn._wired = true;
          ragChatBackfillBtn.addEventListener('click', async () => {
            try {
              const ok = window.confirm('Backfill legacy chat history into your current namespace and re-index to RAG? This may take a minute.');
              if (!ok) return;
              ragChatBackfillBtn.disabled = true;

              ragModalBody.innerHTML = ragModalBody.innerHTML + '<hr/><p><em>Backfill running…</em></p>';

              const controllerB = new AbortController();
              const timeoutB = setTimeout(() => controllerB.abort(), 180000);
              const resB = await fetch('/admin/chat_history_backfill', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ max_sessions: 25, max_messages: 2000, dry_run: false }),
                cache: 'no-store',
                signal: controllerB.signal
              });
              clearTimeout(timeoutB);

              if (!resB.ok) {
                const txt = await resB.text();
                ragModalBody.innerHTML = ragModalBody.innerHTML + `<hr/><p><strong>Backfill failed.</strong> ${txt}</p>`;
                return;
              }
              const js = await resB.json();
              const statusLine = js?.status ? `<p><strong>Status:</strong> ${js.status}</p>` : '';
              const errorLine = js?.error ? `<p><strong>Error:</strong> ${js.error}</p>` : '';
              const errors = Array.isArray(js?.errors) ? js.errors : [];
              const errorsHtml = errors.length
                ? [
                  '<details>',
                  `<summary>Errors (${errors.length})</summary>`,
                  '<ul>',
                  ...errors.slice(0, 20).map(e => `<li>${(e.session || 'session')} — ${(e.error || 'error')}</li>`),
                  '</ul>',
                  '</details>'
                ].join('')
                : '';
              const summary = [
                '<hr/>',
                '<h3>Chat history backfill</h3>',
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
              const timedOutNote = name === 'AbortError'
                ? '<p><em>Request timed out. The server may still be processing; reopen this status to see progress.</em></p>'
                : '';
              ragModalBody.innerHTML = ragModalBody.innerHTML + `<hr/><p><strong>Backfill error.</strong> ${name}${msg ? `: ${msg}` : ''}</p>${timedOutNote}`;
            } finally {
              ragChatBackfillBtn.disabled = false;
            }
          });
        }
        if (ragModalCheck) {
          ragModalCheck.addEventListener('click', async () => {
            try {
              ragModalCheck.disabled = true;
              const controller4 = new AbortController();
              const timeout4 = setTimeout(() => controller4.abort(), 15000);
              const ns3 = (localStorage.getItem('von_namespace') || localStorage.getItem('current_user_namespace')) || '';
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
            } catch (e) {
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
            ragModal.classList.remove('open');
            ragModal.setAttribute('aria-hidden', 'true');
          });
        }
        // Close on backdrop click
        ragModal.addEventListener('click', (ev) => {
          if (ev.target === ragModal) {
            ragModal.classList.remove('open');
            ragModal.setAttribute('aria-hidden', 'true');
          }
        });
      }
      if (newPid !== null) {
        pidSpan.textContent = newPid;
        pidSpan.parentElement.classList.remove('pid-error');
      } else { pidSpan.textContent = '?'; }
      if (newStart && !startTimeIso) {
        startTimeIso = newStart;
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
      failureCount++;
      pidSpan.textContent = '—';
      pidSpan.parentElement.classList.add('pid-error');
      // Exponential backoff on failures (5s,10s,20s,30s cap)
      nextDelay = Math.min(30000, 5000 * Math.pow(2, Math.min(failureCount - 1, 3)));
    }
    // If ontology is busy, stretch the delay (but keep success shorter than failure backoff)
    if (busy) {
      nextDelay = Math.min(15000, Math.max(nextDelay, 10000));
    }
    setTimeout(poll, nextDelay);
  }
  poll();
  // Also update busy indicator more responsively
  setInterval(updateBusyIndicator, 1500);
  updateUptimeLoop();

  // Listen for context reset events to trigger immediate RAG status refresh
  document.addEventListener('von:contextReset', () => {
    console.log('[health_poll] Context reset detected, triggering immediate RAG status refresh');
    // Force an immediate poll (will use current localStorage namespace)
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
