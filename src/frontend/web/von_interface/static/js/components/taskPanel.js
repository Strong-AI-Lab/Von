/**
 * Task Panel Component (JVNAUTOSCI-1040)
 *
 * Provides UI for viewing and managing tasks within conversations.
 * Tasks are stored as Vontology concepts and accessed via REST API.
 */

import { deleteJson, getJson, patchJson, postJson, getUserContext } from '../apiService.js';
import { activateTab } from '../tabNavigation.js';
import { showToast } from '../utils/toast.js';
import { selectBestNameForContext } from '../utils/nameSelection.js';

// Task panel state
let _panelEl = null;
let _taskListEl = null;
let _globalTasksContainer = null;  // Container for global tasks tab
let _tasks = [];
let _currentSessionId = null;
let _isVisible = false;
let _isLoading = false;
let _filterStatus = 'all';
let _filterPriority = 'all';
let _queryFilter = '';
let _viewMode = 'board';
let _globalTaskScope = 'accessible';
let _filterTaskTypeId = 'all';
let _filterTaskSourceId = 'all';
let _isGlobalTabMode = false;  // True when rendering into global tasks tab
let _taskDetailState = {};  // taskId -> detail panel state
let _selectedTaskId = '';
let _bulkTaskVisibility = 'exclude';
let _bulkTaskCollectionId = '';
let _hiddenBulkTaskTotal = 0;
let _hiddenBulkTaskCollections = [];
let _taskTaxonomy = {
    task_types: [],
    task_sources: [],
    defaults: {},
};
let _taskGroupStorageKey = null;
let _selectedTaskGroupIds = new Set();  // Selected ontology-backed task groups.
let _taskGroupOptions = [];
const _taskGroupDisplayNameCache = new Map();
const _taskGroupNameFetchInFlight = new Set();

const TASK_GROUP_STORAGE_KEY_PREFIX = 'von_task_group_filter_v1';
const TASK_LIST_LIMIT = '500';

// Constants
const TASK_STATUS_OPTIONS = [
    { value: 'pending', label: 'Pending', icon: '⏳' },
    { value: 'in_progress', label: 'In Progress', icon: '🔄' },
    { value: 'completed', label: 'Completed', icon: '✅' },
    { value: 'cancelled', label: 'Cancelled', icon: '❌' },
    { value: 'blocked', label: 'Blocked', icon: '🚫' },
];

const PRIORITY_OPTIONS = [
    { value: 'low', label: 'Low', icon: '🟢' },
    { value: 'medium', label: 'Medium', icon: '🟡' },
    { value: 'high', label: 'High', icon: '🟠' },
    { value: 'critical', label: 'Critical', icon: '🔴' },
];

const TASK_SCOPE_OPTIONS = [
    { value: 'accessible', label: 'Accessible tasks' },
    { value: 'assigned_to_me', label: 'Assigned to me' },
    { value: 'created_by_me', label: 'Created by me' },
];

const TASK_LINK_OPTIONS = [
    { value: 'depends_on', label: 'Depends on' },
    { value: 'required_by', label: 'Required by' },
    { value: 'blocks', label: 'Blocks' },
    { value: 'blocked_by', label: 'Blocked by' },
    { value: 'relates_to', label: 'Relates to' },
];

/**
 * Get status display info.
 */
function getStatusInfo(status) {
    return TASK_STATUS_OPTIONS.find(s => s.value === status) || { value: status, label: status, icon: '❓' };
}

/**
 * Get priority display info.
 */
function getPriorityInfo(priority) {
    return PRIORITY_OPTIONS.find(p => p.value === priority) || { value: priority, label: priority, icon: '⚪' };
}

function getCurrentUserConceptId() {
    try {
        const ctx = getUserContext ? getUserContext() : {};
        return normaliseTaskConceptId(ctx?.user_id || '');
    } catch (_) {
        return '';
    }
}

function getTaskTypeOptions() {
    return Array.isArray(_taskTaxonomy?.task_types) ? _taskTaxonomy.task_types : [];
}

function getTaskSourceOptions() {
    return Array.isArray(_taskTaxonomy?.task_sources) ? _taskTaxonomy.task_sources : [];
}

function getTaskPrimaryType(task) {
    if (task?.primary_task_type_id) {
        return {
            concept_id: task.primary_task_type_id,
            label: task.primary_task_type_label || deriveConceptNameFromId(task.primary_task_type_id),
        };
    }
    const types = Array.isArray(task?.task_types) ? task.task_types : [];
    if (types.length > 0) {
        return types[0];
    }
    return null;
}

function getTaskSourceSummary(task) {
    if (!task || typeof task !== 'object') return null;
    const conceptId = task.task_source_id;
    if (!conceptId) return null;
    return {
        concept_id: conceptId,
        label: task.task_source_label || deriveConceptNameFromId(conceptId),
        slug: task.task_source_slug || '',
    };
}

function getHiddenBulkTaskCollections(task) {
    const collections = Array.isArray(task?.hidden_by_default_bulk_task_collections)
        ? task.hidden_by_default_bulk_task_collections
        : [];
    return collections.filter((collection) => (
        collection
        && typeof collection === 'object'
        && collection.hidden_by_default !== false
        && typeof collection.collection_id === 'string'
        && collection.collection_id.trim()
    ));
}

function normaliseBulkTaskCollectionSummary(collection) {
    if (!collection || typeof collection !== 'object') return null;
    const collectionId = typeof collection.collection_id === 'string'
        ? collection.collection_id.trim()
        : '';
    if (!collectionId) return null;
    const label = typeof collection.label === 'string' && collection.label.trim()
        ? collection.label.trim()
        : deriveConceptNameFromId(collectionId);
    const count = Number.isFinite(Number(collection.count))
        ? Math.max(0, Number(collection.count))
        : 0;
    return {
        collection_id: collectionId,
        label,
        kind: typeof collection.kind === 'string' ? collection.kind : '',
        count,
    };
}

function setBulkTaskVisibilitySummary(response) {
    _hiddenBulkTaskTotal = Number.isFinite(Number(response?.hidden_bulk_task_total))
        ? Math.max(0, Number(response.hidden_bulk_task_total))
        : 0;
    _hiddenBulkTaskCollections = Array.isArray(response?.hidden_bulk_task_collections)
        ? response.hidden_bulk_task_collections
            .map(normaliseBulkTaskCollectionSummary)
            .filter(Boolean)
        : [];
}

function buildGlobalTasksUrl() {
    const params = new URLSearchParams();
    params.set('limit', TASK_LIST_LIMIT);
    params.set('bulk_visibility', _bulkTaskVisibility || 'exclude');
    if (_bulkTaskVisibility === 'only' && _bulkTaskCollectionId) {
        params.set('bulk_collection_id', _bulkTaskCollectionId);
    }
    const currentUserId = getCurrentUserConceptId();
    if (_globalTaskScope === 'assigned_to_me' && currentUserId) {
        params.set('assignee_concept_id', currentUserId);
    } else if (_globalTaskScope === 'created_by_me' && currentUserId) {
        params.set('created_by_concept_id', currentUserId);
    }
    return `/api/tasks/?${params.toString()}`;
}

function buildMyTasksUrl(statusFilter = null) {
    const params = new URLSearchParams();
    params.set('include_created', 'true');
    params.set('limit', TASK_LIST_LIMIT);
    params.set('bulk_visibility', _bulkTaskVisibility || 'exclude');
    if (_bulkTaskVisibility === 'only' && _bulkTaskCollectionId) {
        params.set('bulk_collection_id', _bulkTaskCollectionId);
    }
    if (statusFilter && statusFilter !== 'all') {
        params.set('status', statusFilter);
    }
    return `/api/tasks/my?${params.toString()}`;
}

async function ensureTaskTaxonomyLoaded() {
    try {
        const taxonomy = await getJson('/api/tasks/taxonomy');
        if (taxonomy && typeof taxonomy === 'object') {
            _taskTaxonomy = {
                task_types: Array.isArray(taxonomy.task_types) ? taxonomy.task_types : [],
                task_sources: Array.isArray(taxonomy.task_sources) ? taxonomy.task_sources : [],
                defaults: taxonomy.defaults || {},
            };
        }
    } catch (err) {
        console.debug('[taskPanel] Failed to load task taxonomy:', err);
    }
}

function parseCsvList(rawValue) {
    if (typeof rawValue !== 'string') return [];
    const seen = new Set();
    return rawValue
        .split(',')
        .map((part) => part.trim())
        .filter((part) => {
            if (!part) return false;
            const lowered = part.toLowerCase();
            if (seen.has(lowered)) return false;
            seen.add(lowered);
            return true;
        });
}

function isoToLocalInputValue(isoValue) {
    if (!isoValue || typeof isoValue !== 'string') return '';
    const parsed = new Date(isoValue);
    if (Number.isNaN(parsed.getTime())) return '';
    const local = new Date(parsed.getTime() - (parsed.getTimezoneOffset() * 60000));
    return local.toISOString().slice(0, 16);
}

function localInputValueToIso(rawValue) {
    if (!rawValue || typeof rawValue !== 'string' || !rawValue.trim()) return null;
    const parsed = new Date(rawValue);
    if (Number.isNaN(parsed.getTime())) return null;
    return parsed.toISOString();
}

function formatDateTimeOrDash(value) {
    if (!value || typeof value !== 'string') return '—';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '—';
    return parsed.toLocaleString();
}

function getTaskDetailState(taskId) {
    if (!_taskDetailState[taskId]) {
        _taskDetailState[taskId] = {
            expanded: false,
            loading: false,
            error: null,
            task: null,
            comments: [],
            attachments: [],
            history: [],
        };
    }
    return _taskDetailState[taskId];
}

function pruneTaskDetailState() {
    const activeIds = new Set(_tasks.map((task) => getTaskId(task)));
    Object.keys(_taskDetailState).forEach((taskId) => {
        if (!activeIds.has(taskId)) {
            delete _taskDetailState[taskId];
        }
    });
}

/**
 * Initialize the task panel.
 * Call this once on page load to set up the panel element.
 */
export function initializeTaskPanel() {
    _panelEl = document.getElementById('taskPanel');
    _taskListEl = document.getElementById('taskList');

    if (!_panelEl || !_taskListEl) {
        console.warn('[taskPanel] Panel elements not found in DOM');
        return;
    }
    loadTaskGroupSelectionFromStorage();
    renderTaskGroupFilterControls();

    // Set up close button
    const closeBtn = document.getElementById('closeTaskPanel');
    if (closeBtn) {
        closeBtn.addEventListener('click', hideTaskPanel);
    }

    // Set up create task form
    const createBtn = document.getElementById('createTaskBtn');
    if (createBtn) {
        createBtn.addEventListener('click', handleCreateTask);
    }

    // Set up filter dropdown
    const filterSelect = document.getElementById('taskStatusFilter');
    if (filterSelect) {
        filterSelect.addEventListener('change', (e) => {
            _filterStatus = e.target.value;
            renderTaskList();
        });
    }

    const priorityFilter = document.getElementById('taskPriorityFilter');
    if (priorityFilter) {
        priorityFilter.addEventListener('change', (e) => {
            _filterPriority = e.target.value;
            renderTaskList();
        });
    }

    const queryFilter = document.getElementById('taskQueryFilter');
    if (queryFilter) {
        queryFilter.addEventListener('input', (e) => {
            _queryFilter = (e.target.value || '').trim().toLowerCase();
            renderTaskList();
        });
    }

    const viewModeSelect = document.getElementById('taskViewMode');
    if (viewModeSelect) {
        viewModeSelect.addEventListener('change', (e) => {
            _viewMode = e.target.value === 'list' ? 'list' : 'board';
            renderTaskList();
        });
    }

    // Set up refresh button
    const refreshBtn = document.getElementById('refreshTasksBtn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => loadTasks(_currentSessionId));
    }

    console.log('[taskPanel] Initialized');
}

