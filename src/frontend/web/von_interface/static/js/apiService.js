// ============================================================================
// Window Session Support (JVNAUTOSCI-1011)
// ============================================================================
// Each browser window/tab gets a unique session ID stored in sessionStorage.
// This allows different windows to have different organisation contexts without
// interfering with each other.

const WINDOW_SESSION_HEADER = 'X-Von-Window-Session';

import { getSessionScopedOrgId } from './utils/sessionScopedStorage.js';
import { parseStoredContextValue } from './utils/runtimeIdentityBootstrap.js';
import {
  createWindowSessionIdentityCoordinator,
  WINDOW_SESSION_KEY
} from './utils/windowSessionIdentity.js';

const windowSessionIdentityCoordinator = createWindowSessionIdentityCoordinator();

/**
 * Get or generate a unique window session ID.
 * This ID is stored in sessionStorage (window-scoped, not shared across tabs).
 */
function getWindowSessionId() {
  return windowSessionIdentityCoordinator.getWindowSessionId();
}

/**
 * Resolve copied sessionStorage IDs before the tab's first actor-scoped call.
 * Existing tabs retain their ID; only a newly probing colliding document
 * rotates to a fresh server window context.
 */
async function ensureUniqueWindowSessionId() {
  return windowSessionIdentityCoordinator.ensureUniqueWindowSessionId();
}

/**
 * Replace a tab ID that the server has authoritatively bound to another actor.
 * The rejected request has no effect, so callers may retry once with the new
 * opaque ID while the authenticated server session remains the actor authority.
 */
async function replaceMismatchedWindowSessionId(expectedSessionId) {
  return windowSessionIdentityCoordinator.replaceWindowSessionId(expectedSessionId);
}

/**
 * Builds the standard headers object for fetch requests.
 * Includes Content-Type and the window session header.
 */
async function buildHeaders(extraHeaders = {}) {
  const windowSessionId = await ensureUniqueWindowSessionId();
  return {
    'Content-Type': 'application/json',
    [WINDOW_SESSION_HEADER]: windowSessionId,
    ...extraHeaders
  };
}

// Expose for debugging/testing
export {
  ensureUniqueWindowSessionId,
  getWindowSessionId,
  replaceMismatchedWindowSessionId,
  WINDOW_SESSION_HEADER,
  WINDOW_SESSION_KEY
};

// ============================================================================
// Standard HTTP Helpers with Window Session Support
// ============================================================================

function buildHttpError(response, payload) {
  const err = new Error(
    (payload && (payload.error || payload.message)) || `HTTP ${response.status}`
  );
  err.status = response.status;
  err.payload = payload;
  const retryAfterHeader = response.headers?.get?.('Retry-After');
  const parsedRetryAfter = Number.parseInt(retryAfterHeader || '', 10);
  if (Number.isFinite(parsedRetryAfter)) {
    err.retryAfterSeconds = parsedRetryAfter;
  }
  return err;
}

async function readJsonPayload(response) {
  try {
    return await response.json();
  } catch (_) {
    return null;
  }
}

function isWindowSessionActorMismatch(response, payload) {
  return response.status === 403
    && payload?.error_code === 'window_session_actor_mismatch';
}

