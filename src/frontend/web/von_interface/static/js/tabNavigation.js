import { fetchConceptList, initializeConceptTab, updateConceptTabUI } from './conceptTab.js';
import { elements } from './domUtils.js';
import { initializeDynamicTabs, loadDynamicConceptTabContent } from './dynamicTabs.js';
import { isExpertTabsEnabled } from './featureFlags.js';
import { initializeImportExportTab } from './importExportTab.js';
import { initializeVontologyTab } from './vontology.js';

const GUARDED_TAB_IDS = new Set(['vontologyTab', 'importExportTab', 'annotationTab']);

function isTabGuarded(tabId) {
  return !isExpertTabsEnabled() && GUARDED_TAB_IDS.has(tabId);
}

export function setupTabNavigation() {
  console.log("Setting up tab navigation...");

  // Initialize dynamic tabs functionality
  initializeDynamicTabs();

  // These need to be initialized here because they are used for setup
  elements.tabButtons = document.querySelectorAll('.tab-button');
  elements.tabContents = document.querySelectorAll('.tab-content');

  if (!elements.tabButtons || elements.tabButtons.length === 0) {
    console.warn("No tab buttons found. Tab navigation setup skipped.");
    return;
  }

  elements.tabButtons.forEach(button => {
    button.addEventListener('click', async (event) => {
      // Prevent default anchor navigation to avoid jsdom navigation errors in tests
      if (event && typeof event.preventDefault === 'function') {
        event.preventDefault();
      }
      const tabId = button.dataset.tab;
      console.log(`Tab clicked: ${tabId}`);
      activateTab(tabId);
      // loadTabData will be called after tab initialization is complete
    });
  });

  console.log("Tab navigation setup complete.");
}

export function activateTab(tabId) {
  console.log(`Activating tab: ${tabId}`);
  if (isTabGuarded(tabId)) {
    console.warn(`[tabNavigation] Tab ${tabId} is disabled by feature flag; falling back to chat.`);
    tabId = 'chatTab';
  }

  // Re-query elements to include any dynamically created tabs
  const allTabContents = document.querySelectorAll('.tab-content');
  const allTabButtons = document.querySelectorAll('.tab-button');

  allTabContents.forEach(content => content.classList.remove('active'));
  allTabButtons.forEach(button => button.classList.remove('active'));

  const contentToShow = document.getElementById(tabId);
  const buttonToActivate = document.querySelector(`.tab-button[data-tab="${tabId}"]`);

  if (contentToShow) {
    contentToShow.classList.add('active');
    console.log(`Tab content found for ${tabId}, making it active.`);
    // For conceptTab, ensure initialization is triggered even before dynamic load
    if (tabId === 'conceptTab' && !contentToShow.dataset.initialized) {
      const hasList = contentToShow.querySelector('#conceptListUl');
      if (!hasList) {
        console.log('Concept tab pre-initialization (no list present yet).');
        try { initializeConceptTab(); } catch (e) { console.error('Early conceptTab init failed', e); }
        contentToShow.dataset.initialized = 'true';
      }
    }
    loadTabContent(tabId, contentToShow);
  } else {
    console.error(`activateTab: Content element with ID '${tabId}' not found.`);
  }

  if (buttonToActivate) {
    buttonToActivate.classList.add('active');
  } else {
    console.error(`activateTab: Button element with data-tab '${tabId}' not found.`);
  }

  if (document.body) {
    document.body.dataset.activeTab = tabId;
  }

  if (history.replaceState) {
    history.replaceState(null, '', `#${tabId}`);
  } else {
    window.location.hash = tabId;
  }

  try {
    document.dispatchEvent(new CustomEvent('von:tab-activated', {
      detail: { tabId }
    }));
  } catch (_) { /* ignore */ }
}

async function loadTabContent(tabId, contentElement) {
  if (isTabGuarded(tabId)) {
    console.warn(`[tabNavigation] Skipping load for disabled tab ${tabId}.`);
    return;
  }
  // Chat tab is embedded, no dynamic loading needed
  if (tabId === 'chatTab') {
    console.log('Chat tab content is embedded, skipping dynamic loading.');
    return;
  }

  // Prevent duplicate parallel loads
  if (contentElement.dataset.loading === 'true') {
    console.log(`loadTabContent: ${tabId} already loading, skipping.`);
    return;
  }

  // Check if this is a dynamic concept tab
  if (tabId.startsWith('conceptTab_')) {
    const conceptId = contentElement.dataset.conceptId;
    if (conceptId) {
      console.log(`Loading dynamic concept tab content for ${tabId} (concept: ${conceptId})`);
      await loadDynamicConceptTabContent(tabId, conceptId);
      return;
    }
  }

  // Check if content is already loaded
  const isVontologyLoaded = tabId === 'vontologyTab' && contentElement.querySelector('#vontologyTreeContainer');
  const isImportExportLoaded = tabId === 'importExportTab' && contentElement.querySelector('#vontologyFileInput');
  const isConceptLoaded = tabId === 'conceptTab' && contentElement.querySelector('#conceptListUl');
  // Annotation tab: treat presence of primary run button (or dataset flag) as loaded to avoid DOM replacement.
  const annotationHasRunBtn = tabId === 'annotationTab' && contentElement.querySelector('#runAnnotationButton');
  const isAnnotationLoaded = tabId === 'annotationTab' && contentElement.dataset.initialized === 'true';

  if (isVontologyLoaded || isImportExportLoaded || isConceptLoaded || isAnnotationLoaded) {
    console.log(`Tab ${tabId} content already loaded, skipping dynamic loading.`);
    // Still need to initialize functionality if not done yet
    if (tabId === 'importExportTab' && !contentElement.dataset.initialized) {
      console.log('Import/Export tab content loaded but not initialized, initializing now...');
      initializeTabFunctionality(tabId);
      contentElement.dataset.initialized = 'true';
    }
    if (tabId === 'annotationTab' && !contentElement.dataset.initialized) {
      if (annotationHasRunBtn) {
        console.log('Annotation tab has DOM but not initialized (late init), initializing now.');
        initializeTabFunctionality(tabId);
      } else {
        console.log('Annotation tab marked as loaded but run button missing; will attempt full reload.');
      }
    }
    return;
  }

  const tabRoutes = {
    'vontologyTab': '/vontology_tab',
    'importExportTab': '/import_export_tab',
    'conceptTab': '/concept_tab',
    'annotationTab': '/annotation_tab'
  };

  const route = tabRoutes[tabId];
  if (!route) {
    // For tabs like 'settingsTab' that don't use AJAX
    console.log(`No dynamic loading route defined for tab: ${tabId}`);
    return;
  }

  try {
    contentElement.dataset.loading = 'true';
    console.log(`Loading content for ${tabId} from ${route}`);
    const response = await fetch(route);
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }
    const html = await response.text();
    console.log(`Successfully loaded content for ${tabId}.`);
    contentElement.innerHTML = html;

    // Use a small timeout to ensure the DOM is updated before initializing
    setTimeout(() => {
      console.log(`DOM updated for ${tabId}, proceeding with initialization.`);
      initializeTabFunctionality(tabId);
    }, 0);

  } catch (error) {
    console.error(`Error loading content for ${tabId}:`, error);
    contentElement.innerHTML = `<div class="error">Error loading ${tabId} content. Please try again.</div>`;
  } finally {
    delete contentElement.dataset.loading;
  }
}