/**
 * Show the task panel.
 */
export function showTaskPanel(options = {}) {
    applyPanelOpenOptions(options);
    if (_panelEl) {
        loadTaskGroupSelectionFromStorage();
        _isGlobalTabMode = false;
        _taskListEl = document.getElementById('taskList') || _taskListEl;
        _panelEl.classList.remove('hidden');
        _panelEl.setAttribute('aria-hidden', 'false');
        _isVisible = true;
        // Auto-refresh tasks when panel becomes visible
        refreshTasks();
    }
}

function applyPanelOpenOptions(options = {}) {
    if (!options || typeof options !== 'object') return;
    if (Object.prototype.hasOwnProperty.call(options, 'sessionId')) {
        _currentSessionId = options.sessionId || null;
    }
}

/**
 * Hide the task panel.
 */
export function hideTaskPanel() {
    if (_panelEl) {
        _panelEl.classList.add('hidden');
        _panelEl.setAttribute('aria-hidden', 'true');
        _isVisible = false;
    }
}

/**
 * Toggle task panel visibility.
 */
export function toggleTaskPanel(options = {}) {
    if (_isVisible) {
        hideTaskPanel();
    } else {
        showTaskPanel(options);
    }
}

/**
 * Show global tasks view in the dedicated tab (all user's tasks, not filtered by conversation).
 * Renders into the globalTasksContainer instead of the overlay panel.
 */
export async function showGlobalTasks() {
    loadTaskGroupSelectionFromStorage();
    _currentSessionId = null;  // Clear session filter
    _isGlobalTabMode = true;

    // Initialize the global tasks container if needed
    if (!_globalTasksContainer) {
        _globalTasksContainer = document.getElementById('globalTasksContainer');
    }

    if (!_globalTasksContainer) {
        console.warn('[taskPanel] Global tasks container not found');
        return;
    }

    _globalTasksContainer.innerHTML = '<div class="loading">Loading tasks…</div>';
    await ensureTaskTaxonomyLoaded();
    renderGlobalTasksTabContent();
    await loadGlobalTasks();
}

/**
 * Render the global tasks tab content structure.
 */
function renderGlobalTasksTabContent() {
    if (!_globalTasksContainer) return;

    const taskTypeOptions = getTaskTypeOptions();
    const taskSourceOptions = getTaskSourceOptions();

    _globalTasksContainer.innerHTML = `
        <div class="global-tasks-shell">
            <div class="global-tasks-header">
                <div class="global-tasks-header-copy">
                    <span class="global-tasks-eyebrow">Task Workspace</span>
                    <h2>All Tasks</h2>
                    <p class="global-tasks-subtitle">Board lanes, Vontology-backed task categories, and a richer detail view live together here.</p>
                </div>
                <div class="global-tasks-controls">
                    <select id="globalTaskScopeFilter" class="task-filter-select" title="Task scope">
                        ${TASK_SCOPE_OPTIONS.map((option) => `
                            <option value="${option.value}" ${_globalTaskScope === option.value ? 'selected' : ''}>
                                ${option.label}
                            </option>
                        `).join('')}
                    </select>
                    <select id="globalTaskStatusFilter" class="task-filter-select" title="Filter by status">
                    <option value="all">All tasks</option>
                    <option value="pending">Pending</option>
                    <option value="in_progress">In Progress</option>
                    <option value="completed">Completed</option>
                    <option value="cancelled">Cancelled</option>
                    <option value="blocked">Blocked</option>
                </select>
                <select id="globalTaskPriorityFilter" class="task-filter-select" title="Filter by priority">
                    <option value="all">All priorities</option>
                    <option value="low">Low</option>
                    <option value="medium">Medium</option>
                    <option value="high">High</option>
                    <option value="critical">Critical</option>
                </select>
                    <select id="globalTaskTypeFilter" class="task-filter-select" title="Filter by task type">
                        <option value="all">All task types</option>
                        ${taskTypeOptions.map((option) => `
                            <option value="${escapeHtml(option.concept_id)}" ${_filterTaskTypeId === option.concept_id ? 'selected' : ''}>
                                ${escapeHtml(option.label)}
                            </option>
                        `).join('')}
                    </select>
                    <select id="globalTaskSourceFilter" class="task-filter-select" title="Filter by task source">
                        <option value="all">All task sources</option>
                        ${taskSourceOptions.map((option) => `
                            <option value="${escapeHtml(option.concept_id)}" ${_filterTaskSourceId === option.concept_id ? 'selected' : ''}>
                                ${escapeHtml(option.label)}
                            </option>
                        `).join('')}
                    </select>
                <select id="globalTaskViewMode" class="task-filter-select" title="Task view mode">
                    <option value="board" selected>Board view</option>
                    <option value="list">List view</option>
                </select>
                <input id="globalTaskQueryFilter" class="task-filter-query" type="search" placeholder="Search tasks..." aria-label="Search tasks" />
                <button id="refreshGlobalTasksBtn" class="task-refresh-btn" title="Refresh tasks">🔄</button>
                </div>
            </div>
            <div id="globalTaskSummary" class="global-task-summary" aria-live="polite"></div>
            <div id="globalBulkTaskVisibilityControl" class="bulk-task-visibility-control hidden" aria-live="polite"></div>
            <div class="global-tasks-create">
                <input type="text" id="globalNewTaskTitle" class="task-input" placeholder="Task title...">
                <textarea id="globalNewTaskDescription" class="task-textarea" placeholder="Task description..." rows="2"></textarea>
                <div class="global-tasks-create-actions">
                    <select id="globalNewTaskType" class="task-priority-select">
                        ${taskTypeOptions.map((option, index) => `
                            <option value="${escapeHtml(option.concept_id)}" ${index === 0 ? 'selected' : ''}>
                                ${escapeHtml(option.label)}
                            </option>
                        `).join('')}
                    </select>
                    <select id="globalNewTaskPriority" class="task-priority-select">
                        <option value="low">🟢 Low</option>
                        <option value="medium" selected>🟡 Medium</option>
                        <option value="high">🟠 High</option>
                        <option value="critical">🔴 Critical</option>
                    </select>
                    <input type="datetime-local" id="globalNewTaskDueDate" class="task-priority-select" title="Optional due date" />
                    <button id="globalCreateTaskBtn" class="task-create-btn">Create Task</button>
                </div>
            </div>
            <div id="globalTaskGroupFilterRow" class="task-group-filter-row hidden" aria-label="Task groups"></div>
            <div class="global-task-layout">
                <div class="global-task-main">
                    <div id="globalTaskList" class="task-list"></div>
                </div>
                <aside id="globalTaskInspector" class="task-inspector" aria-live="polite"></aside>
            </div>
        </div>
    `;

    // Attach event listeners for the global tasks tab
    const scopeFilter = _globalTasksContainer.querySelector('#globalTaskScopeFilter');
    if (scopeFilter) {
        scopeFilter.value = _globalTaskScope;
        scopeFilter.addEventListener('change', async (e) => {
            _globalTaskScope = e.target.value || 'accessible';
            await loadGlobalTasks();
        });
    }

    const filterSelect = _globalTasksContainer.querySelector('#globalTaskStatusFilter');
    if (filterSelect) {
        filterSelect.value = _filterStatus;
        filterSelect.addEventListener('change', (e) => {
            _filterStatus = e.target.value;
            renderTaskList();
        });
    }

    const priorityFilter = _globalTasksContainer.querySelector('#globalTaskPriorityFilter');
    if (priorityFilter) {
        priorityFilter.value = _filterPriority;
        priorityFilter.addEventListener('change', (e) => {
            _filterPriority = e.target.value;
            renderTaskList();
        });
    }

    const typeFilter = _globalTasksContainer.querySelector('#globalTaskTypeFilter');
    if (typeFilter) {
        typeFilter.value = _filterTaskTypeId;
        typeFilter.addEventListener('change', (e) => {
            _filterTaskTypeId = e.target.value || 'all';
            renderTaskList();
        });
    }

    const sourceFilter = _globalTasksContainer.querySelector('#globalTaskSourceFilter');
    if (sourceFilter) {
        sourceFilter.value = _filterTaskSourceId;
        sourceFilter.addEventListener('change', (e) => {
            _filterTaskSourceId = e.target.value || 'all';
            renderTaskList();
        });
    }

    const queryFilter = _globalTasksContainer.querySelector('#globalTaskQueryFilter');
    if (queryFilter) {
        queryFilter.value = _queryFilter;
        queryFilter.addEventListener('input', (e) => {
            _queryFilter = (e.target.value || '').trim().toLowerCase();
            renderTaskList();
        });
    }

    const viewModeSelect = _globalTasksContainer.querySelector('#globalTaskViewMode');
    if (viewModeSelect) {
        viewModeSelect.value = _viewMode;
        viewModeSelect.addEventListener('change', (e) => {
            _viewMode = e.target.value === 'list' ? 'list' : 'board';
            renderTaskList();
        });
    }

    const refreshBtn = _globalTasksContainer.querySelector('#refreshGlobalTasksBtn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => loadGlobalTasks());
    }

    const createBtn = _globalTasksContainer.querySelector('#globalCreateTaskBtn');
    if (createBtn) {
        createBtn.addEventListener('click', handleGlobalCreateTask);
    }

    // Update the task list element reference for global mode
    _taskListEl = _globalTasksContainer.querySelector('#globalTaskList');
    renderTaskGroupFilterControls();
    renderGlobalTaskSummary();
    renderBulkTaskVisibilityControls();
    renderGlobalTaskInspector();
}

/**
 * Handle task creation from the global tasks tab.
 */
async function handleGlobalCreateTask() {
    const titleInput = _globalTasksContainer?.querySelector('#globalNewTaskTitle');
    const descInput = _globalTasksContainer?.querySelector('#globalNewTaskDescription');
    const prioritySelect = _globalTasksContainer?.querySelector('#globalNewTaskPriority');
    const typeSelect = _globalTasksContainer?.querySelector('#globalNewTaskType');
    const dueInput = _globalTasksContainer?.querySelector('#globalNewTaskDueDate');

    if (!titleInput || !descInput) return;

    const title = titleInput.value.trim();
    const description = descInput.value.trim();

    if (!title) {
        showToast('Task title is required', 'warning');
        titleInput.focus();
        return;
    }

    try {
        const payload = {
            title,
            description,
            priority: prioritySelect?.value || 'medium',
            task_type_ids: typeSelect?.value ? [typeSelect.value] : [],
            due_date: localInputValueToIso(dueInput?.value || ''),
        };

        await postJson('/api/tasks/', payload);
        showToast('Task created', 'success');

        // Clear the form
        titleInput.value = '';
        descInput.value = '';
        if (dueInput) dueInput.value = '';

        // Reload tasks
        await loadGlobalTasks();
    } catch (err) {
        console.error('[taskPanel] Failed to create task:', err);
        showToast('Failed to create task', 'error');
    }
}

/**
 * Check if panel is visible.
 */
export function isTaskPanelVisible() {
    return _isVisible;
}

/**
 * Set the current conversation session and load its tasks.
 */
export async function setCurrentSession(sessionId) {
    _currentSessionId = sessionId;
    if (_isVisible && sessionId) {
        await loadTasks(sessionId);
    }
}

/**
 * Get the current task count for badge display.
 */
export function getTaskCount() {
    return _tasks.filter(t => t.status !== 'completed' && t.status !== 'cancelled').length;
}

/**
 * Load tasks from the API.
 */
