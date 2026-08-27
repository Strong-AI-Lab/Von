import { ensureUniqueWindowSessionId, getWindowSessionId, WINDOW_SESSION_HEADER } from '../apiService.js';
import { parseStoredContextValue } from './runtimeIdentityBootstrap.js';

const COORDINATOR_STATE_KEY = '__vonWorkflowCapabilityIndexStatusCoordinator';
const STATUS_UPDATED_EVENT = 'von:workflowCapabilityIndexStatusUpdated';
const DEFAULT_STATUS_REFRESH_TIMEOUT_MS = 8000;

function resolveCoordinatorWindow() {
  if (typeof window === 'undefined') return null;
  try {
    if (window.parent && window.parent !== window && window.parent.document) {
      return window.parent;
    }
  } catch {
    // Cross-origin frames cannot share status. Use the current window instead.
  }
  return window;
}

function readStoredContext(storage, key) {
  try {
    return parseStoredContextValue(storage?.getItem(key));
  } catch {
    return null;
  }
}

function readWindowSessionId() {
  try {
    return String(getWindowSessionId() || '').trim();
  } catch {
    return '';
  }
}

function readCoordinatorNamespace(coordinatorWindow) {
  try {
    const personalSelected = coordinatorWindow?.sessionStorage?.getItem('von_org_selection') === 'personal';
    return String(
      coordinatorWindow?.sessionStorage?.getItem('current_user_namespace')
      || (!personalSelected && coordinatorWindow?.localStorage?.getItem('current_user_namespace'))
      || '',
    ).trim();
  } catch {
    return '';
  }
}

function resolveCoordinatorContext(coordinatorWindow) {
  const windowSessionId = readWindowSessionId();
  const user = (
    readStoredContext(coordinatorWindow?.sessionStorage, 'von_current_user')
    || readStoredContext(coordinatorWindow?.localStorage, 'von_current_user')
  );
  const personalSelected = (() => {
    try {
      return coordinatorWindow?.sessionStorage?.getItem('von_org_selection') === 'personal';
    } catch {
      return false;
    }
  })();
  const organisation = personalSelected
    ? null
    : (
      readStoredContext(coordinatorWindow?.sessionStorage, 'von_current_org')
      || readStoredContext(coordinatorWindow?.localStorage, 'von_current_org')
    );
  const userConceptId = String(user?.concept_id || user?.conceptId || '').trim();
  const organisationConceptId = String(
    organisation?.concept_id || organisation?.conceptId || '',
  ).trim();
  const namespace = readCoordinatorNamespace(coordinatorWindow);
  return {
    key: JSON.stringify([
      windowSessionId,
      userConceptId,
      organisationConceptId,
      namespace,
    ]),
    headers: {
      ...(windowSessionId ? { [WINDOW_SESSION_HEADER]: windowSessionId } : {}),
      ...(userConceptId ? { 'X-User-Concept-ID': userConceptId } : {}),
    },
  };
}

function getCoordinatorState() {
  const coordinatorWindow = resolveCoordinatorWindow();
  if (!coordinatorWindow) {
    return {
      coordinatorWindow: null,
      contextKey: '',
      contextHeaders: {},
      snapshot: null,
      inFlight: null,
      generation: 0,
    };
  }
  const context = resolveCoordinatorContext(coordinatorWindow);
  const existingState = coordinatorWindow[COORDINATOR_STATE_KEY] || null;
  if (
    !existingState
    || existingState.contextKey !== context.key
  ) {
    coordinatorWindow[COORDINATOR_STATE_KEY] = {
      contextKey: context.key,
      generation: Number(existingState?.generation || 0) + 1,
      snapshot: null,
      inFlight: null,
      inFlightForce: false,
    };
  }
  return {
    coordinatorWindow,
    contextHeaders: context.headers,
    ...coordinatorWindow[COORDINATOR_STATE_KEY],
  };
}

function writeCoordinatorState(nextState) {
  const coordinatorWindow = resolveCoordinatorWindow();
  if (!coordinatorWindow) return;
  coordinatorWindow[COORDINATOR_STATE_KEY] = nextState;
}

function parseCheckedAt(value) {
  if (typeof value !== 'string' || !value.trim()) return null;
  const timestamp = Date.parse(value.trim());
  return Number.isFinite(timestamp) ? timestamp : null;
}

function shouldAcceptSnapshot(current, incoming) {
  if (!current) return true;
  const currentCheckedAt = parseCheckedAt(current.checked_at_utc);
  const incomingCheckedAt = parseCheckedAt(incoming.checked_at_utc);
  if (currentCheckedAt !== null && incomingCheckedAt === null) return false;
  if (currentCheckedAt === null || incomingCheckedAt === null) return true;
  return incomingCheckedAt >= currentCheckedAt;
}

export function getLatestWorkflowCapabilityIndexStatus() {
  return getCoordinatorState().snapshot || null;
}

