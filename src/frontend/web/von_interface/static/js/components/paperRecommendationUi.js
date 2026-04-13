const DEFAULT_REVIEW_EMPTY_MESSAGE =
  'Run a review to inspect recommendation rationale and provenance.';

export function parseCommaSeparatedProfileValues(rawValue) {
  const raw = String(rawValue || '').trim();
  if (!raw) return [];

  const deduped = [];
  const seen = new Set();
  for (const part of raw.split(',')) {
    const cleaned = part.trim();
    if (!cleaned) continue;
    const fingerprint = cleaned.toLocaleLowerCase();
    if (seen.has(fingerprint)) continue;
    seen.add(fingerprint);
    deduped.push(cleaned);
  }
  return deduped;
}

export function joinProfileValues(values) {
  if (!Array.isArray(values) || !values.length) return '';
  return values
    .filter((value) => typeof value === 'string' && value.trim())
    .map((value) => value.trim())
    .join(', ');
}

export function buildRecommendationProfilePayload(values = {}, overrides = {}) {
  return {
    project_description: String(values.projectDescription || ''),
    stated_interest_terms: parseCommaSeparatedProfileValues(values.statedInterestTerms),
    negative_interest_terms: parseCommaSeparatedProfileValues(values.negativeInterestTerms),
    preferred_authors: parseCommaSeparatedProfileValues(values.preferredAuthors),
    preferred_venues: parseCommaSeparatedProfileValues(values.preferredVenues),
    notes: String(values.notes || ''),
    ...overrides,
  };
}

export function renderObservedRecommendationInterests(
  observedElement,
  derivedContext,
  fallbackText = 'No linked research-interest concepts found for this subject yet.',
) {
  if (!observedElement) return;
  const interests = Array.isArray(derivedContext?.research_interest_concepts)
    ? derivedContext.research_interest_concepts
    : [];
  if (!interests.length) {
    observedElement.textContent = fallbackText;
    return;
  }
  observedElement.textContent = interests
    .map((item) => String(item?.name || item?.concept_id || '').trim())
    .filter(Boolean)
    .join(', ');
}

export function applyRecommendationProfileToElements(elements = {}, profilePayload = {}) {
  const profile = profilePayload?.profile || {};
  const {
    projectDescriptionInput,
    interestTermsInput,
    negativeTermsInput,
    preferredAuthorsInput,
    preferredVenuesInput,
    notesInput,
    observedInterestsElement,
  } = elements;

  if (projectDescriptionInput) projectDescriptionInput.value = profile.project_description || '';
  if (interestTermsInput) interestTermsInput.value = joinProfileValues(profile.stated_interest_terms);
  if (negativeTermsInput) negativeTermsInput.value = joinProfileValues(profile.negative_interest_terms);
  if (preferredAuthorsInput) preferredAuthorsInput.value = joinProfileValues(profile.preferred_authors);
  if (preferredVenuesInput) preferredVenuesInput.value = joinProfileValues(profile.preferred_venues);
  if (notesInput) notesInput.value = profile.notes || '';

  renderObservedRecommendationInterests(observedInterestsElement, profilePayload?.derived_context);
}

export function parseCandidatePaperConceptIds(rawValue) {
  const raw = String(rawValue || '').trim();
  if (!raw) return [];

  const deduped = [];
  const seen = new Set();
  for (const part of raw.split(/[\s,]+/)) {
    const cleaned = part.trim();
    if (!cleaned) continue;
    if (seen.has(cleaned)) continue;
    seen.add(cleaned);
    deduped.push(cleaned);
  }
  return deduped;
}

export function normaliseCandidateLimit(rawValue) {
  const parsed = Number.parseInt(String(rawValue || ''), 10);
  if (!Number.isFinite(parsed)) return 25;
  return Math.min(100, Math.max(1, parsed));
}

export function buildRecommendationReviewRequest(values = {}, overrides = {}) {
  return {
    candidate_paper_concept_ids: parseCandidatePaperConceptIds(values.candidatePaperConceptIds),
    candidate_limit: normaliseCandidateLimit(values.candidateLimit),
    include_all_candidates: values.includeAllCandidates !== false,
    trigger_source: String(values.triggerSource || 'manual_review'),
    ...overrides,
  };
}

function formatRecommendationScalar(value, fallback = 'None') {
  const cleaned = String(value || '').trim();
  return cleaned || fallback;
}

export function resolveRecommendationRationaleSummary(row) {
  const summary = String(row?.rationale_summary || '').trim();
  if (summary) return summary;
  const rationaleText = String(row?.rationale_text || '').trim();
  if (rationaleText) return rationaleText;
  const rationaleValue = row?.rationale;
  if (Array.isArray(rationaleValue) && rationaleValue.length) {
    return rationaleValue
      .map((item) => String(item || '').trim())
      .filter(Boolean)
      .join(' ');
  }
  const rationale = String(rationaleValue || '').trim();
  if (rationale) return rationale;
  const generationStatus = String(row?.rationale_generation?.status || '').trim().toLowerCase();
  if (generationStatus === 'unavailable') {
    return 'No authoritative relevance explanation was generated for this recommendation yet.';
  }
  return '';
}

function appendRecommendationList(container, title, items) {
  if (!container || !Array.isArray(items) || !items.length) return;
  const heading = document.createElement('div');
  heading.className = 'speech-settings-note';
  heading.textContent = title;
  container.appendChild(heading);

  const list = document.createElement('ul');
  list.className = 'recommendation-review-list';
  for (const item of items) {
    const li = document.createElement('li');
    li.textContent = item;
    list.appendChild(li);
  }
  container.appendChild(list);
}