export async function loadTasks(sessionId = null) {
    if (_isLoading) return;

    loadTaskGroupSelectionFromStorage();
    _isLoading = true;
    updateLoadingState(true);

    try {
        let url;
        const params = new URLSearchParams();

        if (sessionId) {
            // Load tasks for a specific conversation session
            url = '/api/tasks/';
            params.set('session_id', sessionId);
            params.set('limit', TASK_LIST_LIMIT);
            params.set('bulk_visibility', _bulkTaskVisibility || 'exclude');
            if (_bulkTaskVisibility === 'only' && _bulkTaskCollectionId) {
                params.set('bulk_collection_id', _bulkTaskCollectionId);
            }
        } else {
            // Load current user's tasks via /my endpoint
            url = buildMyTasksUrl();
        }

        if (params.toString() && sessionId) {
            url += '?' + params.toString();
        }

        const response = await getJson(url);
        _tasks = response.tasks || [];
        setBulkTaskVisibilitySummary(response);
        pruneTaskDetailState();
        refreshTaskGroupOptions();
        renderTaskList();
        updateTaskCountBadge();

    } catch (err) {
        console.error('[taskPanel] Failed to load tasks:', err);
        showToast('Failed to load tasks', 'error');
    } finally {
        _isLoading = false;
        updateLoadingState(false);
    }
}

/**
 * Load tasks for the current user (my tasks).
 */
export async function loadMyTasks(statusFilter = null) {
    if (_isLoading) return;

    loadTaskGroupSelectionFromStorage();
    _isLoading = true;
    updateLoadingState(true);

    try {
        const url = buildMyTasksUrl(statusFilter);
        const response = await getJson(url);
        _tasks = response.tasks || [];
        setBulkTaskVisibilitySummary(response);
        pruneTaskDetailState();
        refreshTaskGroupOptions();
        renderTaskList();
        updateTaskCountBadge();

    } catch (err) {
        console.error('[taskPanel] Failed to load my tasks:', err);
        showToast('Failed to load tasks', 'error');
    } finally {
        _isLoading = false;
        updateLoadingState(false);
    }
}

async function loadGlobalTasks() {
    if (_isLoading) return;

    loadTaskGroupSelectionFromStorage();
    _isLoading = true;
    updateLoadingState(true);

    try {
        const response = await getJson(buildGlobalTasksUrl());
        _tasks = response.tasks || [];
        setBulkTaskVisibilitySummary(response);
        pruneTaskDetailState();
        refreshTaskGroupOptions();
        ensureSelectedTaskStillValid();
        renderTaskList();
        updateTaskCountBadge();
    } catch (err) {
        console.error('[taskPanel] Failed to load global tasks:', err);
        showToast('Failed to load tasks', 'error');
    } finally {
        _isLoading = false;
        updateLoadingState(false);
    }
}

function taskMatchesGlobalScope(task) {
    if (!_isGlobalTabMode || _globalTaskScope === 'accessible') return true;
    const currentUserId = getCurrentUserConceptId();
    if (!currentUserId) return true;
    if (_globalTaskScope === 'assigned_to_me') {
        return task?.assignee_concept_id === currentUserId;
    }
    if (_globalTaskScope === 'created_by_me') {
        return task?.created_by_concept_id === currentUserId;
    }
    return true;
}

function buildTaskSearchText(task) {
    const taskTypes = Array.isArray(task?.task_types)
        ? task.task_types.map((item) => item?.label).filter(Boolean)
        : [];
    const sourceSummary = getTaskSourceSummary(task);
    return [
        task?.title,
        task?.description,
        task?.reference_code,
        task?.task_role,
        task?.next_checkpoint,
        task?.progress_signal,
        task?.evidence,
        task?.notes,
        sourceSummary?.label,
        ...taskTypes,
        ...(Array.isArray(task?.labels) ? task.labels : []),
        ...(Array.isArray(task?.components) ? task.components : []),
        ...(Array.isArray(task?.fix_versions) ? task.fix_versions : []),
        ...(Array.isArray(task?.sprint_values) ? task.sprint_values : []),
        task?.parent_task_concept_id,
        task?.epic_task_concept_id,
        task?.backlog_rank,
    ]
        .filter((value) => typeof value === 'string' && value.trim())
        .join(' ')
        .toLowerCase();
}

function getFilteredTasks() {
    return _tasks.filter((task) => {
        if (!taskMatchesGlobalScope(task)) {
            return false;
        }
        if (_filterStatus !== 'all' && task.status !== _filterStatus) {
            return false;
        }
        if (_filterPriority !== 'all' && task.priority !== _filterPriority) {
            return false;
        }
        if (_filterTaskTypeId !== 'all') {
            const typeIds = Array.isArray(task.task_type_ids) ? task.task_type_ids : [];
            if (!typeIds.includes(_filterTaskTypeId)) {
                return false;
            }
        }
        if (_filterTaskSourceId !== 'all' && task.task_source_id !== _filterTaskSourceId) {
            return false;
        }
        if (_queryFilter && !buildTaskSearchText(task).includes(_queryFilter)) {
            return false;
        }
        if (_selectedTaskGroupIds.size > 0) {
            const taskGroupId = deriveTaskGroupConceptId(task);
            if (!taskGroupId || !_selectedTaskGroupIds.has(taskGroupId)) {
                return false;
            }
        }
        return true;
    });
}

function ensureSelectedTaskStillValid(filteredTasks = null) {
    const visibleTasks = Array.isArray(filteredTasks) ? filteredTasks : getFilteredTasks();
    if (visibleTasks.length === 0) {
        _selectedTaskId = '';
        return;
    }
    const selectedStillVisible = visibleTasks.some((task) => getTaskId(task) === _selectedTaskId);
    if (!selectedStillVisible) {
        _selectedTaskId = getTaskId(visibleTasks[0]);
    }
}

function renderGlobalTaskSummary(filteredTasks = getFilteredTasks()) {
    const summaryEl = _globalTasksContainer?.querySelector('#globalTaskSummary');
    if (!summaryEl) return;

    const activeCount = filteredTasks.filter((task) => !['completed', 'cancelled'].includes(task.status)).length;
    const sourcedCount = filteredTasks.filter((task) => task.task_source_id).length;
    const typedCount = filteredTasks.filter((task) => Array.isArray(task.task_type_ids) && task.task_type_ids.length > 0).length;
    const selectedTask = filteredTasks.find((task) => getTaskId(task) === _selectedTaskId) || null;

    summaryEl.innerHTML = `
        <span class="task-summary-chip"><strong>${filteredTasks.length}</strong> visible</span>
        <span class="task-summary-chip"><strong>${activeCount}</strong> active</span>
        <span class="task-summary-chip"><strong>${typedCount}</strong> typed</span>
        <span class="task-summary-chip"><strong>${sourcedCount}</strong> sourced</span>
        ${_hiddenBulkTaskTotal > 0 ? `<span class="task-summary-chip task-summary-chip-bulk"><strong>${_hiddenBulkTaskTotal}</strong> bulk hidden</span>` : ''}
        <span class="task-summary-chip task-summary-chip-soft">${escapeHtml(selectedTask?.title || 'No task selected')}</span>
    `;
}

function getBulkTaskVisibilityControlElements() {
    return [
        document.getElementById('taskBulkTaskVisibilityControl'),
        _globalTasksContainer?.querySelector('#globalBulkTaskVisibilityControl'),
    ].filter(Boolean);
}

function renderBulkTaskVisibilityControls() {
    const controlElements = getBulkTaskVisibilityControlElements();
    if (controlElements.length === 0) return;

    if (_hiddenBulkTaskTotal <= 0 && _hiddenBulkTaskCollections.length === 0) {
        controlElements.forEach((controlEl) => {
            controlEl.classList.add('hidden');
            controlEl.innerHTML = '';
        });
        return;
    }

    const selectedCollection = _hiddenBulkTaskCollections.find(
        (collection) => collection.collection_id === _bulkTaskCollectionId,
    );
    const modeLabel = _bulkTaskVisibility === 'only'
        ? `Showing ${selectedCollection?.label || 'bulk collection'} only`
        : (_bulkTaskVisibility === 'include' ? 'Bulk collections shown' : 'Bulk collections hidden');
    const collectionButtons = _hiddenBulkTaskCollections.map((collection) => `
        <button type="button"
            class="bulk-task-visibility-btn ${_bulkTaskVisibility === 'only' && _bulkTaskCollectionId === collection.collection_id ? 'active' : ''}"
            data-bulk-action="only"
            data-bulk-collection-id="${escapeHtml(collection.collection_id)}"
            title="Show only ${escapeHtml(collection.label)}">
            ${escapeHtml(collection.label)}
            <span class="task-group-filter-count">${collection.count}</span>
        </button>
    `).join('');
    const primaryAction = _bulkTaskVisibility === 'exclude'
        ? '<button type="button" class="bulk-task-visibility-btn" data-bulk-action="include">Show hidden</button>'
        : '<button type="button" class="bulk-task-visibility-btn" data-bulk-action="exclude">Hide bulk</button>';
    const showAllAction = _bulkTaskVisibility === 'only'
        ? '<button type="button" class="bulk-task-visibility-btn" data-bulk-action="include">Show all visible</button>'
        : '';

    controlElements.forEach((controlEl) => {
        controlEl.classList.remove('hidden');
        controlEl.innerHTML = `
            <span class="bulk-task-visibility-summary">
                <strong>${_hiddenBulkTaskTotal}</strong> hidden-by-default bulk tasks
                <span>${escapeHtml(modeLabel)}</span>
            </span>
            <div class="bulk-task-visibility-actions">
                ${primaryAction}
                ${showAllAction}
                ${collectionButtons}
            </div>
        `;

        controlEl.querySelectorAll('[data-bulk-action]').forEach((button) => {
            button.addEventListener('click', async (event) => {
                const action = event.currentTarget?.dataset?.bulkAction || 'exclude';
                if (action === 'only') {
                    _bulkTaskVisibility = 'only';
                    _bulkTaskCollectionId = event.currentTarget?.dataset?.bulkCollectionId || '';
                } else if (action === 'include') {
                    _bulkTaskVisibility = 'include';
                    _bulkTaskCollectionId = '';
                } else {
                    _bulkTaskVisibility = 'exclude';
                    _bulkTaskCollectionId = '';
                }
                if (_isGlobalTabMode) {
                    await loadGlobalTasks();
                } else {
                    await loadTasks(_currentSessionId);
                }
            });
        });
    });
}

/**
 * Render the task list in the panel.
 */
function renderTaskList() {
    if (!_taskListEl) return;

    const filteredTasks = getFilteredTasks();
    ensureSelectedTaskStillValid(filteredTasks);
    renderGlobalTaskSummary(filteredTasks);
    renderBulkTaskVisibilityControls();

    if (filteredTasks.length === 0) {
        const hasGroupFilter = _selectedTaskGroupIds.size > 0;
        const hasHiddenBulkTasks = _isGlobalTabMode
            && _bulkTaskVisibility === 'exclude'
            && _hiddenBulkTaskTotal > 0;
        const emptyHint = hasHiddenBulkTasks
            ? 'Show hidden bulk collections to include Jira migration tasks'
            : (hasGroupFilter ? 'Adjust selected task groups to broaden the list' : 'Create a task using the form above');
        _taskListEl.innerHTML = `
            <div class="task-empty-state">
                <span class="task-empty-icon">📋</span>
                <p>No tasks match the active filters</p>
                <p class="task-empty-hint">${escapeHtml(emptyHint)}</p>
            </div>
        `;
        renderGlobalTaskInspector();
        return;
    }

    let html = '';
    if (_viewMode === 'list') {
        const sortedTasks = [...filteredTasks].sort((left, right) => {
            const leftTimestamp = new Date(left.updated_at || left.created_at || 0).getTime();
            const rightTimestamp = new Date(right.updated_at || right.created_at || 0).getTime();
            return rightTimestamp - leftTimestamp;
        });
        html = '<div class="task-list-mode">';
        sortedTasks.forEach((task) => {
            html += renderTaskItem(task);
        });
        html += '</div>';
    } else {
        const grouped = groupTasksByStatus(filteredTasks);
        const boardStatuses = ['in_progress', 'pending', 'blocked', 'completed', 'cancelled'];
        html = '<div class="task-board-view">';
        boardStatuses.forEach((status) => {
            html += renderTaskGroup(status, grouped[status] || []);
        });
        html += '</div>';
    }

    _taskListEl.innerHTML = html;
    _taskListEl.dataset.viewMode = _viewMode;

    renderGlobalTaskInspector();
    attachTaskEventListeners();
}

