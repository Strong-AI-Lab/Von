// Script to handle resizing the settings iframe based on content
document.addEventListener('DOMContentLoaded', () => {
  // Toggle verbose debugging of resize behavior. Set to true when you need to trace layout changes.
  const SETTINGS_FRAME_RESIZE_DEBUG = false;
  function debugLog(...args) {
    try { if (SETTINGS_FRAME_RESIZE_DEBUG) console.debug('[settings-frame-resize]', ...args); } catch (_) {}
  }

  let lastSentHeight = 0;
  let isUpdating = false;
  let updateCount = 0;
  let maxUpdates = 5; // Reduced to prevent excessive updates
  let hasInitialUpdateBeenSent = false;
  let lastUpdateTime = Date.now();
  
  // Function to calculate and send the document height to the parent
  function sendHeightToParent(forceUpdate = false) {
    try {
      // Prevent recursive calls (unless forced)
      if (isUpdating && !forceUpdate) {
        debugLog('Height update blocked - already updating');
        return;
      }
      
      // Prevent too many updates in succession (unless forced)
      if (!forceUpdate) {
        updateCount++;
        if (updateCount > maxUpdates) {
          console.warn(`Too many height updates, stopping to prevent infinite loop. Count: ${updateCount}`);
          return;
        }
      }
      
      // Wait for a moment to ensure DOM is fully rendered
      setTimeout(() => {
        // Get the document height - use the body scrollHeight or the documentElement's height
        const height = Math.max(
          document.body.scrollHeight,
          document.documentElement.scrollHeight,
          document.body.offsetHeight,
          document.documentElement.offsetHeight,
          document.body.clientHeight,
          document.documentElement.clientHeight
        );
        
        // Ensure minimum height for settings content
        const minHeight = 400;
        const actualHeight = Math.max(height, minHeight);
        
        // Add a small buffer for potential scrollbar
        const heightWithBuffer = actualHeight + 40;
        
        // Only send if height has changed by more than a threshold (prevent micro-adjustments)
        // Skip this check if we're forcing an update
        if (!forceUpdate) {
          const heightDifference = Math.abs(heightWithBuffer - lastSentHeight);
          if (heightDifference < 10) {
            debugLog(`Height change too small (${heightDifference}px), skipping update`);
            return;
          }
        }
        
        // Prevent sending if height is unreasonably large (probably a loop)
        if (heightWithBuffer > 10000) {
          console.warn('Iframe height seems too large, preventing potential loop:', heightWithBuffer);
          return;
        }
        
        isUpdating = true;
        lastSentHeight = heightWithBuffer;
        hasInitialUpdateBeenSent = true;
        lastUpdateTime = Date.now();
        
        // Send the height to the parent window
        window.parent.postMessage({ 
          type: 'settings-frame-height', 
          height: heightWithBuffer 
        }, '*');
        
        const updateType = forceUpdate ? 'FORCED' : updateCount;
  debugLog(`Sent height update to parent: ${heightWithBuffer}px (update #${updateType})`);
        
        // Reset flag after a delay
        setTimeout(() => {
          isUpdating = false;
        }, 100);
      }, 50);
      
    } catch (e) {
      console.error('Error sending height to parent:', e);
      isUpdating = false;
    }
  }
  
  // Listen for messages from parent requesting height update
  window.addEventListener('message', (event) => {
    if (event.data.type === 'request-settings-height') {
      debugLog('Height update requested by parent - forcing recalculation');
      // Force a new height calculation with override
      sendHeightToParent(true); // Pass true to force update
    }
  });
  
  // Initial height calculation with a single attempt
  setTimeout(() => {
    debugLog('Sending initial height calculation');
    sendHeightToParent();
  }, 200);
  
  // Function to check if content is ready and send height if needed
  function checkForContentReady() {
    const organisationSelect = document.getElementById('organisationSelect');
    const modelSelect = document.getElementById('modelSelect');
    
    // Check if dropdowns are populated
    const hasOrganisations = organisationSelect && organisationSelect.options.length > 1;
    const hasModels = modelSelect && modelSelect.options.length > 1;
    
  if (hasOrganisations && hasModels && !hasInitialUpdateBeenSent) {
  debugLog('Content appears ready, sending height update');
  sendHeightToParent();
      return true;
    }
    return false;
  }
  
  // Check for content readiness periodically, but only for first 3 seconds
  let readinessChecks = 0;
  const maxReadinessChecks = 6;
  const readinessInterval = setInterval(() => {
    readinessChecks++;
      if (checkForContentReady() || readinessChecks >= maxReadinessChecks) {
      clearInterval(readinessInterval);
      if (readinessChecks >= maxReadinessChecks) {
        debugLog('Stopped checking for content readiness after 3 seconds');
      }
    }
  }, 500);
  
  // Reset the update counter periodically to allow future updates
  setInterval(() => {
    if (updateCount > 0) {
      debugLog(`Resetting update counter from ${updateCount} to 0`);
      updateCount = 0;
    }
  }, 5000);
  
  // Set up a conservative mutation observer
  const observerConfig = {
    childList: true,
    subtree: true,
    attributes: false,
    characterData: false
  };
  
  let mutationTimeout;
  const observer = new MutationObserver((mutations) => {
    // Only process significant mutations
    const significantMutations = mutations.filter(mutation => {
      return mutation.type === 'childList' && mutation.addedNodes.length > 0;
    });
    
    if (significantMutations.length > 0) {
      debugLog(`DOM mutations detected: (${significantMutations.length}) significant changes`);
      
      // Debounce mutation responses
      clearTimeout(mutationTimeout);
      mutationTimeout = setTimeout(() => {
        if (updateCount < maxUpdates) {
          debugLog('Triggering height update due to significant mutation');
          sendHeightToParent();
        }
      }, 300);
    }
  });
  
  // Start observing with a delay to avoid initial DOM construction
  setTimeout(() => {
    observer.observe(document.body, observerConfig);
  }, 1000);
  
});