export function clearRecommendationReviewResults(
  summaryElement,
  containerElement,
  message = DEFAULT_REVIEW_EMPTY_MESSAGE,
) {
  if (summaryElement) {
    summaryElement.textContent = message;
  }
  if (containerElement) {
    containerElement.replaceChildren();
  }
}

export function renderRecommendationReviewPayload({
  summaryElement,
  containerElement,
  payload,
  cardEnhancer = null,
}) {
  if (!summaryElement || !containerElement) return;

  const trigger = payload?.trigger || {};
  const candidateSelection = payload?.candidate_selection || {};
  const report = payload?.recommendation_report || {};
  const authoritativeSurface = payload?.authoritative_review_surface || 'unknown';
  const externalAuthoritative = payload?.external_channels_authoritative === true;

  const summaryBits = [
    `Trigger: ${formatRecommendationScalar(trigger.label || trigger.trigger_source, 'manual review')}.`,
    `Candidate source: ${formatRecommendationScalar(candidateSelection.source, 'unknown')}.`,
    `Candidates: ${Number(candidateSelection.candidate_count || 0)}.`,
    `Authoritative surface: ${formatRecommendationScalar(authoritativeSurface)}.`,
    externalAuthoritative
      ? 'External channels are authoritative.'
      : 'External channels are not authoritative.',
  ];
  summaryElement.textContent = summaryBits.join(' ');
  containerElement.replaceChildren();

  if (!report?.success) {
    const panel = document.createElement('div');
    panel.className = 'recommendation-review-card is-skipped';

    const title = document.createElement('h4');
    title.className = 'recommendation-review-title';
    title.textContent = formatRecommendationScalar(
      report?.message,
      'Recommendation review could not produce ranked results.',
    );
    panel.appendChild(title);

    appendRecommendationList(panel, 'Suggestions', report?.suggestions || []);
    containerElement.appendChild(panel);
    return;
  }

  const results = Array.isArray(report?.results) ? report.results : [];
  if (!results.length) {
    const hint = document.createElement('div');
    hint.className = 'runtime-hint';
    hint.textContent = 'No recommendation rows were returned for this review.';
    containerElement.appendChild(hint);
    return;
  }

  for (const row of results) {
    const card = document.createElement('article');
    const status = String(row?.status || '').trim().toLowerCase();
    card.className = `recommendation-review-card${status === 'skipped' ? ' is-skipped' : ''}`;

    const header = document.createElement('div');
    header.className = 'recommendation-review-header';

    const title = document.createElement('h4');
    title.className = 'recommendation-review-title';
    title.textContent = formatRecommendationScalar(row?.paper_title, row?.paper_concept_id);
    header.appendChild(title);

    const badge = document.createElement('span');
    const tier = String(row?.recommendation_tier || status || 'unmatched').trim().toLowerCase();
    badge.className = `recommendation-review-badge ${status === 'skipped' ? 'status-skipped' : `tier-${tier}`}`;
    badge.textContent = status === 'skipped'
      ? 'Skipped'
      : `${formatRecommendationScalar(row?.recommendation_tier, 'unmatched')} · ${Number(
        row?.score || 0,
      ).toFixed(2)}`;
    header.appendChild(badge);
    card.appendChild(header);

    const meta = document.createElement('ul');
    meta.className = 'recommendation-review-meta';
    const metaItems = [
      `Paper concept: ${formatRecommendationScalar(row?.paper_concept_id)}`,
      `Publication date: ${formatRecommendationScalar(row?.paper_representation?.publication_date)}`,
    ];
    if (status === 'skipped') {
      metaItems.push(`Skip reason: ${formatRecommendationScalar(row?.skip_reason)}`);
    }
    for (const item of metaItems) {
      const li = document.createElement('li');
      li.textContent = item;
      meta.appendChild(li);
    }
    card.appendChild(meta);

    const summaryText = document.createElement('div');
    summaryText.className = 'recommendation-review-summary';
    summaryText.textContent = formatRecommendationScalar(
      resolveRecommendationRationaleSummary(row),
      'No authoritative relevance explanation was generated for this recommendation yet.',
    );
    card.appendChild(summaryText);

    appendRecommendationList(
      card,
      'Rationale',
      Array.isArray(row?.rationale) ? row.rationale : [],
    );

    const evidenceItems = Array.isArray(row?.evidence)
      ? row.evidence.map((item) => {
          const evidenceType = formatRecommendationScalar(item?.evidence_type);
          const profileValue = formatRecommendationScalar(item?.profile_value);
          const matchedText = formatRecommendationScalar(item?.matched_paper_text);
          const referenceId = formatRecommendationScalar(item?.paper_reference_id);
          return `${evidenceType}: ${profileValue} -> ${matchedText} (ref: ${referenceId})`;
        })
      : [];
    appendRecommendationList(card, 'Evidence', evidenceItems);

    const provenance = row?.provenance || {};
    const provenanceItems = [
      `Paper concept: ${formatRecommendationScalar(provenance?.paper_concept_id)}`,
      `Author concepts: ${Array.isArray(provenance?.author_concept_ids) && provenance.author_concept_ids.length
        ? provenance.author_concept_ids.join(', ')
        : 'None'}`,
      `Topic concepts: ${Array.isArray(provenance?.topic_concept_ids) && provenance.topic_concept_ids.length
        ? provenance.topic_concept_ids.join(', ')
        : 'None'}`,
      `Summary relation: ${formatRecommendationScalar(provenance?.summary_relation_id)}`,
    ];
    appendRecommendationList(card, 'Provenance', provenanceItems);

    if (status === 'skipped' && Array.isArray(row?.representation_failures) && row.representation_failures.length) {
      appendRecommendationList(card, 'Representation failures', row.representation_failures);
    }

    if (typeof cardEnhancer === 'function') {
      cardEnhancer({ card, row });
    }

    containerElement.appendChild(card);
  }
}