/**
 * Group tasks by status.
 */
function groupTasksByStatus(tasks) {
    const grouped = {};
    tasks.forEach(task => {
        const status = task.status || 'pending';
        if (!grouped[status]) {
            grouped[status] = [];
        }
        grouped[status].push(task);
    });
    return grouped;
}

/**
 * Render a group of tasks with a status header.
 */
function renderTaskGroup(status, tasks) {
    const statusInfo = getStatusInfo(status);
    let html = `
        <div class="task-group" data-status="${status}">
            <div class="task-group-header">
                <span class="task-group-icon">${statusInfo.icon}</span>
                <span class="task-group-label">${statusInfo.label}</span>
                <span class="task-group-count">(${tasks.length})</span>
            </div>
            <div class="task-group-body">
    `;

    tasks.forEach((task) => {
        html += renderTaskItem(task);
    });

    if (tasks.length === 0) {
        html += '<div class="task-lane-empty">No tasks</div>';
    }

    html += '</div></div>';
    return html;
}

/**
 * Get task ID (supports both API field name task_concept_id and legacy concept_id).
 */
function getTaskId(task) {
    return task.task_concept_id || task.concept_id || '';
}

function normaliseTaskConceptId(value) {
    if (typeof value !== 'string') return '';
    const trimmed = value.trim();
    if (!trimmed.startsWith('#V#') || trimmed.length <= 3) return '';
    return trimmed;
}

function deriveTaskGroupConceptId(task) {
    if (!task || typeof task !== 'object') return '';
    const candidates = [
        task.task_group_concept_id,
        task.group_concept_id,
        task.parent_task_concept_id,
        task.epic_task_concept_id,
    ];
    for (const candidate of candidates) {
        const normalised = normaliseTaskConceptId(candidate);
        if (normalised) {
            return normalised;
        }
    }
    return '';
}

function deriveGroupLabelFromConceptId(conceptId) {
    const normalised = normaliseTaskConceptId(conceptId);
    if (!normalised) return 'Unknown group';
    const slug = normalised.slice(3);
    const label = slug
        .split(/[_-]+/)
        .map((part) => part.trim())
        .filter(Boolean)
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join(' ');
    return label || normalised;
}

function getTaskGroupStorageKey() {
    let userId = 'anonymous';
    let orgId = 'none';
    try {
        const ctx = getUserContext ? getUserContext() : {};
        const normalisedUser = normaliseTaskConceptId(ctx?.user_id);
        const normalisedOrg = normaliseTaskConceptId(ctx?.org_id);
        if (normalisedUser) userId = normalisedUser;
        if (normalisedOrg) orgId = normalisedOrg;
    } catch (_) {
        // Ignore local/session storage lookup errors.
    }
    return `${TASK_GROUP_STORAGE_KEY_PREFIX}:${userId}:${orgId}`;
}

function loadTaskGroupSelectionFromStorage() {
    const nextStorageKey = getTaskGroupStorageKey();
    if (_taskGroupStorageKey === nextStorageKey) return;
    _taskGroupStorageKey = nextStorageKey;
    _selectedTaskGroupIds = new Set();
    try {
        const raw = localStorage.getItem(_taskGroupStorageKey);
        if (!raw) return;
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) return;
        parsed.forEach((item) => {
            const normalised = normaliseTaskConceptId(item);
            if (normalised) {
                _selectedTaskGroupIds.add(normalised);
            }
        });
    } catch (err) {
        console.warn('[taskPanel] Failed to read task group filter selection:', err);
    }
}

function persistTaskGroupSelectionToStorage() {
    if (!_taskGroupStorageKey) {
        _taskGroupStorageKey = getTaskGroupStorageKey();
    }
    try {
        localStorage.setItem(
            _taskGroupStorageKey,
            JSON.stringify([..._selectedTaskGroupIds]),
        );
    } catch (err) {
        console.warn('[taskPanel] Failed to persist task group filter selection:', err);
    }
}

function compareTaskGroupOptions(left, right) {
    const leftName = String(left?.displayName || '');
    const rightName = String(right?.displayName || '');
    return leftName.localeCompare(rightName, undefined, { sensitivity: 'base' });
}

function resolveTaskGroupDisplayName(conceptId) {
    return (
        _taskGroupDisplayNameCache.get(conceptId)
        || deriveGroupLabelFromConceptId(conceptId)
    );
}

function renderTaskGroupFilterControls() {
    const rows = [
        document.getElementById('taskGroupFilterRow'),
        _globalTasksContainer?.querySelector('#globalTaskGroupFilterRow'),
    ].filter(Boolean);

    rows.forEach((row) => {
        if (!row) return;
        if (_taskGroupOptions.length === 0) {
            row.classList.add('hidden');
            row.innerHTML = '';
            return;
        }

        row.classList.remove('hidden');
        const allActive = _selectedTaskGroupIds.size === 0;
        const chips = _taskGroupOptions.map((group) => {
            const isActive = _selectedTaskGroupIds.has(group.conceptId);
            return `
                <button type="button"
                    class="task-group-filter-btn ${isActive ? 'active' : ''}"
                    data-group-id="${escapeHtml(group.conceptId)}"
                    aria-pressed="${isActive ? 'true' : 'false'}"
                    title="Toggle task group ${escapeHtml(group.displayName)}">
                    ${escapeHtml(group.displayName)}
                    <span class="task-group-filter-count">${group.count}</span>
                </button>
            `;
        }).join('');

        row.innerHTML = `
            <span class="task-group-filter-label">Groups:</span>
            <button type="button"
                class="task-group-filter-btn task-group-filter-all ${allActive ? 'active' : ''}"
                data-group-id=""
                aria-pressed="${allActive ? 'true' : 'false'}"
                title="Show all task groups">
                All groups
            </button>
            ${chips}
        `;

        row.querySelectorAll('.task-group-filter-btn').forEach((btn) => {
            btn.addEventListener('click', (event) => {
                const groupId = normaliseTaskConceptId(
                    event.currentTarget?.dataset?.groupId || '',
                );
                if (!groupId) {
                    _selectedTaskGroupIds.clear();
                } else if (_selectedTaskGroupIds.has(groupId)) {
                    _selectedTaskGroupIds.delete(groupId);
                } else {
                    _selectedTaskGroupIds.add(groupId);
                }
                persistTaskGroupSelectionToStorage();
                renderTaskGroupFilterControls();
                renderTaskList();
            });
        });
    });
}

async function ensureTaskGroupDisplayName(conceptId) {
    const normalised = normaliseTaskConceptId(conceptId);
    if (!normalised) return;
    if (_taskGroupDisplayNameCache.has(normalised)) return;
    if (_taskGroupNameFetchInFlight.has(normalised)) return;

    _taskGroupNameFetchInFlight.add(normalised);
    try {
        const encoded = encodeURIComponent(normalised);
        const payload = await getJson(
            `/vontology/api/vontology/node_content?identifier=${encoded}&raw_only=1&soft=1`,
        );
        const fromNames = selectBestNameForContext(payload?.raw_doc?.names);
        const displayName = (
            (typeof fromNames === 'string' && fromNames.trim())
            || (typeof payload?.display_name === 'string' && payload.display_name.trim())
            || deriveGroupLabelFromConceptId(normalised)
        );
        _taskGroupDisplayNameCache.set(normalised, displayName);
        let didUpdate = false;
        _taskGroupOptions = _taskGroupOptions.map((group) => {
            if (group.conceptId !== normalised) return group;
            if (group.displayName === displayName) return group;
            didUpdate = true;
            return {
                ...group,
                displayName,
            };
        });
        if (didUpdate) {
            _taskGroupOptions.sort(compareTaskGroupOptions);
            renderTaskGroupFilterControls();
        }
    } catch (err) {
        // Keep fallback labels when ontology lookups fail.
        console.debug('[taskPanel] Failed to resolve task group name:', normalised, err);
    } finally {
        _taskGroupNameFetchInFlight.delete(normalised);
    }
}

function refreshTaskGroupOptions() {
    const counts = new Map();
    _tasks.forEach((task) => {
        const groupId = deriveTaskGroupConceptId(task);
        if (!groupId) return;
        counts.set(groupId, (counts.get(groupId) || 0) + 1);
    });

    _taskGroupOptions = Array.from(counts.entries()).map(([conceptId, count]) => ({
        conceptId,
        count,
        displayName: resolveTaskGroupDisplayName(conceptId),
    }));
    _taskGroupOptions.sort(compareTaskGroupOptions);

    const availableGroupIds = new Set(_taskGroupOptions.map((group) => group.conceptId));
    let didPruneSelection = false;
    [..._selectedTaskGroupIds].forEach((groupId) => {
        if (!availableGroupIds.has(groupId)) {
            _selectedTaskGroupIds.delete(groupId);
            didPruneSelection = true;
        }
    });
    if (didPruneSelection) {
        persistTaskGroupSelectionToStorage();
    }

    renderTaskGroupFilterControls();
    _taskGroupOptions.forEach((group) => {
        void ensureTaskGroupDisplayName(group.conceptId);
    });
}

function deriveConceptNameFromId(conceptId, fallbackName = '') {
    const fallback = typeof fallbackName === 'string' ? fallbackName.trim() : '';
    if (fallback) return fallback;
    const normalised = normaliseTaskConceptId(conceptId);
    if (!normalised) return '';
    const raw = normalised.slice(3);
    const pretty = raw
        .split(/[_-]+/)
        .map((part) => part.trim())
        .filter(Boolean)
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join(' ');
    return pretty || normalised;
}

function renderTaskConceptLink({
    conceptId,
    conceptName = '',
    text,
    className,
    title,
    ariaLabel,
}) {
    const safeText = escapeHtml(text);
    const safeClass = escapeHtml(className || '');
    const normalisedId = normaliseTaskConceptId(conceptId);
    if (!normalisedId) {
        return `<span class="${safeClass}">${safeText}</span>`;
    }
    const resolvedName = deriveConceptNameFromId(normalisedId, conceptName) || normalisedId;
    const tooltip = title || 'Open concept';
    const label = ariaLabel || tooltip;
    return `
        <button type="button"
            class="${safeClass} task-concept-link"
            data-concept-id="${escapeHtml(normalisedId)}"
            data-concept-name="${escapeHtml(resolvedName)}"
            title="${escapeHtml(tooltip)}"
            aria-label="${escapeHtml(label)}">
            ${safeText}
        </button>
    `;
}

function openTaskPanelConcept(conceptId, conceptName = '') {
    const normalisedId = normaliseTaskConceptId(conceptId);
    if (!normalisedId) return;
    const resolvedName = deriveConceptNameFromId(normalisedId, conceptName) || normalisedId;
    document.dispatchEvent(new CustomEvent('open-concept-tab', {
        detail: {
            conceptId: normalisedId,
            conceptName: resolvedName,
            activate: true,
        },
    }));
}

function renderTaskMetaChips(chips, cssClass) {
    if (!Array.isArray(chips) || chips.length === 0) return '';
    const display = chips.slice(0, 4).map((value) => `
        <span class="${cssClass}">${escapeHtml(value)}</span>
    `).join('');
    const remainder = chips.length - 4;
    const remainderChip = remainder > 0 ? `<span class="${cssClass}">+${remainder} more</span>` : '';
    return `
        <div class="task-meta-chip-row">
            ${display}
            ${remainderChip}
        </div>
    `;
}

