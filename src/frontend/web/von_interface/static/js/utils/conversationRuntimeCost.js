function normaliseString(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : '';
}

function nonNegativeNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric >= 0 ? numeric : null;
}

function normaliseCost(cost) {
  const source = cost && typeof cost === 'object' ? cost : {};
  const status = normaliseString(source.status).toLowerCase() || 'unavailable';
  const currency = normaliseString(source.currency).toUpperCase() || null;
  const amount = status === 'estimated'
    ? nonNegativeNumber(source.amount)
    : nonNegativeNumber(source.known_amount);
  return { status, currency, amount };
}

function costForSummary(summary) {
  if (!summary || typeof summary !== 'object') return null;
  return normaliseCost(summary?.estimated_cost);
}

function callCount(summary) {
  return nonNegativeNumber(summary?.unique_call_count) ?? nonNegativeNumber(summary?.call_count);
}

function mergeCost(base, overlay) {
  const values = [base, overlay].filter(Boolean);
  if (values.length === 0) return { state: 'unavailable', currency: null };
  const known = values.filter((value) => value.amount !== null);
  if (known.some((value) => !value.currency)) {
    return { state: 'unavailable', reason: 'currency_unavailable', currency: null };
  }
  const currencies = new Set(known.map((value) => value.currency));
  if (currencies.size > 1) {
    return { state: 'unavailable', reason: 'currency_mismatch' };
  }
  const currency = known.find((value) => value?.currency)?.currency || null;
  const hasUnavailable = values.some((value) => value.status === 'unavailable');
  const hasPartial = values.some((value) => value.status === 'partial');
  const hasEstimated = values.some((value) => value.status === 'estimated');
  const hasBillableEvidence = values.some((value) => value.status !== 'not_applicable');
  if (known.length === 0) {
    if (!hasBillableEvidence) return { state: 'not_applicable', currency: null };
    return { state: hasPartial ? 'partial' : 'unavailable', currency };
  }
  const amount = known.reduce((total, value) => total + value.amount, 0);
  if (hasUnavailable || hasPartial) return { state: 'partial', currency, amount };
  if (hasEstimated) return { state: 'estimated', currency, amount };
  return { state: 'not_applicable', currency };
}

function formatAmount(amount, currency) {
  if (amount === null) return '';
  try {
    return new Intl.NumberFormat('en-NZ', {
      style: 'currency',
      currency,
      minimumFractionDigits: amount > 0 && amount < 0.01 ? 4 : 2,
      maximumFractionDigits: 6,
    }).format(amount);
  } catch (_) {
    return `${currency === 'USD' ? 'US$' : `${currency} `}${amount.toFixed(6)}`;
  }
}

function describeCost(cost, { compact = false } = {}) {
  if (cost.state === 'estimated') return formatAmount(cost.amount, cost.currency);
  if (cost.state === 'partial') {
    return cost.amount === undefined ? 'partial' : `known ${formatAmount(cost.amount, cost.currency)} (partial)`;
  }
  if (cost.state === 'not_applicable') return compact ? 'no billable calls' : 'No billable model calls';
  return compact ? 'unavailable' : 'estimate unavailable';
}

function summaryDetailLines(label, summary, mergedCost) {
  const usage = summary?.usage && typeof summary.usage === 'object' ? summary.usage : {};
  const lines = [
    `${label}: ${describeCost(mergedCost)}`,
  ];
  const calls = callCount(summary);
  if (calls !== null) lines.push(`${label} model calls: ${calls.toLocaleString('en-NZ')}`);
  const estimatedCost = summary?.estimated_cost && typeof summary.estimated_cost === 'object'
    ? summary.estimated_cost
    : {};
  const pricedCalls = nonNegativeNumber(estimatedCost.priced_call_count);
  const partialCalls = nonNegativeNumber(estimatedCost.partial_call_count);
  const unpricedCalls = nonNegativeNumber(estimatedCost.unpriced_call_count);
  if (pricedCalls !== null || partialCalls !== null || unpricedCalls !== null) {
    lines.push(`${label} priced/partial/unpriced calls: ${pricedCalls ?? 0}/${partialCalls ?? 0}/${unpricedCalls ?? 0}`);
  }
  if (normaliseString(usage.status)) lines.push(`${label} token coverage: ${usage.status}`);
  const coverage = (summary?.runtime_coverage && typeof summary.runtime_coverage === 'object')
    ? summary.runtime_coverage
    : (summary?.coverage && typeof summary.coverage === 'object' ? summary.coverage : {});
  if (normaliseString(coverage.status)) lines.push(`${label} cost coverage: ${coverage.status}`);
  const pricing = summary?.estimated_cost?.pricing && typeof summary.estimated_cost.pricing === 'object'
    ? summary.estimated_cost.pricing
    : {};
  if (normaliseString(pricing.version)) lines.push(`Pricing version: ${pricing.version}`);
  if (normaliseString(pricing.effective_at_utc)) lines.push(`Pricing effective date: ${pricing.effective_at_utc}`);
  const pricingVersions = Array.isArray(summary?.estimated_cost?.pricing_versions)
    ? summary.estimated_cost.pricing_versions.map(normaliseString).filter(Boolean)
    : [];
  if (!normaliseString(pricing.version) && pricingVersions.length > 0) {
    lines.push(`Pricing versions: ${pricingVersions.join(', ')}`);
  }
  return lines;
}

