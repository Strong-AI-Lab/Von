import { getJsonDetailed } from '../apiService.js';

function getMountElement(stepContainer, suffix) {
  return (
    document.getElementById(`conceptSummaryRendererMount_${suffix}`) ||
    document.getElementById('conceptSummaryRendererMount') ||
    stepContainer.querySelector('.concept-summary-renderer-mount')
  );
}

function createPanelElement(suffix) {
  const panel = document.createElement('section');
  panel.id = `conceptSummaryRendererPanel_${suffix}`;
  panel.className = 'concept-summary-card hidden';
  return panel;
}

function renderBadgeRow(badges = []) {
  if (!Array.isArray(badges) || badges.length === 0) return '';
  return `
    <div class="concept-summary-badges">
      ${badges
        .filter((badge) => typeof badge === 'string' && badge.trim())
        .map((badge) => `<span class="concept-summary-badge">${badge}</span>`)
        .join('')}
    </div>
  `;
}

function renderFacts(facts = []) {
  if (!Array.isArray(facts) || facts.length === 0) return '';
  return `
    <dl class="concept-summary-facts">
      ${facts
        .filter((item) => item && typeof item.label === 'string' && typeof item.value === 'string')
        .map(
          (item) => `
            <div class="concept-summary-fact">
              <dt>${item.label}</dt>
              <dd>${item.value}</dd>
            </div>
          `,
        )
        .join('')}
    </dl>
  `;
}

function renderExpandedSections(expandedSections = []) {
  const sections = Array.isArray(expandedSections)
    ? expandedSections.filter(
        (section) =>
          section &&
          typeof section.title === 'string' &&
          Array.isArray(section.items) &&
          section.items.some((item) => typeof item === 'string' && item.trim()),
      )
    : [];
  if (sections.length === 0) return '';
  return `
    <div class="concept-summary-expanded hidden">
      ${sections
        .map(
          (section) => `
            <section class="concept-summary-expanded-section">
              <h4>${section.title}</h4>
              <ul>
                ${section.items
                  .filter((item) => typeof item === 'string' && item.trim())
                  .map((item) => `<li>${item}</li>`)
                  .join('')}
              </ul>
            </section>
          `,
        )
        .join('')}
    </div>
  `;
}

function applyPanelPayload(panel, payload) {
  const panelData = payload?.panel || {};
  const title = typeof panelData.title === 'string' ? panelData.title.trim() : '';
  const subtitle = typeof panelData.subtitle === 'string' ? panelData.subtitle.trim() : '';
  const summary = typeof panelData.summary === 'string' ? panelData.summary.trim() : '';
  const eyebrow = typeof panelData.eyebrow === 'string' ? panelData.eyebrow.trim() : 'Concept';
  const variant = typeof panelData.variant === 'string' ? panelData.variant.trim() : 'generic';
  const hasExpandedSections =
    Array.isArray(panelData.expanded_sections) &&
    panelData.expanded_sections.some(
      (section) => Array.isArray(section?.items) && section.items.some((item) => typeof item === 'string' && item.trim()),
    );

  panel.className = `concept-summary-card concept-summary-card-${variant}`;
  panel.innerHTML = `
    <div class="concept-summary-header">
      <div class="concept-summary-eyebrow">${eyebrow}</div>
      ${
        hasExpandedSections
          ? `<button type="button" class="concept-summary-toggle" aria-expanded="false">Show more</button>`
          : ''
      }
    </div>
    <h3 class="concept-summary-title">${title}</h3>
    ${subtitle ? `<p class="concept-summary-subtitle">${subtitle}</p>` : ''}
    ${renderBadgeRow(panelData.badges)}
    ${renderFacts(panelData.facts)}
    ${summary ? `<p class="concept-summary-text">${summary}</p>` : ''}
    ${renderExpandedSections(panelData.expanded_sections)}
  `;

  const toggle = panel.querySelector('.concept-summary-toggle');
  const expanded = panel.querySelector('.concept-summary-expanded');
  if (toggle && expanded) {
    toggle.addEventListener('click', () => {
      const isExpanded = !expanded.classList.contains('hidden');
      expanded.classList.toggle('hidden', isExpanded);
      toggle.setAttribute('aria-expanded', isExpanded ? 'false' : 'true');
      toggle.textContent = isExpanded ? 'Show more' : 'Show less';
    });
  }
}

export async function ensureConceptSummaryRendererPanelForConceptTab({
  conceptId,
  suffix,
}) {
  const stepContainer =
    document.getElementById(`conceptStep1_${suffix}`) || document.getElementById('conceptStep1');
  if (!stepContainer) return false;

  const mount = getMountElement(stepContainer, suffix);
  if (!mount) return false;

  let panel = document.getElementById(`conceptSummaryRendererPanel_${suffix}`);
  if (!panel) {
    panel = createPanelElement(suffix);
    mount.replaceChildren(panel);
  }

  try {
    const { data } = await getJsonDetailed(
      `/api/concepts/${encodeURIComponent(conceptId)}/summary_renderer`,
      { cache: 'no-store' },
    );
    if (!data || !data.panel) {
      panel.className = 'concept-summary-card hidden';
      panel.innerHTML = '';
      return false;
    }
    applyPanelPayload(panel, data);
    return true;
  } catch (error) {
    console.warn('[conceptSummaryRendererPanel] Failed to load concept summary renderer', error);
    panel.className = 'concept-summary-card hidden';
    panel.innerHTML = '';
    return false;
  }
}