function renderTaskLinkRows(task) {
    const links = Array.isArray(task.task_links) ? task.task_links : [];
    if (links.length === 0) {
        return '<p class="task-detail-empty">No task links</p>';
    }
    return links.map((link) => {
        const linkType = escapeHtml(link.link_type || 'relates_to');
        const targetId = escapeHtml(link.target_task_concept_id || '');
        return `
            <li class="task-link-row">
                <span class="task-link-type">${linkType}</span>
                <span class="task-link-target">${targetId}</span>
                <button class="task-link-remove-btn"
                    data-task-id="${escapeHtml(getTaskId(task))}"
                    data-link-type="${linkType}"
                    data-target-task-id="${targetId}"
                    title="Remove link">
                    Remove
                </button>
            </li>
        `;
    }).join('');
}

function renderTaskCommentRows(comments) {
    if (!Array.isArray(comments) || comments.length === 0) {
        return '<p class="task-detail-empty">No comments</p>';
    }
    return comments.map((comment) => `
        <li class="task-detail-row">
            <div class="task-detail-row-main">${escapeHtml(comment.body || '')}</div>
            <div class="task-detail-row-meta">
                ${escapeHtml(comment.author_concept_id || 'Unknown author')}
                • ${escapeHtml(formatDateTimeOrDash(comment.created_at))}
            </div>
        </li>
    `).join('');
}

function renderTaskAttachmentRows(attachments) {
    if (!Array.isArray(attachments) || attachments.length === 0) {
        return '<p class="task-detail-empty">No attachments</p>';
    }
    return attachments.map((attachment) => {
        const uri = escapeHtml(attachment.uri || '');
        const filename = escapeHtml(attachment.filename || 'Attachment');
        const note = attachment.note ? `<span class="task-detail-row-meta">Note: ${escapeHtml(attachment.note)}</span>` : '';
        return `
            <li class="task-detail-row">
                <a class="task-attachment-link" href="${uri}" target="_blank" rel="noopener noreferrer">${filename}</a>
                <div class="task-detail-row-meta">${escapeHtml(formatDateTimeOrDash(attachment.created_at))}</div>
                ${note}
            </li>
        `;
    }).join('');
}

function renderTaskHistoryRows(history) {
    if (!Array.isArray(history) || history.length === 0) {
        return '<p class="task-detail-empty">No history events</p>';
    }
    return history.slice(0, 20).map((eventRow) => `
        <li class="task-detail-row">
            <div class="task-detail-row-main">${escapeHtml(eventRow.event_type || 'event')}</div>
            <div class="task-detail-row-meta">${escapeHtml(formatDateTimeOrDash(eventRow.created_at))}</div>
        </li>
    `).join('');
}

function renderTaskDetailsPanel(task, detailState) {
    const taskId = getTaskId(task);
    const detailTask = detailState.task || task;

    if (detailState.loading) {
        return `
            <div class="task-detail-panel" data-task-id="${escapeHtml(taskId)}">
                <div class="task-detail-loading">Loading task details…</div>
            </div>
        `;
    }

    if (detailState.error) {
        return `
            <div class="task-detail-panel" data-task-id="${escapeHtml(taskId)}">
                <div class="task-detail-error">${escapeHtml(detailState.error)}</div>
            </div>
        `;
    }

    const labelsValue = Array.isArray(detailTask.labels) ? detailTask.labels.join(', ') : '';
    const componentsValue = Array.isArray(detailTask.components) ? detailTask.components.join(', ') : '';
    const fixVersionsValue = Array.isArray(detailTask.fix_versions) ? detailTask.fix_versions.join(', ') : '';
    const sprintValues = Array.isArray(detailTask.sprint_values) ? detailTask.sprint_values.join(', ') : '';
    const backlogRankValue = typeof detailTask.backlog_rank === 'string' ? detailTask.backlog_rank : '';
    const parentValue = detailTask.parent_task_concept_id || '';
    const epicValue = detailTask.epic_task_concept_id || '';
    const reportToValue = detailTask.report_to_concept_id || '';
    const startValue = isoToLocalInputValue(detailTask.start_date);
    const dueValue = isoToLocalInputValue(detailTask.due_date);

    return `
        <div class="task-detail-panel" data-task-id="${escapeHtml(taskId)}">
            <div class="task-detail-section">
                <h4>Editable task context</h4>
                <div class="task-detail-grid">
                    <label class="task-detail-label">
                        Task type
                        <select class="task-detail-input task-detail-type-input">
                            <option value="">Unspecified</option>
                            ${getTaskTypeOptions().map((option) => `
                                <option value="${escapeHtml(option.concept_id)}" ${(detailTask.task_type_ids || []).includes(option.concept_id) ? 'selected' : ''}>
                                    ${escapeHtml(option.label)}
                                </option>
                            `).join('')}
                        </select>
                    </label>
                    <label class="task-detail-label">
                        Task source
                        <select class="task-detail-input task-detail-source-input">
                            <option value="">Unspecified</option>
                            ${getTaskSourceOptions().map((option) => `
                                <option value="${escapeHtml(option.concept_id)}" ${detailTask.task_source_id === option.concept_id ? 'selected' : ''}>
                                    ${escapeHtml(option.label)}
                                </option>
                            `).join('')}
                        </select>
                    </label>
                    <label class="task-detail-label">
                        Report to concept ID
                        <input class="task-detail-input task-detail-report-to-input" type="text" value="${escapeHtml(reportToValue)}" />
                    </label>
                    <label class="task-detail-label">
                        Task role
                        <input class="task-detail-input task-detail-role-input" type="text" value="${escapeHtml(detailTask.task_role || '')}" />
                    </label>
                    <label class="task-detail-label">
                        Reference code
                        <input class="task-detail-input task-detail-reference-code-input" type="text" value="${escapeHtml(detailTask.reference_code || '')}" />
                    </label>
                    <label class="task-detail-label">
                        Progress signal
                        <input class="task-detail-input task-detail-progress-signal-input" type="text" value="${escapeHtml(detailTask.progress_signal || '')}" />
                    </label>
                    <label class="task-detail-label">
                        Evidence
                        <input class="task-detail-input task-detail-evidence-input" type="text" value="${escapeHtml(detailTask.evidence || '')}" />
                    </label>
                    <label class="task-detail-label">
                        Next checkpoint
                        <textarea class="task-detail-input task-detail-next-checkpoint-input" rows="2">${escapeHtml(detailTask.next_checkpoint || '')}</textarea>
                    </label>
                    <label class="task-detail-label">
                        Notes
                        <textarea class="task-detail-input task-detail-notes-input" rows="3">${escapeHtml(detailTask.notes || '')}</textarea>
                    </label>
                    <label class="task-detail-label">
                        Labels (comma separated)
                        <input class="task-detail-input task-detail-labels-input" type="text" value="${escapeHtml(labelsValue)}" />
                    </label>
                    <label class="task-detail-label">
                        Components (comma separated)
                        <input class="task-detail-input task-detail-components-input" type="text" value="${escapeHtml(componentsValue)}" />
                    </label>
                    <label class="task-detail-label">
                        Fix versions (comma separated)
                        <input class="task-detail-input task-detail-fix-versions-input" type="text" value="${escapeHtml(fixVersionsValue)}" />
                    </label>
                    <label class="task-detail-label">
                        Sprint values (comma separated)
                        <input class="task-detail-input task-detail-sprint-values-input" type="text" value="${escapeHtml(sprintValues)}" />
                    </label>
                    <label class="task-detail-label">
                        Backlog rank
                        <input class="task-detail-input task-detail-backlog-rank-input" type="text" value="${escapeHtml(backlogRankValue)}" />
                    </label>
                    <label class="task-detail-label">
                        Parent task concept ID
                        <input class="task-detail-input task-detail-parent-input" type="text" value="${escapeHtml(parentValue)}" />
                    </label>
                    <label class="task-detail-label">
                        Epic task concept ID
                        <input class="task-detail-input task-detail-epic-input" type="text" value="${escapeHtml(epicValue)}" />
                    </label>
                    <label class="task-detail-label">
                        Start date/time
                        <input class="task-detail-input task-detail-start-input" type="datetime-local" value="${escapeHtml(startValue)}" />
                    </label>
                    <label class="task-detail-label">
                        Due date/time
                        <input class="task-detail-input task-detail-due-input" type="datetime-local" value="${escapeHtml(dueValue)}" />
                    </label>
                </div>
                <button class="task-save-fields-btn" data-task-id="${escapeHtml(taskId)}">Save details</button>
            </div>

            <div class="task-detail-section">
                <h4>Task links</h4>
                <ul class="task-detail-list">
                    ${renderTaskLinkRows(detailTask)}
                </ul>
                <div class="task-detail-inline-form">
                    <input class="task-detail-input task-link-target-input" type="text" placeholder="Target task concept ID (e.g. #V#task_2)" />
                    <select class="task-detail-input task-link-type-input">
                        ${TASK_LINK_OPTIONS.map((option) => `
                            <option value="${option.value}">${option.label}</option>
                        `).join('')}
                    </select>
                    <button class="task-add-link-btn" data-task-id="${escapeHtml(taskId)}">Add link</button>
                </div>
            </div>

            <div class="task-detail-section">
                <h4>Comments</h4>
                <ul class="task-detail-list">
                    ${renderTaskCommentRows(detailState.comments)}
                </ul>
                <div class="task-detail-inline-form">
                    <textarea class="task-detail-input task-comment-input" rows="2" placeholder="Add a comment…"></textarea>
                    <button class="task-add-comment-btn" data-task-id="${escapeHtml(taskId)}">Add comment</button>
                </div>
            </div>

            <div class="task-detail-section">
                <h4>Attachments</h4>
                <ul class="task-detail-list">
                    ${renderTaskAttachmentRows(detailState.attachments)}
                </ul>
                <div class="task-detail-grid">
                    <label class="task-detail-label">
                        Filename
                        <input class="task-detail-input task-attachment-filename-input" type="text" placeholder="spec.pdf" />
                    </label>
                    <label class="task-detail-label">
                        URI
                        <input class="task-detail-input task-attachment-uri-input" type="url" placeholder="https://example.org/spec.pdf" />
                    </label>
                    <label class="task-detail-label">
                        Note
                        <input class="task-detail-input task-attachment-note-input" type="text" placeholder="Optional note" />
                    </label>
                </div>
                <button class="task-add-attachment-btn" data-task-id="${escapeHtml(taskId)}">Add attachment</button>
            </div>

            <div class="task-detail-section">
                <h4>History</h4>
                <ul class="task-detail-list">
                    ${renderTaskHistoryRows(detailState.history)}
                </ul>
            </div>
        </div>
    `;
}

function renderTaskConceptValue(conceptId, fallbackName = '') {
    const normalisedId = normaliseTaskConceptId(conceptId);
    if (!normalisedId) return '—';
    return renderTaskConceptLink({
        conceptId: normalisedId,
        conceptName: fallbackName,
        text: resolveTaskGroupDisplayName(normalisedId) || deriveConceptNameFromId(normalisedId, fallbackName),
        className: 'task-inspector-concept',
        title: 'Open concept',
        ariaLabel: `Open concept ${deriveConceptNameFromId(normalisedId, fallbackName) || normalisedId}`,
    });
}

function renderTaskTimeline(detailState, task) {
    const history = Array.isArray(detailState?.history) ? detailState.history : [];
    const steps = history.length > 0
        ? history.slice(0, 5)
        : [
            { event_type: 'task_created', created_at: task?.created_at },
            { event_type: task?.status || 'pending', created_at: task?.updated_at || task?.created_at },
        ];

    return `
        <div class="task-inspector-timeline" role="list" aria-label="Task timeline">
            ${steps.map((step, index) => `
                <div class="task-timeline-step ${index === 0 ? 'is-active' : ''}" role="listitem">
                    <span class="task-timeline-dot" aria-hidden="true"></span>
                    <span class="task-timeline-label">${escapeHtml((step?.event_type || 'event').replace(/_/g, ' '))}</span>
                    <span class="task-timeline-time">${escapeHtml(formatDateTimeOrDash(step?.created_at))}</span>
                </div>
            `).join('')}
        </div>
    `;
}