function initializeTabFunctionality(tabId) {
  const contentElement = document.getElementById(tabId);
  switch (tabId) {
    case 'vontologyTab':
      console.log('Initializing Vontology tab functionality...');
      initializeVontologyTab();
      break;
    case 'importExportTab':
      console.log('Initializing Import/Export tab functionality...');
      initializeImportExportTab();
      break;
    case 'conceptTab':
      console.log('Initializing Concept tab functionality...');
      if (!contentElement?.dataset.initialized) {
        initializeConceptTab();
      } else {
        console.log('Concept tab already initialized, skipping duplicate init.');
      }
      break;
    case 'annotationTab':
      console.log('Initializing Annotation tab functionality...');
      // Defer setting dataset.initialized until after annotationTab initialize completes
      import('./annotationTab.js').then(m => {
        try {
          if (m.initializeAnnotationTab) m.initializeAnnotationTab();
          // Only mark initialized after successful call to avoid race where flag is set
          // before listeners attach inside module (module uses attachOnce for idempotency).
          if (contentElement) contentElement.dataset.initialized = 'true';
        } catch (e) {
          console.error('Annotation tab init threw error', e);
        }
      }).catch(e => console.error('Failed to init annotation tab', e));
      break;
  }
  // For other tabs, set initialized flag immediately if not already
  if (tabId !== 'annotationTab') {
    if (contentElement && !contentElement.dataset.initialized) {
      contentElement.dataset.initialized = 'true';
    }
  }

  // Now load the tab data after initialization is complete
  setTimeout(async () => {
    await loadTabData(tabId);
  }, 10); // Small delay to ensure initialization is complete
}

export async function loadTabData(tabId) {
  console.log(`loadTabData: Loading data for tab: ${tabId}`);

  switch (tabId) {
    case 'conceptTab':
      // This will be called after the tab is initialized
      updateConceptTabUI();
      await fetchConceptList();
      break;
    case 'importExportTab':
      // Import/Export tab initializes its own functionality
      console.log('Import/Export tab initialized, no additional data loading needed.');
      break;
    case 'vontologyTab':
      {
        // Load vontology tree data
        const { maybeLoadVontologyTree } = await import('./vontology.js');
        await maybeLoadVontologyTree();
      }
      break;
    case 'globalTasksTab':
      {
        const { showGlobalTasks } = await import('./components/taskPanel.js');
        await showGlobalTasks();
      }
      break;
    case 'messagesTab':
      {
        const { showMessagesTab } = await import('./components/messagePanel.js');
        await showMessagesTab();
      }
      break;
    case 'chatTab':
    case 'settingsTab':
    case 'annotationTab':
      // Settings tab specific handling
      if (tabId === 'settingsTab') {
        // Force height recalculation for settings iframe
        setTimeout(() => {
          const settingsFrame = document.getElementById('settingsFrame');
          if (settingsFrame && settingsFrame.contentWindow) {
            // Send multiple requests to ensure height is calculated properly
            settingsFrame.contentWindow.postMessage({ type: 'request-settings-height' }, '*');

            // Initialize authentication status check when settings tab is activated
            if (settingsFrame.contentWindow.initializeSettingsAuthentication) {
              settingsFrame.contentWindow.initializeSettingsAuthentication();
            }

            // Also send after a longer delay to catch any async content loading
            setTimeout(() => {
              settingsFrame.contentWindow.postMessage({ type: 'request-settings-height' }, '*');
              // Retry authentication check in case the first one was too early
              if (settingsFrame.contentWindow.initializeSettingsAuthentication) {
                settingsFrame.contentWindow.initializeSettingsAuthentication();
              }
            }, 500);

            // And one more after content should be fully loaded
            setTimeout(() => {
              settingsFrame.contentWindow.postMessage({ type: 'request-settings-height' }, '*');
            }, 1500);
          }
        }, 100);
      }
      break;
    default:
      console.warn(`loadTabData: No specific data loading action defined for tab ${tabId}`);
  }
}

// Export aliases for testing compatibility
export const switchToTab = activateTab;
export const initializeTabNavigation = setupTabNavigation;
