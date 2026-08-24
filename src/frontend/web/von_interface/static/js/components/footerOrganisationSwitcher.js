import { getUserContext } from '../apiService.js';
import {
  loadMyOrganisations,
  normaliseOrganisationDisplayName,
  switchOrganisation,
} from './orgSelector.js';

const MEMBERSHIP_CACHE_TTL_MS = 60_000;

let membershipCache = {
  userConceptId: null,
  organisations: null,
  loadedAtMs: 0,
  pending: null,
};

function normaliseConceptId(value) {
  const text = typeof value === 'string' ? value.trim() : '';
  return text || null;
}

function currentUserConceptId() {
  return normaliseConceptId(getUserContext()?.user_id);
}

function normaliseMemberships(items) {
  const seen = new Set();
  const organisations = [];
  for (const item of Array.isArray(items) ? items : []) {
    const conceptId = normaliseConceptId(item?.concept_id);
    if (!conceptId || seen.has(conceptId)) continue;
    seen.add(conceptId);
    organisations.push({
      conceptId,
      name: normaliseOrganisationDisplayName(item?.name) || conceptId,
      role: typeof item?.role === 'string' ? item.role.trim() : '',
    });
  }
  return organisations;
}

export function invalidateFooterOrganisationMemberships() {
  membershipCache = {
    userConceptId: null,
    organisations: null,
    loadedAtMs: 0,
    pending: null,
  };
}

async function getFooterOrganisationMemberships({ force = false } = {}) {
  const userConceptId = currentUserConceptId();
  if (membershipCache.userConceptId !== userConceptId) {
    membershipCache = {
      userConceptId,
      organisations: null,
      loadedAtMs: 0,
      pending: null,
    };
  }

  const cacheFresh = Array.isArray(membershipCache.organisations)
    && (Date.now() - membershipCache.loadedAtMs) < MEMBERSHIP_CACHE_TTL_MS;
  if (!force && cacheFresh) return membershipCache.organisations;
  if (membershipCache.pending) return membershipCache.pending;

  const requestCache = membershipCache;
  requestCache.pending = loadMyOrganisations()
    .then((items) => {
      const organisations = normaliseMemberships(items);
      if (membershipCache === requestCache) {
        requestCache.organisations = organisations;
        requestCache.loadedAtMs = Date.now();
      }
      return organisations;
    })
    .finally(() => {
      if (membershipCache === requestCache) requestCache.pending = null;
    });
  return requestCache.pending;
}

function setNativeTitle(element, value) {
  const title = String(value || '').trim();
  if (!title) {
    element.removeAttribute('title');
    element.removeAttribute('data-original-title');
    element.removeAttribute('data-keep-title');
    return;
  }
  element.title = title;
  element.dataset.originalTitle = title;
  element.dataset.keepTitle = 'true';
}

function writeSwitchingContext(conceptId, name) {
  try {
    sessionStorage.setItem('von_org_switching', JSON.stringify({
      concept_id: conceptId || null,
      name: name || null,
    }));
  } catch (_) {
    // Display-only hint. The authoritative switch remains server-bound.
  }
}

function clearSwitchingContext() {
  try { sessionStorage.removeItem('von_org_switching'); } catch (_) { /* ignore */ }
}