function renderTaskInspector(task, detailState) {
    const detailTask = detailState?.task || task;
    const taskId = getTaskId(detailTask);
    const sourceSummary = getTaskSourceSummary(detailTask);
    const taskTypes = Array.isArray(detailTask?.task_types) ? detailTask.task_types : [];
    const labelsValue = Array.isArray(detailTask.labels) ? detailTask.labels.join(', ') : '';
    const componentsValue = Array.isArray(detailTask.components) ? detailTask.components.join(', ') : '';
    const fixVersionsValue = Array.isArray(detailTask.fix_versions) ? detailTask.fix_versions.join(', ') : '';
    const sprintValues = Array.isArray(detailTask.sprint_values) ? detailTask.sprint_values.join(', ') : '';
    const backlogRankValue = typeof detailTask.backlog_rank === 'string' ? detailTask.backlog_rank : '';
    const parentValue = detailTask.parent_task_concept_id || '';
    const epicValue = detailTask.epic_task_concept_id || '';
    const reportToValue = detailTask.report_to_concept_id || '';
    const startValue = isoToLocalInputValue(detailTask.start_date);
    const dueValue = isoToLocalInputValue(detailTask.due_date);

    if (detailState?.loading) {
        return `
            <div class="task-inspector-card">
                <div class="task-detail-loading">Loading task details…</div>
            </div>
        `;
    }

    if (detailState?.error) {
        return `
            <div class="task-inspector-card">
                <div class="task-detail-error">${escapeHtml(detailState.error)}</div>
            </div>
        `;
    }

    return `
        <div class="task-inspector-card" data-task-id="${escapeHtml(taskId)}">
            <div class="task-inspector-header">
                <div>
                    <div class="task-inspector-eyebrow">${escapeHtml(detailTask.reference_code || taskId)}</div>
                    <h3>${escapeHtml(detailTask.title || 'Untitled task')}</h3>
                    <p>${escapeHtml(detailTask.description || 'No description yet.')}</p>
                </div>
                <div class="task-inspector-badges">
                    <span class="task-status-badge">${escapeHtml(getStatusInfo(detailTask.status).label)}</span>
                    <span class="task-priority-chip">${escapeHtml(getPriorityInfo(detailTask.priority).label)}</span>
                </div>
            </div>

            ${renderTaskTimeline(detailState, detailTask)}

            <div class="task-inspector-table">
                <div class="task-inspector-row"><div class="task-inspector-label">Title</div><div class="task-inspector-value">${escapeHtml(detailTask.title || 'Untitled task')}</div></div>
                <div class="task-inspector-row"><div class="task-inspector-label">Type</div><div class="task-inspector-value">${taskTypes.length > 0 ? taskTypes.map((item) => `<span class="task-type-chip">${escapeHtml(item.label || item.concept_id || 'Typed')}</span>`).join('') : '<span class="task-empty-inline">Unspecified</span>'}</div></div>
                <div class="task-inspector-row"><div class="task-inspector-label">Source</div><div class="task-inspector-value">${sourceSummary ? `<span class="task-source-chip">${escapeHtml(sourceSummary.label)}</span>` : '<span class="task-empty-inline">Unspecified</span>'}</div></div>
                <div class="task-inspector-row"><div class="task-inspector-label">Priority</div><div class="task-inspector-value">${escapeHtml(getPriorityInfo(detailTask.priority).label)}</div></div>
                <div class="task-inspector-row"><div class="task-inspector-label">Owner</div><div class="task-inspector-value">${renderTaskConceptValue(detailTask.assignee_concept_id)}</div></div>
                <div class="task-inspector-row"><div class="task-inspector-label">Report to</div><div class="task-inspector-value">${renderTaskConceptValue(detailTask.report_to_concept_id)}</div></div>
                <div class="task-inspector-row"><div class="task-inspector-label">Von role</div><div class="task-inspector-value">${escapeHtml(detailTask.task_role || '—')}</div></div>
                <div class="task-inspector-row"><div class="task-inspector-label">Status</div><div class="task-inspector-value">${escapeHtml(getStatusInfo(detailTask.status).label)}</div></div>
                <div class="task-inspector-row"><div class="task-inspector-label">Next checkpoint</div><div class="task-inspector-value">${escapeHtml(detailTask.next_checkpoint || '—')}</div></div>
                <div class="task-inspector-row"><div class="task-inspector-label">Start / deadline</div><div class="task-inspector-value">${escapeHtml([formatDateTimeOrDash(detailTask.start_date), formatDateTimeOrDash(detailTask.due_date)].filter((value) => value !== '—').join(' → ') || '—')}</div></div>
                <div class="task-inspector-row"><div class="task-inspector-label">Progress signal</div><div class="task-inspector-value">${escapeHtml(detailTask.progress_signal || '—')}</div></div>
                <div class="task-inspector-row"><div class="task-inspector-label">Evidence</div><div class="task-inspector-value">${escapeHtml(detailTask.evidence || '—')}</div></div>
                <div class="task-inspector-row"><div class="task-inspector-label">Notes</div><div class="task-inspector-value">${escapeHtml(detailTask.notes || '—')}</div></div>
                <div class="task-inspector-row"><div class="task-inspector-label">Attachments / links</div><div class="task-inspector-value">${escapeHtml(`${detailTask.attachments_count || 0} attachments, ${(detailTask.task_links || []).length} links`)}</div></div>
            </div>

            <div class="task-detail-section">
                <h4>Editable task context</h4>
                <div class="task-detail-grid">
                    <label class="task-detail-label">
                        Task type
                        <select class="task-detail-input task-detail-type-input">
                            <option value="">Unspecified</option>
                            ${getTaskTypeOptions().map((option) => `
                                <option value="${escapeHtml(option.concept_id)}" ${(detailTask.task_type_ids || []).includes(option.concept_id) ? 'selected' : ''}>
                                    ${escapeHtml(option.label)}
                                </option>
                            `).join('')}
                        </select>
                    </label>
                    <label class="task-detail-label">
                        Task source
                        <select class="task-detail-input task-detail-source-input">
                            <option value="">Unspecified</option>
                            ${getTaskSourceOptions().map((option) => `
                                <option value="${escapeHtml(option.concept_id)}" ${detailTask.task_source_id === option.concept_id ? 'selected' : ''}>
                                    ${escapeHtml(option.label)}
                                </option>
                            `).join('')}
                        </select>
                    </label>
                    <label class="task-detail-label">
                        Report to concept ID
                        <input class="task-detail-input task-detail-report-to-input" type="text" value="${escapeHtml(reportToValue)}" />
                    </label>
                    <label class="task-detail-label">
                        Task role
                        <input class="task-detail-input task-detail-role-input" type="text" value="${escapeHtml(detailTask.task_role || '')}" />
                    </label>
                    <label class="task-detail-label">
                        Reference code
                        <input class="task-detail-input task-detail-reference-code-input" type="text" value="${escapeHtml(detailTask.reference_code || '')}" />
                    </label>
                    <label class="task-detail-label">
                        Progress signal
                        <input class="task-detail-input task-detail-progress-signal-input" type="text" value="${escapeHtml(detailTask.progress_signal || '')}" />
                    </label>
                    <label class="task-detail-label">
                        Evidence
                        <input class="task-detail-input task-detail-evidence-input" type="text" value="${escapeHtml(detailTask.evidence || '')}" />
                    </label>
                    <label class="task-detail-label">
                        Next checkpoint
                        <textarea class="task-detail-input task-detail-next-checkpoint-input" rows="2">${escapeHtml(detailTask.next_checkpoint || '')}</textarea>
                    </label>
                    <label class="task-detail-label">
                        Notes
                        <textarea class="task-detail-input task-detail-notes-input" rows="3">${escapeHtml(detailTask.notes || '')}</textarea>
                    </label>
                    <label class="task-detail-label">
                        Labels (comma separated)
                        <input class="task-detail-input task-detail-labels-input" type="text" value="${escapeHtml(labelsValue)}" />
                    </label>
                    <label class="task-detail-label">
                        Components (comma separated)
                        <input class="task-detail-input task-detail-components-input" type="text" value="${escapeHtml(componentsValue)}" />
                    </label>
                    <label class="task-detail-label">
                        Fix versions (comma separated)
                        <input class="task-detail-input task-detail-fix-versions-input" type="text" value="${escapeHtml(fixVersionsValue)}" />
                    </label>
                    <label class="task-detail-label">
                        Sprint values (comma separated)
                        <input class="task-detail-input task-detail-sprint-values-input" type="text" value="${escapeHtml(sprintValues)}" />
                    </label>
                    <label class="task-detail-label">
                        Backlog rank
                        <input class="task-detail-input task-detail-backlog-rank-input" type="text" value="${escapeHtml(backlogRankValue)}" />
                    </label>
                    <label class="task-detail-label">
                        Parent task concept ID
                        <input class="task-detail-input task-detail-parent-input" type="text" value="${escapeHtml(parentValue)}" />
                    </label>
                    <label class="task-detail-label">
                        Epic task concept ID
                        <input class="task-detail-input task-detail-epic-input" type="text" value="${escapeHtml(epicValue)}" />
                    </label>
                    <label class="task-detail-label">
                        Start date/time
                        <input class="task-detail-input task-detail-start-input" type="datetime-local" value="${escapeHtml(startValue)}" />
                    </label>
                    <label class="task-detail-label">
                        Due date/time
                        <input class="task-detail-input task-detail-due-input" type="datetime-local" value="${escapeHtml(dueValue)}" />
                    </label>
                </div>
                <button class="task-save-fields-btn" data-task-id="${escapeHtml(taskId)}">Save details</button>
            </div>

            <div class="task-detail-section">
                <h4>Task links</h4>
                <ul class="task-detail-list">
                    ${renderTaskLinkRows(detailTask)}
                </ul>
                <div class="task-detail-inline-form">
                    <input class="task-detail-input task-link-target-input" type="text" placeholder="Target task concept ID (e.g. #V#task_2)" />
                    <select class="task-detail-input task-link-type-input">
                        ${TASK_LINK_OPTIONS.map((option) => `
                            <option value="${option.value}">${option.label}</option>
                        `).join('')}
                    </select>
                    <button class="task-add-link-btn" data-task-id="${escapeHtml(taskId)}">Add link</button>
                </div>
            </div>

            <div class="task-detail-section">
                <h4>Comments</h4>
                <ul class="task-detail-list">
                    ${renderTaskCommentRows(detailState?.comments)}
                </ul>
                <div class="task-detail-inline-form">
                    <textarea class="task-detail-input task-comment-input" rows="2" placeholder="Add a comment…"></textarea>
                    <button class="task-add-comment-btn" data-task-id="${escapeHtml(taskId)}">Add comment</button>
                </div>
            </div>

            <div class="task-detail-section">
                <h4>Attachments</h4>
                <ul class="task-detail-list">
                    ${renderTaskAttachmentRows(detailState?.attachments)}
                </ul>
                <div class="task-detail-grid">
                    <label class="task-detail-label">
                        Filename
                        <input class="task-detail-input task-attachment-filename-input" type="text" placeholder="spec.pdf" />
                    </label>
                    <label class="task-detail-label">
                        URI
                        <input class="task-detail-input task-attachment-uri-input" type="url" placeholder="https://example.org/spec.pdf" />
                    </label>
                    <label class="task-detail-label">
                        Note
                        <input class="task-detail-input task-attachment-note-input" type="text" placeholder="Optional note" />
                    </label>
                </div>
                <button class="task-add-attachment-btn" data-task-id="${escapeHtml(taskId)}">Add attachment</button>
            </div>

            <div class="task-detail-section">
                <h4>History</h4>
                <ul class="task-detail-list">
                    ${renderTaskHistoryRows(detailState?.history)}
                </ul>
            </div>
        </div>
    `;
}