export async function getJson(url) {
  const res = await fetch(url, {
    method: 'GET',
    headers: await buildHeaders()
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function getJsonDetailed(url, options = {}) {
  const {
    headers: extraHeaders = {},
    method = 'GET',
    ...fetchOptions
  } = options || {};

  const res = await fetch(url, {
    method,
    headers: await buildHeaders(extraHeaders),
    ...fetchOptions
  });

  let data;
  try {
    data = await res.json();
  } catch (_) {
    data = null;
  }

  if (!res.ok) {
    const err = new Error(
      (data && (data.error || data.message)) || `HTTP ${res.status}`
    );
    err.status = res.status;
    err.payload = data;

    const retryAfterHeader = res.headers.get('Retry-After');
    const parsedRetryAfter = Number.parseInt(retryAfterHeader || '', 10);
    if (Number.isFinite(parsedRetryAfter)) {
      err.retryAfterSeconds = parsedRetryAfter;
    }

    throw err;
  }

  return {
    data,
    status: res.status,
    headers: res.headers
  };
}

export async function postJson(url, data) {
  let headers = await buildHeaders();
  let res = await fetch(url, {
    method: 'POST',
    headers,
    body: data ? JSON.stringify(data) : '{}'
  });
  if (res.ok) return res.json();

  let payload = await readJsonPayload(res);
  if (isWindowSessionActorMismatch(res, payload)) {
    await replaceMismatchedWindowSessionId(headers[WINDOW_SESSION_HEADER]);
    headers = await buildHeaders();
    res = await fetch(url, {
      method: 'POST',
      headers,
      body: data ? JSON.stringify(data) : '{}'
    });
    if (res.ok) return res.json();
    payload = await readJsonPayload(res);
  }
  throw buildHttpError(res, payload);
}

export async function postJsonDetailed(url, data, options = {}) {
  const {
    headers: extraHeaders = {},
    method = 'POST',
    ...fetchOptions
  } = options || {};

  const res = await fetch(url, {
    method,
    headers: await buildHeaders(extraHeaders),
    body: JSON.stringify(data || {}),
    ...fetchOptions
  });

  let payload;
  try {
    payload = await res.json();
  } catch (_) {
    payload = null;
  }

  if (!res.ok) {
    const err = new Error(
      (payload && (payload.error || payload.message)) || `HTTP ${res.status}`
    );
    err.status = res.status;
    err.payload = payload;
    throw err;
  }

  return {
    data: payload,
    status: res.status,
    headers: res.headers
  };
}

export async function fetchWithTimeout(url, options = {}) {
  const { timeoutMs = 0, signal: parentSignal = null, ...fetchOptions } = options || {};

  if (typeof AbortController !== 'function' || timeoutMs <= 0) {
    return fetch(url, parentSignal ? { ...fetchOptions, signal: parentSignal } : fetchOptions);
  }

  const controller = new AbortController();
  let timeoutId;
  let releaseParentAbort = null;
  let timedOut = false;

  if (parentSignal?.aborted) {
    controller.abort();
  } else if (parentSignal && typeof parentSignal.addEventListener === 'function') {
    const forwardAbort = () => {
      try {
        controller.abort();
      } catch (_) {
        // Ignore abort races.
      }
    };
    parentSignal.addEventListener('abort', forwardAbort, { once: true });
    releaseParentAbort = () => {
      try {
        parentSignal.removeEventListener('abort', forwardAbort);
      } catch (_) {
        // Ignore detach races.
      }
    };
  }

  timeoutId = setTimeout(() => {
    timedOut = true;
    try {
      controller.abort();
    } catch (_) {
      // Ignore abort races.
    }
  }, timeoutMs);

  try {
    return await fetch(url, {
      ...fetchOptions,
      signal: controller.signal
    });
  } catch (err) {
    if (timedOut && err && typeof err === 'object') {
      try {
        Object.defineProperty(err, 'vonTimeout', {
          configurable: true,
          enumerable: false,
          value: true
        });
      } catch (_) {
        err.vonTimeout = true;
      }
    }
    throw err;
  } finally {
    if (timeoutId) clearTimeout(timeoutId);
    if (typeof releaseParentAbort === 'function') {
      releaseParentAbort();
    }
  }
}

export async function putJson(url, data, opts = {}) {
  const extraHeaders = opts.headers || {};
  const res = await fetch(url, {
    method: 'PUT',
    headers: await buildHeaders(extraHeaders),
    body: JSON.stringify(data)
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// PATCH helper (e.g., partial updates like notes)
export async function patchJson(url, data, opts = {}) {
  const extraHeaders = opts.headers || {};
  const res = await fetch(url, {
    method: 'PATCH',
    headers: await buildHeaders(extraHeaders),
    body: JSON.stringify(data || {})
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function deleteJson(url, data) {
  const options = {
    method: 'DELETE',
    headers: await buildHeaders()
  };
  if (data !== undefined) {
    options.body = JSON.stringify(data);
  }
  const res = await fetch(url, options);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/**
 * Get current user context from sessionStorage/localStorage for request-scoped identity.
 * Returns object with user_id, org_id, language fields (all optional).
 * Connector administration preferences are deliberately excluded: a
 * browser-wide OAuth/profile selector is not conversational resource scope.
 *
 * JVNAUTOSCI-1011: Now reads org context from sessionStorage first (window-scoped),
 * falling back to localStorage for backward compatibility.
 *
 * Call this to attach context to annotation/elicitation requests so backend
 * can log identity per-request without session state conflicts between multiple clients.
 */
export function getUserContext() {
  const ctx = {};

  try {
    const storedUser = parseStoredContextValue(localStorage.getItem('von_current_user'));
    // Prefer concept_id (e.g., #V#michael_witbrock) over id (MongoDB ObjectID)
    // Settings page may populate either field depending on data source
    if (storedUser) {
      ctx.user_id = storedUser.concept_id || storedUser.id || null;
    }
  } catch (e) {
    console.debug('[context] Failed to parse von_current_user', e);
  }

  // JVNAUTOSCI-1011: Use central helper for session-scoped org context
  ctx.org_id = getSessionScopedOrgId();

  // Get language preference from localStorage or fallback
  try {
    ctx.language = localStorage.getItem('von_preferred_language') || 'en-NZ';
  } catch (_e) {
    ctx.language = 'en-NZ';
  }

  return ctx;
}

/**
 * Attach user context to a payload object (mutates in place for performance).
 * Adds a 'context' field with user_id, org_id, language.
 */
export function attachUserContext(payload) {
  if (!payload || typeof payload !== 'object') return payload;
  payload.context = getUserContext();
  return payload;
}

// Send a per-turn annotation payload to the backend
export async function annotateTurn(payload) {
  try {
    // Automatically attach user context for request-scoped identity
    attachUserContext(payload);

    console.info('[annotations] annotateTurn request', payload);
    const res = await fetch('/api/annotations/turn', {
      method: 'POST',
      headers: await buildHeaders(),
      body: JSON.stringify(payload)
    });
    const text = await res.text();
    if (!res.ok) {
      console.warn('[annotations] annotateTurn non-OK response', res.status, text);
      throw new Error(`HTTP ${res.status}`);
    }
    try {
      const json = JSON.parse(text);
      console.info('[annotations] annotateTurn response (parsed)', json);
      return json;
    } catch (_e) {
      console.info('[annotations] annotateTurn response (raw text)', text);
      // Return raw text as fallback
      return text;
    }
  } catch (err) {
    console.error('[annotations] annotateTurn error', err);
    throw err;
  }
}

export async function acceptAnnotation(payload) {
  // payload: { turn_id, span: {start,end,text}, candidate: {concept_id|id|name}, user_id? }
  try {
    // Automatically attach user context for request-scoped identity
    attachUserContext(payload);

    const res = await fetch('/api/annotations/accept', {
      method: 'POST',
      headers: await buildHeaders(),
      body: JSON.stringify(payload)
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  } catch (e) {
    console.error('[annotations] acceptAnnotation error', e);
    throw e;
  }
}

export async function revokeAnnotation(payload) {
  // payload: { turn_id? , candidate_id? , object_text? }
  try {
    const res = await fetch('/api/annotations/revoke', {
      method: 'POST',
      headers: await buildHeaders(),
      body: JSON.stringify(payload)
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  } catch (e) {
    console.error('[annotations] revokeAnnotation error', e);
    throw e;
  }
}

// Lightweight ontological type search (corrected path /vontology/api/vontology/search)
// Adds fallback_substring=1 to broaden partial token recall. Future: includeIndividuals flag.
export async function searchTypes(q, limit = 8, opts = {}) {
  if (!q || !q.trim()) return [];
  const cleaned = q.trim();
  const includeIndividuals = !!opts.includeIndividuals;
  // Compose query params explicitly to avoid accidental omission drift
  const params = new URLSearchParams();
  params.set('q', cleaned);
  params.set('limit', String(limit));
  params.set('fallback_substring', '1');
  if (includeIndividuals) params.set('include_individuals', '1');
  const url = `/vontology/api/vontology/search?${params.toString()}`;
  const t0 = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
  try {
    const res = await fetch(url, { headers: { 'Accept': 'application/json' } });
    const text = await res.text();
    if (!res.ok) {
      console.warn('[annotations] searchTypes HTTP non-OK', res.status, text.slice(0, 180));
      return [];
    }
    let data = null;
    try { data = JSON.parse(text); } catch (e) { console.warn('[annotations] searchTypes parse fail', e); return []; }
    if (!data || !Array.isArray(data.results)) return [];
    const elapsed = ((typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now()) - t0;
    if (elapsed > 400) {
      console.info('[annotations] searchTypes slow query', { q: cleaned, ms: Math.round(elapsed), count: data.results.length });
    }
    const filtered = includeIndividuals ? data.results : data.results.filter(r => r.kind === 'type');
    return filtered;
  } catch (e) {
    console.warn('[annotations] searchTypes failed', e);
    return [];
  }
}

// Create one explicitly typed concept through the governed HTTP boundary.
export async function createConcept(parentConceptId, name, kind, fields = {}) {
  const cleanName = String(name || '').trim();
  if (!cleanName) throw new Error('name required');
  if (!['instance', 'type', 'predicate'].includes(kind)) {
    throw new Error("kind must be 'instance', 'type', or 'predicate'");
  }

  const payload = { ...fields, name: cleanName, kind };
  if (parentConceptId) payload.parent_concept_ids = [parentConceptId];
  const { data } = await postJsonDetailed('/api/concepts/', payload);
  return data;
}

// Create a new instance under a selected parent type concept.
export async function createInstance(parentConceptId, name) {
  if (!parentConceptId) throw new Error('parentConceptId required');
  try {
    const response = await createConcept(parentConceptId, name, 'instance');
    return response?.concept || response;
  } catch (e) {
    console.error('[annotations] createInstance error', e);
    throw e;
  }
}

// Create a new TYPE (subtype) under a selected parent type concept.
export async function createType(parentConceptId, name) {
  if (!parentConceptId) throw new Error('parentConceptId required');
  try {
    const response = await createConcept(parentConceptId, name, 'type');
    return response?.concept || response;
  } catch (e) {
    console.error('[annotations] createType error', e);
    throw e;
  }
}