export function acceptWorkflowCapabilityIndexStatus(
  report,
  { contextKey = null, generation = null } = {},
) {
  if (!report || typeof report !== 'object') {
    return getLatestWorkflowCapabilityIndexStatus();
  }

  const state = getCoordinatorState();
  if (
    (contextKey && state.contextKey !== contextKey)
    || (generation !== null && state.generation !== generation)
  ) {
    return state.snapshot || null;
  }
  if (!shouldAcceptSnapshot(state.snapshot, report)) {
    return state.snapshot;
  }

  const snapshot = { ...report };
  writeCoordinatorState({
    contextKey: state.contextKey,
    generation: state.generation,
    snapshot,
    inFlight: state.inFlight || null,
    inFlightForce: state.inFlightForce === true,
  });

  try {
    const EventConstructor = state.coordinatorWindow?.CustomEvent || globalThis.CustomEvent;
    state.coordinatorWindow?.document?.dispatchEvent(new EventConstructor(
      STATUS_UPDATED_EVENT,
      { detail: snapshot },
    ));
  } catch {
    // Rendering can still read the stored snapshot if event dispatch is unavailable.
  }
  return snapshot;
}

export async function refreshWorkflowCapabilityIndexStatus({
  force = false,
  fetchImpl = globalThis.fetch,
  timeoutMs = DEFAULT_STATUS_REFRESH_TIMEOUT_MS,
} = {}) {
  await ensureUniqueWindowSessionId?.();
  const state = getCoordinatorState();
  const requestContextKey = state.contextKey;
  const requestGeneration = state.generation;
  if (state.inFlight) {
    if (!force || state.inFlightForce === true) return state.inFlight;
    return state.inFlight.then(() => refreshWorkflowCapabilityIndexStatus({
      force: true,
      fetchImpl,
      timeoutMs,
    }));
  }
  if (typeof fetchImpl !== 'function') return state.snapshot || null;

  const url = force
    ? '/api/workflows/capability-index/status?nocache=1'
    : '/api/workflows/capability-index/status';
  const request = (async () => {
    let timeoutId = null;
    const controller = typeof AbortController === 'function'
      ? new AbortController()
      : null;
    try {
      const fetchPromise = fetchImpl(url, {
        cache: 'no-store',
        headers: { ...state.contextHeaders },
        ...(controller ? { signal: controller.signal } : {}),
      });
      const safeTimeoutMs = Number(timeoutMs);
      const response = Number.isFinite(safeTimeoutMs) && safeTimeoutMs > 0
        ? await Promise.race([
          fetchPromise,
          new Promise((resolve) => {
            timeoutId = setTimeout(() => {
              try {
                controller?.abort();
              } catch {
                // The timeout still releases shared coordination if abort fails.
              }
              resolve(null);
            }, safeTimeoutMs);
          }),
        ])
        : await fetchPromise;
      if (!response?.ok) return getLatestWorkflowCapabilityIndexStatus();
      const payload = await response.json();
      return acceptWorkflowCapabilityIndexStatus(payload, {
        contextKey: requestContextKey,
        generation: requestGeneration,
      });
    } catch (error) {
      console.warn('Failed to refresh workflow capability index status', error);
      return getLatestWorkflowCapabilityIndexStatus();
    } finally {
      if (timeoutId) clearTimeout(timeoutId);
      const latest = getCoordinatorState();
      if (
        latest.contextKey === requestContextKey
        && latest.generation === requestGeneration
      ) {
        writeCoordinatorState({
          contextKey: requestContextKey,
          generation: requestGeneration,
          snapshot: latest.snapshot || null,
          inFlight: null,
          inFlightForce: false,
        });
      }
    }
  })();

  writeCoordinatorState({
    contextKey: requestContextKey,
    generation: requestGeneration,
    snapshot: state.snapshot || null,
    inFlight: request,
    inFlightForce: force,
  });
  return request;
}

export function subscribeToWorkflowCapabilityIndexStatus(
  callback,
  { emitCurrent = true } = {},
) {
  const state = getCoordinatorState();
  if (typeof callback !== 'function' || !state.coordinatorWindow?.document) {
    return () => {};
  }
  const handler = (event) => callback(event?.detail || null);
  state.coordinatorWindow.document.addEventListener(STATUS_UPDATED_EVENT, handler);
  if (emitCurrent && state.snapshot) callback(state.snapshot);
  return () => {
    state.coordinatorWindow?.document?.removeEventListener(STATUS_UPDATED_EVENT, handler);
  };
}

export function resetWorkflowCapabilityIndexStatusCoordinator() {
  const coordinatorWindow = resolveCoordinatorWindow();
  if (!coordinatorWindow) return;
  const context = resolveCoordinatorContext(coordinatorWindow);
  const existingState = coordinatorWindow[COORDINATOR_STATE_KEY] || null;
  writeCoordinatorState({
    contextKey: context.key,
    generation: Number(existingState?.generation || 0) + 1,
    snapshot: null,
    inFlight: null,
    inFlightForce: false,
  });
}

export const __testOnly_resetWorkflowCapabilityIndexStatusCoordinator =
  resetWorkflowCapabilityIndexStatusCoordinator;
