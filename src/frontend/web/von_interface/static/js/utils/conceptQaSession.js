import {
  ensureUniqueWindowSessionId,
  WINDOW_SESSION_HEADER,
} from '../apiService.js';
import { getCurrentUserConceptId } from '../domUtils.js';

export const CONCEPT_QA_ORIGIN_KIND = 'concept_q_and_a';
export const CONCEPT_QA_MODE = 'concept_q_and_a';

function cleanText(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function firstObject(...values) {
  return values.find((value) => value && typeof value === 'object' && !Array.isArray(value)) || null;
}

function normaliseLifecycle(value) {
  const token = cleanText(value).toLowerCase().replace(/[\s-]+/g, '_');
  if (['finished', 'completed', 'complete', 'ended'].includes(token)) return 'finished';
  if (['cancelled', 'canceled', 'abandoned'].includes(token)) return 'cancelled';
  return token === 'active' || token === 'open' || token === 'in_progress'
    ? 'active'
    : (token || 'active');
}

function normaliseActionList(value, lifecycle) {
  const supplied = Array.isArray(value)
    ? value.map(cleanText).filter(Boolean)
    : [];
  if (supplied.length) return [...new Set(supplied)];
  if (lifecycle === 'active') return ['submit_turn', 'finish', 'cancel', 'open_transcript', 'copy_reference'];
  return ['open_transcript', 'copy_reference'];
}

function normaliseInitialQuestion(value) {
  const source = firstObject(value) || {};
  const status = cleanText(source.status).toLowerCase().replace(/[\s-]+/g, '_');
  const failure = firstObject(source.failure);
  return {
    status: status || 'ready',
    retryable: source.retryable === true || status === 'retryable_failure',
    attempt_count: Number.isInteger(source.attempt_count) ? source.attempt_count : 0,
    failure: failure ? {
      error_code: cleanText(failure.error_code) || null,
      message: cleanText(failure.message) || null,
    } : null,
  };
}

export function normaliseConceptQaReceipts(value) {
  const source = firstObject(value?.receipts, value?.representation, value) || {};
  return {
    exact_input: firstObject(source.exact_input, source.exact_answer) || null,
    formalisation: firstObject(source.formalisation, source.formalization) || null,
    notes: firstObject(source.notes, source.concept_notes) || null,
  };
}

export function normaliseConceptQaSession(value, defaults = {}) {
  const root = firstObject(value) || {};
  const source = firstObject(
    root.qa_session,
    root.q_and_a_session,
    root.session,
    root.active_session,
    root.concept_q_and_a,
    root,
  ) || {};
  const canonicalRef = firstObject(
    source.conversation_reference,
    source.conversation_ref?.conversation_ref,
    root.conversation_reference,
  );
  const sessionId = cleanText(source.session_id)
    || cleanText(source.chat_session_id)
    || cleanText(canonicalRef?.session_id)
    || cleanText(defaults.sessionId);
  if (!sessionId) return null;
  const lifecycle = normaliseLifecycle(
    source.lifecycle?.status
      || source.lifecycle?.state
      || source.lifecycle
      || source.status
      || defaults.lifecycle,
  );
  const conceptId = cleanText(source.focal_concept_id)
    || cleanText(source.concept_id)
    || cleanText(source.target_concept_id)
    || (Array.isArray(source.focal_concept_ids)
      ? cleanText(source.focal_concept_ids.find((item) => cleanText(item)))
      : '')
    || cleanText(defaults.conceptId);
  const turns = Array.isArray(source.turns)
    ? source.turns
    : (Array.isArray(root.turns) ? root.turns : []);
  const receipts = normaliseConceptQaReceipts(root.receipts || source.receipts || source.latest_receipts);
  const initialQuestion = normaliseInitialQuestion(source.initial_question);
  const availableActions = normaliseActionList(
    source.available_actions || source.allowed_actions,
    lifecycle,
  ).filter((action) => !(initialQuestion.retryable && action === 'submit_turn'));

  return {
    ...source,
    session_id: sessionId,
    chat_session_id: cleanText(source.chat_session_id) || sessionId,
    interaction_id: cleanText(source.interaction_id) || null,
    concept_id: conceptId || null,
    focal_concept_id: conceptId || null,
    mode: CONCEPT_QA_MODE,
    origin_kind: CONCEPT_QA_ORIGIN_KIND,
    lifecycle,
    status: lifecycle,
    available_actions: availableActions,
    conversation_reference: canonicalRef || null,
    turns,
    receipts,
    initial_question: initialQuestion,
    created: root.created === true || source.created === true,
    resumed: root.resumed === true || source.resumed === true,
    updated_at: cleanText(source.updated_at) || cleanText(root.updated_at) || null,
  };
}

export function normaliseConceptQaProjection(value, defaults = {}) {
  const source = firstObject(value) || {};
  const sessions = Array.isArray(source.sessions)
    ? source.sessions.map((session) => normaliseConceptQaSession(session, defaults)).filter(Boolean)
    : [];
  const activeSession = normaliseConceptQaSession(source.active_session, defaults)
    || sessions.find((session) => session.lifecycle === 'active')
    || null;
  return {
    sessions,
    active_session: activeSession,
    legacy_interactions: Array.isArray(source.legacy_interactions)
      ? source.legacy_interactions
      : (Array.isArray(source.legacy_active_interactions) ? source.legacy_active_interactions : []),
  };
}

export function isConceptQaSession(value) {
  const mode = cleanText(value?.mode).toLowerCase().replace(/[\s-]+/g, '_');
  const originKind = cleanText(value?.origin_kind).toLowerCase().replace(/[\s-]+/g, '_');
  return mode === CONCEPT_QA_MODE || originKind === CONCEPT_QA_ORIGIN_KIND;
}

export function getConceptQaElicitationGap(value) {
  const source = firstObject(value?.session, value) || {};
  let candidate = firstObject(source.current_gap, source.elicitation_gap);
  if (!candidate) {
    const turns = Array.isArray(source.turns) ? source.turns : [];
    for (let index = turns.length - 1; index >= 0; index -= 1) {
      const turn = turns[index];
      if (turn?.role !== 'assistant') continue;
      candidate = firstObject(
        turn?.concept_q_and_a?.elicitation_predicate,
        turn?.elicitation_predicate,
      );
      if (candidate) break;
    }
  }
  if (!candidate) return null;
  const predicateId = cleanText(candidate.predicate_concept_id);
  const predicateLabel = cleanText(candidate.predicate_label) || predicateId;
  const status = cleanText(candidate.status).toLowerCase();
  const gapStatus = cleanText(candidate.gap_status).toLowerCase();
  if (!predicateLabel || (status !== 'missing' && gapStatus !== 'asserted_relation_missing')) {
    return null;
  }
  return {
    predicate_concept_id: predicateId || null,
    predicate_label: predicateLabel,
    status: status || 'missing',
    gap_status: gapStatus || null,
    requirement_kind: cleanText(candidate.requirement_kind) || null,
    priority_class: cleanText(candidate.priority_class) || null,
    priority: Number.isFinite(Number(candidate.priority)) ? Number(candidate.priority) : null,
    declared_on_type_concept_id: cleanText(candidate.declared_on_type_concept_id) || null,
    inherited: candidate.inherited === true,
  };
}

export function describeConceptQaElicitationGap(value) {
  const gap = getConceptQaElicitationGap(value);
  if (!gap) return '';
  if (gap.priority_class === 'constitutive'
      || gap.requirement_kind === 'constitutive_relation') {
    return [
      `Current constitutive gap: ${gap.predicate_label} — asserted relation missing.`,
      'This is a prioritised Q&A target, not an assertion or a creation block.',
    ].join(' ');
  }
  const priority = gap.priority === null ? '' : ` (priority ${gap.priority})`;
  return [
    `Current suggested relation: ${gap.predicate_label}${priority}.`,
    'This is a salient Q&A target, not a requirement or an asserted relation.',
  ].join(' ');
}

export function conceptQaActionAllowed(session, action) {
  const projection = normaliseConceptQaSession(session);
  if (!projection) return false;
  const actions = new Set(projection.available_actions);
  if (actions.has(action)) return true;
  if (action === 'submit_turn') {
    return projection.lifecycle === 'active'
      && projection.initial_question?.retryable !== true;
  }
  return ['open_transcript', 'copy_reference'].includes(action);
}

export function createConceptQaTurnId() {
  try {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  } catch (_) {
    // Fall through to a collision-resistant browser-local identifier.
  }
  return `qa-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

async function requestJson(url, { method = 'GET', body = null } = {}) {
  const windowSessionId = await ensureUniqueWindowSessionId();
  const headers = {
    'Accept': 'application/json',
    'Content-Type': 'application/json',
    [WINDOW_SESSION_HEADER]: windowSessionId,
  };
  const userConceptId = getCurrentUserConceptId();
  if (userConceptId) headers['X-User-Concept-ID'] = userConceptId;
  const response = await fetch(url, {
    method,
    headers,
    cache: 'no-store',
    ...(body === null ? {} : { body: JSON.stringify(body) }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload?.error || payload?.message || `HTTP ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function conceptQaBase(conceptId) {
  return `/api/concepts/${encodeURIComponent(cleanText(conceptId))}/q_and_a`;
}

export async function fetchConceptQaProjection(conceptId) {
  const payload = await requestJson(conceptQaBase(conceptId));
  return normaliseConceptQaProjection(payload, { conceptId });
}

export async function startConceptQaSession(conceptId, body = {}) {
  const payload = await requestJson(`${conceptQaBase(conceptId)}/start`, {
    method: 'POST',
    body,
  });
  const session = normaliseConceptQaSession(payload, { conceptId });
  if (!session) throw new Error('The server did not return a concept Q&A session.');
  return session;
}

export async function submitConceptQaTurn({ conceptId, sessionId, answer, notesInput = '' }) {
  const turnId = createConceptQaTurnId();
  const payload = await requestJson(
    `${conceptQaBase(conceptId)}/sessions/${encodeURIComponent(cleanText(sessionId))}/turns`,
    {
      method: 'POST',
      body: {
        turn_id: turnId,
        answer: String(answer || ''),
        ...(String(notesInput || '').trim() ? { notes_input: String(notesInput) } : {}),
      },
    },
  );
  const session = normaliseConceptQaSession(payload, { conceptId, sessionId });
  if (!session) throw new Error('The server did not return the updated concept Q&A session.');
  return { payload, session, turn_id: turnId, receipts: normaliseConceptQaReceipts(payload) };
}

export async function transitionConceptQaSession({ conceptId, sessionId, action }) {
  if (!['finish', 'cancel'].includes(action)) throw new Error('Unsupported concept Q&A transition.');
  const payload = await requestJson(
    `${conceptQaBase(conceptId)}/sessions/${encodeURIComponent(cleanText(sessionId))}/${action}`,
    { method: 'POST', body: {} },
  );
  const session = normaliseConceptQaSession(payload, { conceptId, sessionId });
  if (!session) throw new Error('The server did not return the concept Q&A lifecycle read-back.');
  return session;
}

function resolveFormalisationConfirmationUrl({ conceptId, sessionId, assertionId, confirmation }) {
  const projected = cleanText(confirmation?.action);
  const expectedPrefix = `${conceptQaBase(conceptId)}/sessions/${encodeURIComponent(cleanText(sessionId))}/formalisations/`;
  if (projected) {
    try {
      const resolved = new URL(projected, globalThis.location?.origin || 'http://localhost');
      const currentOrigin = globalThis.location?.origin || resolved.origin;
      if (resolved.origin === currentOrigin && resolved.pathname.startsWith(expectedPrefix)) {
        return `${resolved.pathname}${resolved.search}`;
      }
    } catch (_) {
      // Use the fixed same-origin route below.
    }
  }
  return `${expectedPrefix}${encodeURIComponent(cleanText(assertionId))}/confirm`;
}

export async function confirmConceptQaFormalisation({
  conceptId,
  sessionId,
  assertionId,
  confirmation = null,
}) {
  if (!cleanText(assertionId)) throw new Error('A tentative assertion ID is required.');
  const url = resolveFormalisationConfirmationUrl({
    conceptId,
    sessionId,
    assertionId,
    confirmation,
  });
  const payload = await requestJson(url, {
    // Confirmation is deliberately a bounded POST even if a malformed client
    // projection advertises another method.
    method: 'POST',
    body: {
      request_id: createConceptQaTurnId(),
      ...(Number.isInteger(confirmation?.expected_receipt_revision)
        ? { expected_receipt_revision: confirmation.expected_receipt_revision }
        : {}),
    },
  });
  const session = normaliseConceptQaSession(payload, { conceptId, sessionId });
  if (!session) throw new Error('The server did not return confirmation read-back.');
  return { payload, session, receipts: normaliseConceptQaReceipts(payload) };
}

export const _test = {
  normaliseLifecycle,
  resolveFormalisationConfirmationUrl,
};
