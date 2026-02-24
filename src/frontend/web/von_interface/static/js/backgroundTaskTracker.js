const ACTIVE_STORAGE_KEY = 'von_background_tasks_active_v1';
const HISTORY_STORAGE_KEY = 'von_background_tasks_history_v1';
const MAX_HISTORY_ITEMS = 60;
const ACTIVE_TASK_STALE_MS = 15 * 60 * 1000;

export const BACKGROUND_TASKS_CHANGED_EVENT = 'von:backgroundTasksChanged';

let taskSequence = 0;

function resolveStorage(kind) {
  try {
    if (kind === 'session') {
      return typeof sessionStorage !== 'undefined' ? sessionStorage : null;
    }
    return typeof localStorage !== 'undefined' ? localStorage : null;
  } catch (_) {
    return null;
  }
}

function asString(value) {
  if (typeof value === 'string') return value.trim();
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return '';
}

function normaliseTaskType(taskType) {
  const raw = asString(taskType) || 'background_task';
  return raw.replace(/\s+/g, '_').toLowerCase();
}

function normaliseLabel(taskType, explicitLabel) {
  const raw = asString(explicitLabel);
  if (raw) return raw;
  return normaliseTaskType(taskType)
    .split('_')
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function readJsonArray(storage, key) {
  if (!storage) return [];
  try {
    const raw = storage.getItem(key);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch (_) {
    return [];
  }
}

function writeJsonArray(storage, key, value) {
  if (!storage) return;
  try {
    storage.setItem(key, JSON.stringify(value));
  } catch (_) {
    // Ignore quota and serialization failures for diagnostics-only data.
  }
}

function safeDurationMs(startedAtMs, finishedAtMs) {
  const startMs = Number(startedAtMs);
  const finishMs = Number(finishedAtMs);
  if (!Number.isFinite(startMs) || !Number.isFinite(finishMs)) return null;
  return Math.max(0, Math.round(finishMs - startMs));
}

function normaliseStatus(rawStatus) {
  const value = asString(rawStatus).toLowerCase();
  if (value === 'error' || value === 'failed' || value === 'failure') return 'error';
  if (value === 'cancelled' || value === 'canceled') return 'cancelled';
  return 'success';
}

function normaliseActiveEntry(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const id = asString(raw.id);
  if (!id) return null;
  const startedAtMs = Number(raw.startedAtMs);
  const safeStartedAtMs = Number.isFinite(startedAtMs) ? startedAtMs : Date.now();
  const taskType = normaliseTaskType(raw.taskType);
  return {
    id,
    taskType,
    label: normaliseLabel(taskType, raw.label),
    detail: asString(raw.detail),
    startedAtMs: safeStartedAtMs,
    startedAtIso: asString(raw.startedAtIso) || new Date(safeStartedAtMs).toISOString(),
  };
}

function normaliseHistoryEntry(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const id = asString(raw.id);
  if (!id) return null;
  const startedAtMs = Number(raw.startedAtMs);
  const finishedAtMs = Number(raw.finishedAtMs);
  const safeStartedAtMs = Number.isFinite(startedAtMs) ? startedAtMs : Date.now();
  const safeFinishedAtMs = Number.isFinite(finishedAtMs) ? finishedAtMs : safeStartedAtMs;
  const taskType = normaliseTaskType(raw.taskType);
  return {
    id,
    taskType,
    label: normaliseLabel(taskType, raw.label),
    detail: asString(raw.detail),
    status: normaliseStatus(raw.status),
    errorMessage: asString(raw.errorMessage),
    startedAtMs: safeStartedAtMs,
    startedAtIso: asString(raw.startedAtIso) || new Date(safeStartedAtMs).toISOString(),
    finishedAtMs: safeFinishedAtMs,
    finishedAtIso: asString(raw.finishedAtIso) || new Date(safeFinishedAtMs).toISOString(),
    durationMs: safeDurationMs(safeStartedAtMs, safeFinishedAtMs),
  };
}

function writeActiveTasks(entries) {
  const storage = resolveStorage('session');
  if (!storage) return;
  const normalised = Array.isArray(entries)
    ? entries.map(normaliseActiveEntry).filter(Boolean)
    : [];
  writeJsonArray(storage, ACTIVE_STORAGE_KEY, normalised);
}

function readActiveTasks() {
  const storage = resolveStorage('session');
  const parsed = readJsonArray(storage, ACTIVE_STORAGE_KEY)
    .map(normaliseActiveEntry)
    .filter(Boolean);
  const cutoff = Date.now() - ACTIVE_TASK_STALE_MS;
  const pruned = parsed.filter((entry) => entry.startedAtMs >= cutoff);
  if (pruned.length !== parsed.length) {
    writeActiveTasks(pruned);
  }
  return pruned;
}

function writeHistory(entries) {
  const storage = resolveStorage('local');
  if (!storage) return;
  const normalised = Array.isArray(entries)
    ? entries.map(normaliseHistoryEntry).filter(Boolean).slice(0, MAX_HISTORY_ITEMS)
    : [];
  writeJsonArray(storage, HISTORY_STORAGE_KEY, normalised);
}

function readHistory() {
  return readJsonArray(resolveStorage('local'), HISTORY_STORAGE_KEY)
    .map(normaliseHistoryEntry)
    .filter(Boolean);
}

function emitBackgroundTaskChange() {
  try {
    const detail = getBackgroundTaskState();
    document.dispatchEvent(new CustomEvent(BACKGROUND_TASKS_CHANGED_EVENT, { detail }));
  } catch (_) {
    // Ignore dispatch errors in constrained test runners.
  }
}

export function getBackgroundTaskState(options = {}) {
  const historyLimitRaw = Number(options?.historyLimit);
  const historyLimit = Number.isFinite(historyLimitRaw) && historyLimitRaw > 0
    ? Math.min(Math.round(historyLimitRaw), MAX_HISTORY_ITEMS)
    : Math.min(20, MAX_HISTORY_ITEMS);
  return {
    active: readActiveTasks(),
    history: readHistory().slice(0, historyLimit),
  };
}

export function clearBackgroundTaskHistory() {
  writeHistory([]);
  emitBackgroundTaskChange();
}

export function startBackgroundTask(taskType, options = {}) {
  const type = normaliseTaskType(taskType);
  taskSequence += 1;
  const startedAtMs = Date.now();
  const entry = normaliseActiveEntry({
    id: `${type}:${startedAtMs}:${taskSequence}`,
    taskType: type,
    label: options?.label,
    detail: options?.detail,
    startedAtMs,
    startedAtIso: new Date(startedAtMs).toISOString(),
  });
  const activeTasks = readActiveTasks();
  activeTasks.push(entry);
  writeActiveTasks(activeTasks);
  emitBackgroundTaskChange();
  return {
    id: entry.id,
    taskType: entry.taskType,
    label: entry.label,
    detail: entry.detail,
    startedAtMs: entry.startedAtMs,
    startedAtIso: entry.startedAtIso,
  };
}

export function finishBackgroundTask(handle, options = {}) {
  const id = asString(typeof handle === 'string' ? handle : handle?.id);
  if (!id) return null;

  const activeTasks = readActiveTasks();
  const taskIndex = activeTasks.findIndex((entry) => entry.id === id);
  const activeEntry = taskIndex >= 0 ? activeTasks[taskIndex] : null;
  if (taskIndex >= 0) {
    activeTasks.splice(taskIndex, 1);
    writeActiveTasks(activeTasks);
  }

  const finishedAtMs = Date.now();
  const fallbackStartMs = Number((typeof handle === 'object' && handle) ? handle.startedAtMs : null);
  const startedAtMs = Number.isFinite(activeEntry?.startedAtMs)
    ? activeEntry.startedAtMs
    : (Number.isFinite(fallbackStartMs) ? fallbackStartMs : finishedAtMs);
  const taskType = normaliseTaskType(activeEntry?.taskType || handle?.taskType || 'background_task');
  const historyEntry = normaliseHistoryEntry({
    id,
    taskType,
    label: options?.label || activeEntry?.label || handle?.label,
    detail: options?.detail || activeEntry?.detail || handle?.detail,
    status: options?.status,
    errorMessage: options?.error ? String(options.error?.message || options.error) : '',
    startedAtMs,
    startedAtIso: activeEntry?.startedAtIso || handle?.startedAtIso || new Date(startedAtMs).toISOString(),
    finishedAtMs,
    finishedAtIso: new Date(finishedAtMs).toISOString(),
  });

  const history = readHistory();
  history.unshift(historyEntry);
  writeHistory(history);
  emitBackgroundTaskChange();
  return historyEntry;
}

export function formatBackgroundTaskSummary(activeTasks, options = {}) {
  const tasks = Array.isArray(activeTasks) ? activeTasks.filter(Boolean) : [];
  if (!tasks.length) return '';
  const maxLabelsRaw = Number(options?.maxLabels);
  const maxLabels = Number.isFinite(maxLabelsRaw) && maxLabelsRaw > 0 ? Math.round(maxLabelsRaw) : 1;
  const labels = tasks.map((task) => normaliseLabel(task.taskType, task.label));
  const shown = labels.slice(0, maxLabels);
  const hiddenCount = Math.max(0, labels.length - shown.length);
  if (hiddenCount > 0) {
    return `BG: ${shown.join(', ')} +${hiddenCount}`;
  }
  return `BG: ${shown.join(', ')}`;
}

export function formatBackgroundTaskTooltip(activeTasks) {
  const tasks = Array.isArray(activeTasks) ? activeTasks.filter(Boolean) : [];
  if (!tasks.length) return '';
  const nowMs = Date.now();
  return tasks
    .map((task) => {
      const label = normaliseLabel(task.taskType, task.label);
      const ageMs = Math.max(0, Math.round(nowMs - Number(task.startedAtMs || nowMs)));
      const detail = asString(task.detail);
      if (detail) {
        return `${label} (${ageMs}ms) - ${detail}`;
      }
      return `${label} (${ageMs}ms)`;
    })
    .join('\n');
}

export function subscribeBackgroundTaskUpdates(listener, options = {}) {
  if (typeof listener !== 'function') {
    return () => { };
  }

  const onTaskChange = (event) => {
    const detail = event?.detail || getBackgroundTaskState();
    listener(detail);
  };
  const onStorage = (event) => {
    if (!event) return;
    if (event.key && event.key !== ACTIVE_STORAGE_KEY && event.key !== HISTORY_STORAGE_KEY) return;
    listener(getBackgroundTaskState());
  };

  try {
    document.addEventListener(BACKGROUND_TASKS_CHANGED_EVENT, onTaskChange);
  } catch (_) { }
  try {
    window.addEventListener('storage', onStorage);
  } catch (_) { }

  if (options?.emitInitial !== false) {
    listener(getBackgroundTaskState());
  }

  return () => {
    try {
      document.removeEventListener(BACKGROUND_TASKS_CHANGED_EVENT, onTaskChange);
    } catch (_) { }
    try {
      window.removeEventListener('storage', onStorage);
    } catch (_) { }
  };
}

export function __testOnly_resetBackgroundTaskTracker() {
  taskSequence = 0;
  try {
    resolveStorage('session')?.removeItem(ACTIVE_STORAGE_KEY);
  } catch (_) { }
  try {
    resolveStorage('local')?.removeItem(HISTORY_STORAGE_KEY);
  } catch (_) { }
  emitBackgroundTaskChange();
}