export function createFooterOrganisationSwitcher({
  organisationInfo = {},
  onOpenConcept = null,
} = {}) {
  let currentConceptId = normaliseConceptId(organisationInfo?.conceptId);
  let currentName = normaliseOrganisationDisplayName(organisationInfo?.name)
    || currentConceptId
    || 'Personal';
  let latestOrganisations = null;
  let switching = false;

  const segment = document.createElement('span');
  segment.className = 'footer-segment footer-org-switcher';

  const label = document.createElement('span');
  label.className = 'footer-label-inline';
  label.textContent = 'Org:';
  segment.appendChild(label);

  const control = document.createElement('span');
  control.className = 'footer-org-control';

  const currentButton = document.createElement('button');
  currentButton.type = 'button';
  currentButton.className = 'concept-footer-button footer-org-current-button';
  currentButton.textContent = currentName;
  currentButton.disabled = !currentConceptId;
  currentButton.dataset.conceptId = currentConceptId || '';
  currentButton.dataset.conceptName = currentConceptId ? currentName : '';
  setNativeTitle(
    currentButton,
    currentConceptId
      ? `Open ${currentName} in Vontology\nID: ${currentConceptId}`
      : 'Personal context has no organisation concept',
  );
  currentButton.addEventListener('click', (event) => {
    event.stopPropagation();
    if (!currentConceptId || typeof onOpenConcept !== 'function') return;
    void onOpenConcept({
      conceptId: currentConceptId,
      conceptName: currentName,
      displayName: currentName,
    });
  });
  control.appendChild(currentButton);

  const details = document.createElement('details');
  details.className = 'footer-org-menu-details';

  const summary = document.createElement('summary');
  summary.className = 'concept-footer-button footer-org-menu-trigger';
  summary.setAttribute('aria-label', 'Switch organisation');
  setNativeTitle(summary, 'Switch organisation');
  summary.textContent = '▾';
  details.appendChild(summary);

  const menu = document.createElement('span');
  menu.className = 'footer-org-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', 'Available organisations');

  const status = document.createElement('span');
  status.className = 'footer-org-switch-status';
  status.setAttribute('aria-live', 'polite');
  status.textContent = 'Loading organisations…';
  menu.appendChild(status);

  const options = document.createElement('span');
  options.className = 'footer-org-options';
  menu.appendChild(options);

  const retryButton = document.createElement('button');
  retryButton.type = 'button';
  retryButton.className = 'footer-org-retry-button';
  retryButton.textContent = 'Retry';
  retryButton.hidden = true;
  menu.appendChild(retryButton);

  details.appendChild(menu);
  control.appendChild(details);
  segment.appendChild(control);

  const setCurrentDisplay = (conceptId, name) => {
    currentConceptId = normaliseConceptId(conceptId);
    currentName = normaliseOrganisationDisplayName(name)
      || currentConceptId
      || 'Personal';
    currentButton.textContent = currentName;
    currentButton.disabled = !currentConceptId;
    currentButton.dataset.conceptId = currentConceptId || '';
    currentButton.dataset.conceptName = currentConceptId ? currentName : '';
    setNativeTitle(
      currentButton,
      currentConceptId
        ? `Open ${currentName} in Vontology\nID: ${currentConceptId}`
        : 'Personal context has no organisation concept',
    );
  };

  const setOptionsDisabled = (disabled) => {
    for (const button of options.querySelectorAll('button')) {
      button.disabled = disabled || button.dataset.current === 'true';
    }
    summary.setAttribute('aria-disabled', disabled ? 'true' : 'false');
    details.classList.toggle('is-switching', disabled);
  };

  const renderOptions = (organisations) => {
    latestOrganisations = Array.isArray(organisations) ? organisations : [];
    options.replaceChildren();

    const choices = [{ conceptId: null, name: 'Personal', role: '' }, ...latestOrganisations];
    if (currentConceptId && !choices.some((choice) => choice.conceptId === currentConceptId)) {
      choices.push({ conceptId: currentConceptId, name: currentName, role: '', unavailable: true });
    }

    for (const choice of choices) {
      const optionButton = document.createElement('button');
      optionButton.type = 'button';
      optionButton.className = 'footer-org-option';
      optionButton.setAttribute('role', 'menuitemradio');
      const isCurrent = (choice.conceptId || null) === (currentConceptId || null);
      optionButton.setAttribute('aria-checked', isCurrent ? 'true' : 'false');
      optionButton.dataset.current = isCurrent ? 'true' : 'false';
      optionButton.dataset.organisationConceptId = choice.conceptId || '';
      optionButton.disabled = isCurrent || switching || choice.unavailable === true;

      const check = document.createElement('span');
      check.className = 'footer-org-option-check';
      check.setAttribute('aria-hidden', 'true');
      check.textContent = isCurrent ? '✓' : '';
      optionButton.appendChild(check);

      const name = document.createElement('span');
      name.className = 'footer-org-option-name';
      name.textContent = choice.name;
      optionButton.appendChild(name);

      if (choice.role) {
        const role = document.createElement('span');
        role.className = 'footer-org-option-role';
        role.textContent = choice.role;
        optionButton.appendChild(role);
      }

      optionButton.addEventListener('click', async (event) => {
        event.stopPropagation();
        if (switching || isCurrent || choice.unavailable) return;

        switching = true;
        retryButton.hidden = true;
        writeSwitchingContext(choice.conceptId, choice.name);
        setOptionsDisabled(true);
        status.classList.remove('error');
        status.textContent = `Switching to ${choice.name}…`;

        try {
          const response = await switchOrganisation(choice.conceptId, choice.conceptId ? choice.name : null);
          const committedConceptId = normaliseConceptId(response?.organisation_id);
          const committedName = committedConceptId ? choice.name : 'Personal';
          setCurrentDisplay(committedConceptId, committedName);
          switching = false;
          clearSwitchingContext();
          renderOptions(latestOrganisations);
          status.textContent = `Switched to ${committedName}. Refreshing organisation data…`;
          details.open = false;
        } catch (error) {
          switching = false;
          clearSwitchingContext();
          renderOptions(latestOrganisations);
          status.classList.add('error');
          status.textContent = 'Switch failed. Choose an organisation to retry.';
          setNativeTitle(status, error?.message || 'Organisation switch failed');
        }
      });

      options.appendChild(optionButton);
    }

    status.classList.remove('error');
    status.textContent = latestOrganisations.length > 0
      ? 'Choose an organisation for this window.'
      : 'No organisation memberships are available.';
    setOptionsDisabled(switching);
  };

  const loadOptions = async ({ force = false } = {}) => {
    const userConceptIdAtStart = currentUserConceptId();
    retryButton.hidden = true;
    status.classList.remove('error');
    status.textContent = force ? 'Retrying organisation list…' : 'Loading organisations…';
    try {
      const organisations = await getFooterOrganisationMemberships({ force });
      if (currentUserConceptId() !== userConceptIdAtStart) return;
      if (!segment.isConnected && document.body?.contains(segment) === false) return;
      renderOptions(organisations);
    } catch (error) {
      status.classList.add('error');
      status.textContent = 'Organisations are temporarily unavailable.';
      setNativeTitle(status, error?.message || 'Organisation list unavailable');
      retryButton.hidden = false;
    }
  };

  retryButton.addEventListener('click', (event) => {
    event.stopPropagation();
    void loadOptions({ force: true });
  });

  details.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      details.open = false;
      summary.focus();
    }
  });

  let outsidePointerListener = null;
  details.addEventListener('toggle', () => {
    if (details.open) {
      if (!latestOrganisations) void loadOptions();
      outsidePointerListener = (event) => {
        if (!details.contains(event.target)) details.open = false;
      };
      document.addEventListener('pointerdown', outsidePointerListener);
    } else if (outsidePointerListener) {
      document.removeEventListener('pointerdown', outsidePointerListener);
      outsidePointerListener = null;
    }
  });

  void loadOptions();
  return segment;
}

try {
  document.addEventListener('authStatusChanged', invalidateFooterOrganisationMemberships);
} catch (_) {
  // Constrained test environments may not provide document at import time.
}
