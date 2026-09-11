/**
 * Dynamically calculate and set positions for tabs and content based on actual header height
 * JVNAUTOSCI-550: Replace hard-coded CSS positions with JavaScript calculation
 */
export function setupDynamicLayout() {
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
    tabContainer.style.top = `${headerHeight}px`;
    const measuredTabHeight = Math.max(
      44,
      Math.ceil(tabContainer.getBoundingClientRect().height || tabContainer.scrollHeight || 44)
    );
    const tabHeight = measuredTabHeight;
    const contentTop = headerHeight + tabHeight + 8; // 8px margin
    const narrow = window.matchMedia('(max-width: 800px), (max-width: 1024px) and (pointer: coarse)').matches;
    const footer = document.querySelector('.footer-container');
    const footerSpace = narrow ? Math.ceil(footer?.getBoundingClientRect().height || 64) + 8 : Math.max(64, Math.ceil(footer?.getBoundingClientRect().height || 0));
    // At normal zoom the visual viewport excludes the on-screen keyboard.
    // Pinch zoom must not resize the application or discard a draft.
    const viewport = window.visualViewport;
    const viewportHeight = narrow && viewport?.scale === 1 ? viewport.height : window.innerHeight;
    const keyboardInset = narrow && viewport?.scale === 1
      ? Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop) : 0;
    document.documentElement.style.setProperty('--von-viewport-height', `${viewportHeight}px`);
    document.documentElement.style.setProperty('--von-keyboard-inset', `${keyboardInset}px`);

    document.documentElement.style.setProperty('--von-fixed-shell-height', `${contentTop}px`);
    document.documentElement.style.setProperty('--von-fixed-footer-clearance', `${footerSpace}px`);

    // Position main tab content area below tabs
    if (tabContentArea) {
      tabContentArea.style.marginTop = `${contentTop}px`;
      // Also update the minimum height calculation to account for dynamic header
      const totalTopSpace = contentTop + 20; // content margin + padding
      tabContentArea.style.minHeight = `calc(100vh - ${totalTopSpace + footerSpace}px)`;
      tabContentArea.style.setProperty('--von-tab-content-available-height', `calc(100vh - ${contentTop + footerSpace}px)`);
      tabContentArea.style.removeProperty('height');
      tabContentArea.style.overflowY = 'visible';
    }

    // Position standalone content areas below tabs. Content already inside the
    // tab-content-area inherits that offset from the parent and must not stack it.
    contentAreas.forEach(area => {
      if (tabContentArea && tabContentArea.contains(area)) {
        area.style.marginTop = '0px';
      } else {
        area.style.marginTop = `${contentTop}px`;
      }
    });
  }

  // Initial layout
  updateLayout();

  // Re-calculate on window resize
  window.addEventListener('resize', updateLayout);
  window.visualViewport?.addEventListener('resize', updateLayout);
  window.visualViewport?.addEventListener('scroll', updateLayout);

  // Re-calculate when search UI changes (in case it affects header height)
  const searchInput = document.getElementById('vontologySearchInput');
  // Re-select header here (local inside updateLayout previously) to avoid scope errors
  const headerEl = document.getElementById('globalHeader');
  const tabContainer = document.querySelector('.tab-container');
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

  if (tabContainer && !tabContainer._dynamicLayoutObserved) {
    try {
      const resizeObserver = new ResizeObserver(() => {
        requestAnimationFrame(updateLayout);
      });
      resizeObserver.observe(tabContainer);
      tabContainer._dynamicLayoutObserved = true;
    } catch (e) {
      console.warn('Dynamic layout: Tab container ResizeObserver setup failed', e);
    }
  }

  const footer = document.querySelector('.footer-container');
  if (footer && !footer._dynamicLayoutObserved && typeof ResizeObserver === 'function') {
    new ResizeObserver(() => requestAnimationFrame(updateLayout)).observe(footer);
    footer._dynamicLayoutObserved = true;
  }

  if (!document._dynamicTabStripLayoutListenerBound) {
    document.addEventListener('von:tab-strip-layout-changed', () => {
      requestAnimationFrame(updateLayout);
    });
    document._dynamicTabStripLayoutListenerBound = true;
  }
}