function renderGlobalTaskInspector() {
    const inspectorEl = _globalTasksContainer?.querySelector('#globalTaskInspector');
    if (!inspectorEl || !_isGlobalTabMode) return;

    if (!_selectedTaskId) {
        inspectorEl.innerHTML = `
            <div class="task-inspector-empty">
                <h3>Select a task</h3>
                <p>Choose a card from the board to see a fuller task view and edit its richer task context.</p>
            </div>
        `;
        return;
    }

    const selectedTask = _tasks.find((task) => getTaskId(task) === _selectedTaskId);
    if (!selectedTask) {
        inspectorEl.innerHTML = `
            <div class="task-inspector-empty">
                <h3>Task not available</h3>
                <p>The currently selected task is no longer visible with the active filters.</p>
            </div>
        `;
        return;
    }

    const detailState = getTaskDetailState(_selectedTaskId);
    inspectorEl.innerHTML = renderTaskInspector(selectedTask, detailState);
}

async function selectTask(taskId, { loadDetails = true } = {}) {
    if (!taskId) return;
    _selectedTaskId = taskId;
    renderTaskList();
    if (!loadDetails) return;

    const detailState = getTaskDetailState(taskId);
    if (detailState.loading) return;
    if (detailState.task) {
        return;
    }
    await loadTaskDetails(taskId);
}

/**
 * Render a single task item.
 */
function renderTaskItem(task) {
    const taskId = getTaskId(task);
    const priorityInfo = getPriorityInfo(task.priority);
    const statusInfo = getStatusInfo(task.status);
    const detailState = getTaskDetailState(taskId);
    const primaryType = getTaskPrimaryType(task);
    const sourceSummary = getTaskSourceSummary(task);
    const hiddenBulkCollections = getHiddenBulkTaskCollections(task);
    const isSelected = _isGlobalTabMode && taskId === _selectedTaskId;

    // Escape HTML in title/description
    const titleText = task.title || 'Untitled Task';
    const description = escapeHtml(task.description || '');
    const truncatedDescription = description.length > 120
        ? description.substring(0, 120) + '...'
        : description;
    const titleHtml = renderTaskConceptLink({
        conceptId: taskId,
        conceptName: titleText,
        text: titleText,
        className: 'task-title task-title-link',
        title: 'Open task concept',
        ariaLabel: `Open task concept ${titleText}`,
    });

    // Format dates if present
    let startDateHtml = '';
    if (task.start_date) {
        const startDate = new Date(task.start_date);
        startDateHtml = `
            <span class="task-start-date" title="Start date">
                🟢 ${startDate.toLocaleDateString()}
            </span>
        `;
    }

    let dueDateHtml = '';
    if (task.due_date) {
        const dueDate = new Date(task.due_date);
        const isOverdue = dueDate < new Date() && task.status !== 'completed' && task.status !== 'cancelled';
        dueDateHtml = `
            <span class="task-due-date ${isOverdue ? 'overdue' : ''}" title="Due date">
                📅 ${dueDate.toLocaleDateString()}
            </span>
        `;
    }

    // Show conversation link in global mode (when _currentSessionId is null)
    let conversationLinkHtml = '';
    if (!_currentSessionId && task.conversation_session_id) {
        const convName = escapeHtml(task.conversation_name || 'Unnamed');
        conversationLinkHtml = `
            <a href="#" class="task-conversation-link"
               data-session-id="${escapeHtml(task.conversation_session_id)}"
               title="Open conversation: ${convName}">
                💬 Conversation: ${convName}
            </a>
        `;
    }

    const labelsHtml = renderTaskMetaChips(task.labels, 'task-label-chip');
    const componentsHtml = renderTaskMetaChips(task.components, 'task-component-chip');
    const typeChipsHtml = primaryType
        ? `<div class="task-meta-chip-row"><span class="task-type-chip">${escapeHtml(primaryType.label || 'Typed')}</span>${sourceSummary ? `<span class="task-source-chip">${escapeHtml(sourceSummary.label || 'Sourced')}</span>` : ''}</div>`
        : (sourceSummary ? `<div class="task-meta-chip-row"><span class="task-source-chip">${escapeHtml(sourceSummary.label || 'Sourced')}</span></div>` : '');
    const bulkCollectionChipsHtml = hiddenBulkCollections.length > 0
        ? `<div class="task-meta-chip-row">${hiddenBulkCollections.map((collection) => `
            <span class="task-bulk-collection-chip" title="Hidden by default bulk collection">
                ${escapeHtml(collection.label || deriveConceptNameFromId(collection.collection_id))}
            </span>
        `).join('')}</div>`
        : '';
    const parentChipHtml = task.parent_task_concept_id
        ? renderTaskConceptLink({
            conceptId: task.parent_task_concept_id,
            text: `Parent: ${task.parent_task_concept_id}`,
            className: 'task-hierarchy-chip',
            title: 'Open parent concept',
            ariaLabel: `Open parent concept ${task.parent_task_concept_id}`,
        })
        : '';
    const hierarchyHtml = `
        <div class="task-hierarchy-row">
            ${parentChipHtml}
            ${task.epic_task_concept_id ? `<span class="task-hierarchy-chip">Epic: ${escapeHtml(task.epic_task_concept_id)}</span>` : ''}
        </div>
    `;
    const linksCount = Array.isArray(task.task_links) ? task.task_links.length : 0;
    const referenceCodeHtml = task.reference_code
        ? `<span class="task-reference-badge">${escapeHtml(task.reference_code)}</span>`
        : '';

    return `
        <div class="task-item ${_isGlobalTabMode ? 'task-item-selectable' : ''} ${isSelected ? 'is-selected' : ''} ${hiddenBulkCollections.length > 0 ? 'task-item-bulk-hidden' : ''}" data-task-id="${taskId}" ${_isGlobalTabMode ? 'tabindex="0" role="button"' : ''}>
            <div class="task-item-header">
                <span class="task-priority" title="Priority: ${priorityInfo.label}">${priorityInfo.icon}</span>
                ${referenceCodeHtml}
                ${titleHtml}
                <span class="task-status-badge" title="Status">${statusInfo.icon} ${escapeHtml(statusInfo.label)}</span>
            </div>
            <div class="task-item-body">
                <p class="task-description">${truncatedDescription}</p>
                ${typeChipsHtml}
                ${bulkCollectionChipsHtml}
                ${labelsHtml}
                ${componentsHtml}
                ${hierarchyHtml}
                ${conversationLinkHtml}
            </div>
            <div class="task-item-footer">
                ${startDateHtml}
                ${dueDateHtml}
                <span class="task-meta-counts">
                    💬 ${task.comments_count || 0}
                    📎 ${task.attachments_count || 0}
                    🕓 ${task.history_count || 0}
                    🔗 ${linksCount}
                </span>
                <div class="task-actions">
                    <button class="task-detail-toggle-btn" data-task-id="${taskId}" title="${_isGlobalTabMode ? 'Inspect task details' : 'Show task details'}">
                        ${_isGlobalTabMode ? 'Inspect' : (detailState.expanded ? 'Hide details' : 'Details')}
                    </button>
                    <select class="task-status-select" data-task-id="${taskId}" title="Change status">
                        ${TASK_STATUS_OPTIONS.map(opt => `
                            <option value="${opt.value}" ${task.status === opt.value ? 'selected' : ''}>
                                ${opt.icon} ${opt.label}
                            </option>
                        `).join('')}
                    </select>
                    <button class="task-delete-btn" data-task-id="${taskId}" title="Delete task">🗑️</button>
                </div>
            </div>
            ${(!_isGlobalTabMode && detailState.expanded) ? renderTaskDetailsPanel(task, detailState) : ''}
        </div>
    `;
}

/**
 * Simple HTML escape.
 */
function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function replaceCachedTask(updatedTask) {
    const updatedTaskId = getTaskId(updatedTask);
    if (!updatedTaskId) return;
    const existingIndex = _tasks.findIndex((item) => getTaskId(item) === updatedTaskId);
    if (existingIndex >= 0) {
        _tasks[existingIndex] = updatedTask;
        refreshTaskGroupOptions();
    }
}

async function loadTaskDetails(taskId, { refreshList = false } = {}) {
    const detailState = getTaskDetailState(taskId);
    detailState.loading = true;
    detailState.error = null;
    renderTaskList();

    try {
        const [task, commentsResult, attachmentsResult, historyResult] = await Promise.all([
            getJson(`/api/tasks/${encodeURIComponent(taskId)}`),
            getJson(`/api/tasks/${encodeURIComponent(taskId)}/comments?limit=100`),
            getJson(`/api/tasks/${encodeURIComponent(taskId)}/attachments?limit=100`),
            getJson(`/api/tasks/${encodeURIComponent(taskId)}/history?limit=200`),
        ]);
        detailState.task = task;
        detailState.comments = commentsResult.comments || [];
        detailState.attachments = attachmentsResult.attachments || [];
        detailState.history = historyResult.history || [];
        replaceCachedTask(task);
        if (refreshList) {
            updateTaskCountBadge();
        }
    } catch (err) {
        console.error('[taskPanel] Failed to load task details:', err);
        detailState.error = 'Failed to load task details';
    } finally {
        detailState.loading = false;
        renderTaskList();
    }
}

async function toggleTaskDetails(taskId) {
    const detailState = getTaskDetailState(taskId);
    detailState.expanded = !detailState.expanded;
    renderTaskList();
    if (detailState.expanded) {
        await loadTaskDetails(taskId);
    }
}

async function saveTaskParityFields(taskId, panelEl) {
    const payload = {};
    const setIfPresent = (selector, key, transform = (value) => value) => {
        const input = panelEl.querySelector(selector);
        if (!input) return;
        payload[key] = transform(input.value || '');
    };

    setIfPresent('.task-detail-type-input', 'task_type_ids', (value) => {
        const trimmed = value.trim();
        return trimmed ? [trimmed] : [];
    });
    setIfPresent('.task-detail-source-input', 'task_source_id', (value) => value.trim() || null);
    setIfPresent('.task-detail-report-to-input', 'report_to_concept_id', (value) => value.trim() || null);
    setIfPresent('.task-detail-role-input', 'task_role', (value) => value.trim() || null);
    setIfPresent('.task-detail-next-checkpoint-input', 'next_checkpoint', (value) => value.trim() || null);
    setIfPresent('.task-detail-progress-signal-input', 'progress_signal', (value) => value.trim() || null);
    setIfPresent('.task-detail-evidence-input', 'evidence', (value) => value.trim() || null);
    setIfPresent('.task-detail-notes-input', 'notes', (value) => value.trim() || null);
    setIfPresent('.task-detail-reference-code-input', 'reference_code', (value) => value.trim() || null);
    setIfPresent('.task-detail-labels-input', 'labels', parseCsvList);
    setIfPresent('.task-detail-components-input', 'components', parseCsvList);
    setIfPresent('.task-detail-fix-versions-input', 'fix_versions', parseCsvList);
    setIfPresent('.task-detail-sprint-values-input', 'sprint_values', parseCsvList);
    setIfPresent('.task-detail-backlog-rank-input', 'backlog_rank', (value) => value.trim() || null);
    setIfPresent('.task-detail-parent-input', 'parent_task_concept_id', (value) => value.trim() || null);
    setIfPresent('.task-detail-epic-input', 'epic_task_concept_id', (value) => value.trim() || null);
    setIfPresent('.task-detail-start-input', 'start_date', localInputValueToIso);
    setIfPresent('.task-detail-due-input', 'due_date', localInputValueToIso);

    await patchJson(`/api/tasks/${encodeURIComponent(taskId)}`, payload);
    showToast('Task fields updated', 'success');
    await loadTaskDetails(taskId, { refreshList: true });
}

