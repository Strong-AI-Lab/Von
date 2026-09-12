/**
 * Dynamically calculate and set positions for tabs and content based on actual header height
 * JVNAUTOSCI-550: Replace hard-coded CSS positions with JavaScript calculation
 */
export function setupDynamicLayout() {
  // Reparent live nodes so asynchronous refreshes and conversation scope survive.
  const footerContainer = document.querySelector('.footer-container');
  const footerHome = document.createComment('desktop footer');
  footerContainer?.before(footerHome);
  const aboutButton = document.getElementById('infoIcon');
  const aboutHome = document.createComment('desktop organisation information');
  aboutButton?.before(aboutHome);
  const mobileControls = document.createElement('div');
  mobileControls.className = 'mobile-shell-controls';
  const footerDetails = document.createElement('details');
  footerDetails.className = 'mobile-footer-details';
  const summary = document.createElement('summary');
  summary.textContent = 'Info';
  summary.setAttribute('aria-label', 'Conversation model, cost, account and diagnostics');
  const detailsPanel = document.createElement('div');
  detailsPanel.className = 'mobile-footer-details-panel';
  footerDetails.append(summary, detailsPanel);
  mobileControls.append(footerDetails);
  let organisation = null;
  let organisationHome = null;
  const narrowLayout = () => window.matchMedia('(max-width: 800px), (max-width: 1024px) and (pointer: coarse)').matches;

  function placeOrganisation() {
    const fresh = footerContainer?.querySelector('.footer-org-switcher');
    if (fresh && fresh !== organisation) {
      organisation?.remove();
      organisationHome?.remove();
      organisation = fresh;
      organisationHome = document.createComment('desktop organisation');
      fresh.before(organisationHome);
    }
    if (organisation && narrowLayout()) {
      if (organisation.parentElement !== mobileControls) mobileControls.prepend(organisation);
      const button = organisation.querySelector('.footer-org-current-button');
      const trigger = organisation.querySelector('.footer-org-menu-trigger');
      let compactLabel = trigger?.querySelector('.mobile-org-label');
      if (trigger && !compactLabel) {
        compactLabel = document.createElement('span');
        compactLabel.className = 'mobile-org-label';
        trigger.prepend(compactLabel);
      }
      const label = button?.textContent || 'Personal';
      if (compactLabel && compactLabel.textContent !== label) compactLabel.textContent = label;
      trigger?.setAttribute('aria-label', `Switch organisation: ${button?.dataset.conceptName || label}`);
    } else if (organisationHome?.isConnected && organisation?.parentNode !== organisationHome.parentNode) {
      organisationHome.after(organisation);
    }
  }
  footerDetails.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      footerDetails.open = false;
      summary.focus();
    }
  });
  document.addEventListener('pointerdown', event => {
    if (footerDetails.open && !footerDetails.contains(event.target)) footerDetails.open = false;
  });
  // Rendering replaces children; compact canonical identity labels arrive later.
  if (footerContainer && typeof MutationObserver === 'function') {
    const observer = new MutationObserver(placeOrganisation);
    observer.observe(footerContainer, { childList: true, subtree: true, characterData: true });
    observer.observe(mobileControls, { childList: true, subtree: true, characterData: true });
  }

  function updateLayout() {
    const header = document.getElementById('globalHeader');
    const tabContainer = document.querySelector('.tab-container');
    const contentAreas = document.querySelectorAll('.tab-content');
    const tabContentArea = document.querySelector('.tab-content-area');

    if (!header || !tabContainer) {
      console.warn('Dynamic layout: Required elements not found');
      return;
    }

    const narrow = narrowLayout();
    const headerSlot = header.querySelector('.global-organisation-identity') || header;
    if (footerContainer && narrow && !mobileControls.isConnected) {
      headerSlot.append(mobileControls);
      detailsPanel.append(footerContainer);
      if (aboutButton) detailsPanel.append(aboutButton);
    } else if (!narrow && mobileControls.isConnected) {
      footerHome.after(footerContainer);
      if (aboutButton) aboutHome.after(aboutButton);
      mobileControls.remove();
    }
    placeOrganisation();
    const headerHeight = header.getBoundingClientRect().height;
    tabContainer.style.top = `${headerHeight}px`;
    const tabHeight = Math.max(44, Math.ceil(tabContainer.getBoundingClientRect().height || tabContainer.scrollHeight || 44));
    const contentTop = headerHeight + tabHeight + 8;
    const composer = document.querySelector('.chat-composer');
    document.documentElement.style.setProperty('--von-mobile-composer-height', `${Math.ceil(composer?.getBoundingClientRect().height || 0)}px`);
    const footerSpace = narrow ? 0 : Math.max(64, Math.ceil(footerContainer?.getBoundingClientRect().height || 0));
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

  const composer = document.querySelector('.chat-composer');
  if (composer && typeof ResizeObserver === 'function') {
    new ResizeObserver(() => requestAnimationFrame(updateLayout)).observe(composer);
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

