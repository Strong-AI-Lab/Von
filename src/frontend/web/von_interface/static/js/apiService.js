// ============================================================================
// Window Session Support (JVNAUTOSCI-1011)
// ============================================================================
// Each browser window/tab gets a unique session ID stored in sessionStorage.
// This allows different windows to have different organisation contexts without
// interfering with each other.

const WINDOW_SESSION_KEY = 'von_window_session_id';
const WINDOW_SESSION_HEADER = 'X-Von-Window-Session';

import { getSessionScopedOrgId } from './utils/sessionScopedStorage.js';
import { parseStoredContextValue } from './utils/runtimeIdentityBootstrap.js';

/**
 * Get or generate a unique window session ID.
 * This ID is stored in sessionStorage (window-scoped, not shared across tabs).
 */
function getWindowSessionId() {
  let sessionId = sessionStorage.getItem(WINDOW_SESSION_KEY);
  if (!sessionId) {
    // Generate a new UUID-like session ID
    const randomId =
      (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function')
        ? crypto.randomUUID().replace(/-/g, '')
        : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
    sessionId = 'ws_' + randomId;
    sessionStorage.setItem(WINDOW_SESSION_KEY, sessionId);
    console.debug('[windowSession] Generated new window session:', sessionId);
  }
  return sessionId;
}

/**
 * Builds the standard headers object for fetch requests.
 * Includes Content-Type and the window session header.
 */
function buildHeaders(extraHeaders = {}) {
  return {
    'Content-Type': 'application/json',
    [WINDOW_SESSION_HEADER]: getWindowSessionId(),
    ...extraHeaders
  };
}

// Expose for debugging/testing
export { getWindowSessionId, WINDOW_SESSION_HEADER, WINDOW_SESSION_KEY };

// ============================================================================
// Standard HTTP Helpers with Window Session Support
// ============================================================================

export async function getJson(url) {
  const res = await fetch(url, {
    method: 'GET',
    headers: buildHeaders()
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
    headers: buildHeaders(extraHeaders),
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
  const res = await fetch(url, {
    method: 'POST',
    headers: buildHeaders(),
    body: data ? JSON.stringify(data) : '{}'
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function postJsonDetailed(url, data, options = {}) {
  const {
    headers: extraHeaders = {},
    method = 'POST',
    ...fetchOptions
  } = options || {};

  const res = await fetch(url, {
    method,
    headers: buildHeaders(extraHeaders),
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
    headers: buildHeaders(extraHeaders),
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
    headers: buildHeaders(extraHeaders),
    body: JSON.stringify(data || {})
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function deleteJson(url) {
  const res = await fetch(url, {
    method: 'DELETE',
    headers: buildHeaders()
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/**
 * Get current user context from sessionStorage/localStorage for request-scoped identity.
 * Returns object with user_id, org_id, language fields (all optional).
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

  try {
    ctx.gmail_profile = localStorage.getItem('von_gmail_profile') || null;
  } catch (_e) {
    ctx.gmail_profile = null;
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
      headers: buildHeaders(),
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
      headers: buildHeaders(),
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
      headers: buildHeaders(),
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

// Create a new instance under a selected parent type concept
export async function createInstance(parentConceptId, name) {
  if (!parentConceptId || !name) throw new Error('parentConceptId and name required');
  const payload = { parent_id: parentConceptId, new_concept_name: name, create_as_instance: true };
  const url = '/vontology/api/vontology/create_concept';
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const text = await res.text();
    let json = {};
    try { json = text ? JSON.parse(text) : {}; } catch (_) { json = { parse_error: true, raw: text }; }
    if (!res.ok || !json.success) {
      console.warn('[annotations] createInstance non-OK', { status: res.status, body: json, raw: text.slice(0, 200) });
      const msg = (json && (json.message || json.error)) ? `${json.message || json.error} (HTTP ${res.status})` : `Create instance failed (HTTP ${res.status})`;
      throw new Error(msg);
    }
    return json.concept || json;
  } catch (e) {
    console.error('[annotations] createInstance error', e);
    throw e;
  }
}

// Create a new TYPE (subtype) under a selected parent type concept
export async function createType(parentConceptId, name) {
  if (!parentConceptId || !name) throw new Error('parentConceptId and name required');
  const payload = { parent_id: parentConceptId, new_concept_name: name, create_as_instance: false };
  const url = '/vontology/api/vontology/create_concept';
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const text = await res.text();
    let json = {};
    try { json = text ? JSON.parse(text) : {}; } catch (_) { json = { parse_error: true, raw: text }; }
    if (!res.ok || !json.success) {
      console.warn('[annotations] createType non-OK', { status: res.status, body: json, raw: text.slice(0, 200) });
      const msg = (json && (json.message || json.error)) ? `${json.message || json.error} (HTTP ${res.status})` : `Create type failed (HTTP ${res.status})`;
      throw new Error(msg);
    }
    return json.concept || json;
  } catch (e) {
    console.error('[annotations] createType error', e);
    throw e;
  }
}