async function addTaskLink(taskId, panelEl) {
    const targetRaw = panelEl.querySelector('.task-link-target-input')?.value || '';
    const linkType = panelEl.querySelector('.task-link-type-input')?.value || 'relates_to';
    const targetTaskId = targetRaw.trim();
    if (!targetTaskId) {
        showToast('Target task concept ID is required', 'warning');
        return;
    }

    await postJson(`/api/tasks/${encodeURIComponent(taskId)}/links`, {
        target_task_concept_id: targetTaskId,
        link_type: linkType,
    });
    const targetInput = panelEl.querySelector('.task-link-target-input');
    if (targetInput) targetInput.value = '';
    showToast('Task link added', 'success');
    await loadTaskDetails(taskId, { refreshList: true });
}

async function removeTaskLink(taskId, targetTaskId, linkType) {
    await postJson(`/api/tasks/${encodeURIComponent(taskId)}/links/remove`, {
        target_task_concept_id: targetTaskId,
        link_type: linkType,
    });
    showToast('Task link removed', 'success');
    await loadTaskDetails(taskId, { refreshList: true });
}

async function addTaskComment(taskId, panelEl) {
    const bodyRaw = panelEl.querySelector('.task-comment-input')?.value || '';
    const body = bodyRaw.trim();
    if (!body) {
        showToast('Comment body is required', 'warning');
        return;
    }

    await postJson(`/api/tasks/${encodeURIComponent(taskId)}/comments`, { body });
    const input = panelEl.querySelector('.task-comment-input');
    if (input) input.value = '';
    showToast('Comment added', 'success');
    await loadTaskDetails(taskId, { refreshList: true });
}

async function addTaskAttachment(taskId, panelEl) {
    const filename = (panelEl.querySelector('.task-attachment-filename-input')?.value || '').trim();
    const uri = (panelEl.querySelector('.task-attachment-uri-input')?.value || '').trim();
    const note = (panelEl.querySelector('.task-attachment-note-input')?.value || '').trim();

    if (!filename || !uri) {
        showToast('Filename and URI are required', 'warning');
        return;
    }

    await postJson(`/api/tasks/${encodeURIComponent(taskId)}/attachments`, {
        filename,
        uri,
        note: note || null,
    });
    const filenameInput = panelEl.querySelector('.task-attachment-filename-input');
    const uriInput = panelEl.querySelector('.task-attachment-uri-input');
    const noteInput = panelEl.querySelector('.task-attachment-note-input');
    if (filenameInput) filenameInput.value = '';
    if (uriInput) uriInput.value = '';
    if (noteInput) noteInput.value = '';
    showToast('Attachment added', 'success');
    await loadTaskDetails(taskId, { refreshList: true });
}

/**
 * Attach event listeners to task items.
 */
function attachTaskEventListeners() {
    const roots = [
        _taskListEl,
        _globalTasksContainer?.querySelector('#globalTaskInspector'),
    ].filter(Boolean);

    if (roots.length === 0) return;

    roots.forEach((root) => {
        root.querySelectorAll('.task-concept-link').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                const conceptId = e.currentTarget.dataset.conceptId;
                const conceptName = e.currentTarget.dataset.conceptName || '';
                openTaskPanelConcept(conceptId, conceptName);
            });
            btn.addEventListener('keydown', (e) => {
                if (e.key !== 'Enter' && e.key !== ' ') return;
                e.preventDefault();
                e.stopPropagation();
                e.currentTarget.click();
            });
        });

        root.querySelectorAll('.task-status-select').forEach((select) => {
            select.addEventListener('change', async (e) => {
                const taskId = e.target.dataset.taskId;
                const newStatus = e.target.value;
                await updateTaskStatus(taskId, newStatus);
            });
        });

        root.querySelectorAll('.task-delete-btn').forEach((btn) => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const taskId = e.currentTarget.dataset.taskId;
                if (confirm('Delete this task?')) {
                    await deleteTask(taskId);
                }
            });
        });

        root.querySelectorAll('.task-detail-toggle-btn').forEach((btn) => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const taskId = e.currentTarget.dataset.taskId;
                if (!taskId) return;
                if (_isGlobalTabMode) {
                    await selectTask(taskId);
                } else {
                    await toggleTaskDetails(taskId);
                }
            });
        });

        root.querySelectorAll('.task-save-fields-btn').forEach((btn) => {
            btn.addEventListener('click', async (e) => {
                const taskId = e.currentTarget.dataset.taskId;
                const panelEl = e.currentTarget.closest('.task-detail-panel')
                    || e.currentTarget.closest('.task-inspector-card');
                if (!taskId || !panelEl) return;
                try {
                    await saveTaskParityFields(taskId, panelEl);
                } catch (err) {
                    console.error('[taskPanel] Failed to save task fields:', err);
                    showToast('Failed to update task fields', 'error');
                }
            });
        });

        root.querySelectorAll('.task-add-link-btn').forEach((btn) => {
            btn.addEventListener('click', async (e) => {
                const taskId = e.currentTarget.dataset.taskId;
                const panelEl = e.currentTarget.closest('.task-detail-panel')
                    || e.currentTarget.closest('.task-inspector-card');
                if (!taskId || !panelEl) return;
                try {
                    await addTaskLink(taskId, panelEl);
                } catch (err) {
                    console.error('[taskPanel] Failed to add task link:', err);
                    showToast('Failed to add task link', 'error');
                }
            });
        });

        root.querySelectorAll('.task-link-remove-btn').forEach((btn) => {
            btn.addEventListener('click', async (e) => {
                const taskId = e.currentTarget.dataset.taskId;
                const targetTaskId = e.currentTarget.dataset.targetTaskId;
                const linkType = e.currentTarget.dataset.linkType;
                if (!taskId || !targetTaskId || !linkType) return;
                try {
                    await removeTaskLink(taskId, targetTaskId, linkType);
                } catch (err) {
                    console.error('[taskPanel] Failed to remove task link:', err);
                    showToast('Failed to remove task link', 'error');
                }
            });
        });

        root.querySelectorAll('.task-add-comment-btn').forEach((btn) => {
            btn.addEventListener('click', async (e) => {
                const taskId = e.currentTarget.dataset.taskId;
                const panelEl = e.currentTarget.closest('.task-detail-panel')
                    || e.currentTarget.closest('.task-inspector-card');
                if (!taskId || !panelEl) return;
                try {
                    await addTaskComment(taskId, panelEl);
                } catch (err) {
                    console.error('[taskPanel] Failed to add task comment:', err);
                    showToast('Failed to add comment', 'error');
                }
            });
        });

        root.querySelectorAll('.task-add-attachment-btn').forEach((btn) => {
            btn.addEventListener('click', async (e) => {
                const taskId = e.currentTarget.dataset.taskId;
                const panelEl = e.currentTarget.closest('.task-detail-panel')
                    || e.currentTarget.closest('.task-inspector-card');
                if (!taskId || !panelEl) return;
                try {
                    await addTaskAttachment(taskId, panelEl);
                } catch (err) {
                    console.error('[taskPanel] Failed to add task attachment:', err);
                    showToast('Failed to add attachment', 'error');
                }
            });
        });

        root.querySelectorAll('.task-conversation-link').forEach((link) => {
            link.addEventListener('click', async (e) => {
                e.preventDefault();
                e.stopPropagation();
                const sessionId = e.currentTarget.dataset.sessionId;
                if (sessionId) {
                    activateTab('chatTab');
                    try {
                        const { switchToChatSession } = await import('../chatTab.js');
                        await switchToChatSession(sessionId);
                    } catch (err) {
                        console.warn('[taskPanel] Failed to switch to conversation:', err);
                        showToast('Could not open conversation', 'error');
                    }
                }
            });
        });
    });

    if (_isGlobalTabMode && _taskListEl) {
        _taskListEl.querySelectorAll('.task-item.task-item-selectable').forEach((item) => {
            item.addEventListener('click', async (e) => {
                if (e.target.closest('button, select, a, input, textarea, label, option')) return;
                const taskId = e.currentTarget.dataset.taskId;
                if (taskId) {
                    await selectTask(taskId);
                }
            });
            item.addEventListener('keydown', async (e) => {
                if (e.key !== 'Enter' && e.key !== ' ') return;
                e.preventDefault();
                const taskId = e.currentTarget.dataset.taskId;
                if (taskId) {
                    await selectTask(taskId);
                }
            });
        });
    }
}

/**
 * Handle create task button click.
 */
async function handleCreateTask() {
    const titleInput = document.getElementById('newTaskTitle');
    const descInput = document.getElementById('newTaskDescription');
    const prioritySelect = document.getElementById('newTaskPriority');

    if (!titleInput || !descInput) return;

    const title = titleInput.value.trim();
    const description = descInput.value.trim();

    if (!title) {
        showToast('Task title is required', 'warning');
        titleInput.focus();
        return;
    }
    if (!description) {
        showToast('Task description is required', 'warning');
        descInput.focus();
        return;
    }

    try {
        const payload = {
            title,
            description,
            priority: prioritySelect?.value || 'medium',
        };

        // Link to current conversation if available
        if (_currentSessionId) {
            payload.session_id = _currentSessionId;
        }

        await postJson('/api/tasks/', payload);

        // Clear form
        titleInput.value = '';
        descInput.value = '';
        if (prioritySelect) prioritySelect.value = 'medium';

        showToast('Task created', 'success');

        // Reload tasks
        await loadTasks(_currentSessionId);

    } catch (err) {
        console.error('[taskPanel] Failed to create task:', err);
        showToast('Failed to create task', 'error');
    }
}

/**
 * Update a task's status.
 */
async function updateTaskStatus(taskId, newStatus) {
    try {
        await patchJson(`/api/tasks/${encodeURIComponent(taskId)}`, { status: newStatus });
        showToast('Task updated', 'success');
        await loadTasks(_currentSessionId);
    } catch (err) {
        console.error('[taskPanel] Failed to update task:', err);
        showToast('Failed to update task', 'error');
    }
}

/**
 * Delete a task.
 */
async function deleteTask(taskId) {
    try {
        await deleteJson(`/api/tasks/${encodeURIComponent(taskId)}`);
        showToast('Task deleted', 'success');

        // Remove from local cache and re-render
        _tasks = _tasks.filter(t => getTaskId(t) !== taskId);
        refreshTaskGroupOptions();
        renderTaskList();
        updateTaskCountBadge();

    } catch (err) {
        console.error('[taskPanel] Failed to delete task:', err);
        showToast('Failed to delete task', 'error');
    }
}

/**
 * Update loading state.
 */
function updateLoadingState(isLoading) {
    if (!_taskListEl) return;

    if (isLoading) {
        _taskListEl.classList.add('is-loading');
    } else {
        _taskListEl.classList.remove('is-loading');
    }
}

/**
 * Update the task count badge in both the conversation toggle button and global tasks button.
 */
function updateTaskCountBadge() {
    const count = getTaskCount();
    const displayCount = count > 99 ? '99+' : String(count);

    // Update conversation-specific badge
    const badge = document.getElementById('taskCountBadge');
    if (badge) {
        if (count > 0) {
            badge.textContent = displayCount;
            badge.classList.remove('hidden');
        } else {
            badge.classList.add('hidden');
        }
    }

    // Update global tasks badge
    const globalBadge = document.getElementById('globalTaskCountBadge');
    if (globalBadge) {
        if (count > 0) {
            globalBadge.textContent = displayCount;
            globalBadge.classList.remove('hidden');
        } else {
            globalBadge.classList.add('hidden');
        }
    }
}

/**
 * Get all loaded tasks.
 */
export function getTasks() {
    return [..._tasks];
}

/**
 * Refresh tasks for the current context.
 * If a conversation session is active, loads tasks for that conversation.
 * Otherwise, loads all tasks for the current user (global view).
 */
export async function refreshTasks() {
    if (_currentSessionId) {
        await loadTasks(_currentSessionId);
    } else if (_isGlobalTabMode) {
        await loadGlobalTasks();
    } else {
        await loadMyTasks();
    }
}