/**
 * Convert the actor-scoped cost snapshot emitted by chatTab into presentation
 * strings. Missing evidence stays unavailable; it never becomes a zero cost.
 */
export function formatConversationRuntimeCostFooter(snapshot) {
  const source = snapshot && typeof snapshot === 'object' ? snapshot : {};
  const context = source.context && typeof source.context === 'object' ? source.context : null;
  const baseline = source.baseline && typeof source.baseline === 'object' ? source.baseline : null;
  const live = source.live && typeof source.live === 'object' ? source.live : null;
  const isLoading = source.loading === true;
  const conversationBase = baseline?.conversation || null;
  const restartBase = baseline?.since_restart || null;
  const liveSummary = live?.summary && typeof live.summary === 'object' ? live.summary : null;
  const overlay = liveSummary ? costForSummary(liveSummary) : null;
  let chatCost = mergeCost(costForSummary(conversationBase), overlay);
  let restartCost = mergeCost(costForSummary(restartBase), overlay);
  const liveCalculating = live?.active === true && !liveSummary;

  // The live request may have a priced result before the persisted aggregate
  // arrives. It proves only a subtotal: earlier conversation/run calls are
  // still unknown, so never present it as the complete total.
  if (!baseline && liveSummary) {
    if (chatCost.state === 'estimated') chatCost = { ...chatCost, state: 'partial' };
    if (restartCost.state === 'estimated') restartCost = { ...restartCost, state: 'partial' };
  }

  // A retained baseline is useful during a transient refresh failure, but it
  // is no longer proof of the current total. Keep its known subtotal visible
  // while degrading complete/no-call claims rather than silently showing stale
  // evidence as current.
  if (baseline && source.error) {
    chatCost = chatCost.state === 'estimated'
      ? { ...chatCost, state: 'partial' }
      : (chatCost.state === 'not_applicable' ? { state: 'unavailable', currency: null } : chatCost);
    restartCost = restartCost.state === 'estimated'
      ? { ...restartCost, state: 'partial' }
      : (restartCost.state === 'not_applicable' ? { state: 'unavailable', currency: null } : restartCost);
  }

  if (!context?.conversation_session_id) {
    return {
      visible: false,
      state: 'unavailable',
      desktopText: '',
      mobileText: '',
      ariaLabel: '',
      title: '',
      details: [],
    };
  }

  if (!baseline && !isLoading && !live && !source.error) {
    return {
      visible: false,
      state: 'unavailable',
      desktopText: '',
      mobileText: '',
      ariaLabel: '',
      title: '',
      details: [],
    };
  }

  if (!baseline && (isLoading || liveCalculating)) {
    return {
      visible: true,
      state: 'calculating',
      desktopText: 'Est. cost: calculating…',
      mobileText: 'Cost calculating…',
      ariaLabel: 'Estimated conversation cost is calculating.',
      title: 'Estimated cost is calculating from provider-reported usage. It is not a provider invoice.',
      details: [],
    };
  }

  const desktopText = `Est. cost: Chat ${describeCost(chatCost)} · Since restart ${describeCost(restartCost)}`;
  const mobileText = `Chat ${describeCost(chatCost, { compact: true })} · Run ${describeCost(restartCost, { compact: true })}`;
  const details = [
    ...summaryDetailLines('Chat', conversationBase, chatCost),
    ...summaryDetailLines('Since restart', restartBase, restartCost),
  ];
  if (live?.request_id) details.push(`Current request: ${live.request_id}`);
  if (source.runtime?.started_at_utc) details.push(`Runtime started: ${source.runtime.started_at_utc}`);
  if (source.error) details.push('Latest refresh unavailable; showing retained evidence as a known subtotal.');
  details.push('Estimate from provider-reported usage; not a provider invoice.');
  return {
    visible: true,
    state: chatCost.state === 'estimated' && restartCost.state === 'estimated' ? 'estimated' : chatCost.state,
    desktopText,
    mobileText,
    ariaLabel: `${desktopText}. ${details.join('. ')}`,
    title: details.join('\n'),
    details,
  };
}
