/**
 * Task Panel Component (JVNAUTOSCI-1040)
 *
 * Provides UI for viewing and managing tasks within conversations.
 * Tasks are stored as Vontology concepts and accessed via REST API.
 */

import { deleteJson, getJson, patchJson, postJson } from '../apiService.js';
import { activateTab } from '../tabNavigation.js';
import { showToast } from '../utils/toast.js';

// Task panel state
let _panelEl = null;
let _taskListEl = null;
let _globalTasksContainer = null;  // Container for global tasks tab
let _tasks = [];
let _currentSessionId = null;
let _isVisible = false;
let _isLoading = false;
let _filterStatus = 'all';
let _isGlobalTabMode = false;  // True when rendering into global tasks tab

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
export function showTaskPanel() {
    if (_panelEl) {
        _panelEl.classList.remove('hidden');
        _panelEl.setAttribute('aria-hidden', 'false');
        _isVisible = true;
        // Auto-refresh tasks when panel becomes visible
        refreshTasks();
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
export function toggleTaskPanel() {
    if (_isVisible) {
        hideTaskPanel();
    } else {
        showTaskPanel();
    }
}

/**
 * Show global tasks view in the dedicated tab (all user's tasks, not filtered by conversation).
 * Renders into the globalTasksContainer instead of the overlay panel.
 */
export async function showGlobalTasks() {
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

    // Render the task panel UI into the container
    renderGlobalTasksTabContent();

    // Load all user's tasks
    await loadMyTasks();
}

/**
 * Render the global tasks tab content structure.
 */
function renderGlobalTasksTabContent() {
    if (!_globalTasksContainer) return;

    _globalTasksContainer.innerHTML = `
        <div class="global-tasks-header">
            <h2>My Tasks</h2>
            <div class="global-tasks-controls">
                <select id="globalTaskStatusFilter" class="task-filter-select" title="Filter by status">
                    <option value="all">All tasks</option>
                    <option value="pending">Pending</option>
                    <option value="in_progress">In Progress</option>
                    <option value="completed">Completed</option>
                    <option value="cancelled">Cancelled</option>
                    <option value="blocked">Blocked</option>
                </select>
                <button id="refreshGlobalTasksBtn" class="task-refresh-btn" title="Refresh tasks">🔄</button>
            </div>
        </div>
        <div class="global-tasks-create">
            <input type="text" id="globalNewTaskTitle" class="task-input" placeholder="Task title...">
            <textarea id="globalNewTaskDescription" class="task-textarea" placeholder="Task description..." rows="2"></textarea>
            <div class="global-tasks-create-actions">
                <select id="globalNewTaskPriority" class="task-priority-select">
                    <option value="low">🟢 Low</option>
                    <option value="medium" selected>🟡 Medium</option>
                    <option value="high">🟠 High</option>
                    <option value="critical">🔴 Critical</option>
                </select>
                <button id="globalCreateTaskBtn" class="task-create-btn">Create Task</button>
            </div>
        </div>
        <div id="globalTaskList" class="task-list"></div>
    `;

    // Attach event listeners for the global tasks tab
    const filterSelect = _globalTasksContainer.querySelector('#globalTaskStatusFilter');
    if (filterSelect) {
        filterSelect.value = _filterStatus;
        filterSelect.addEventListener('change', (e) => {
            _filterStatus = e.target.value;
            renderTaskList();
        });
    }

    const refreshBtn = _globalTasksContainer.querySelector('#refreshGlobalTasksBtn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => loadMyTasks());
    }

    const createBtn = _globalTasksContainer.querySelector('#globalCreateTaskBtn');
    if (createBtn) {
        createBtn.addEventListener('click', handleGlobalCreateTask);
    }

    // Update the task list element reference for global mode
    _taskListEl = _globalTasksContainer.querySelector('#globalTaskList');
}

/**
 * Handle task creation from the global tasks tab.
 */
async function handleGlobalCreateTask() {
    const titleInput = _globalTasksContainer?.querySelector('#globalNewTaskTitle');
    const descInput = _globalTasksContainer?.querySelector('#globalNewTaskDescription');
    const prioritySelect = _globalTasksContainer?.querySelector('#globalNewTaskPriority');

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
        };

        await postJson('/api/tasks/', payload);
        showToast('Task created', 'success');

        // Clear the form
        titleInput.value = '';
        descInput.value = '';

        // Reload tasks
        await loadMyTasks();
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

    _isLoading = true;
    updateLoadingState(true);

    try {
        let url;
        const params = new URLSearchParams();

        if (sessionId) {
            // Load tasks for a specific conversation session
            url = '/api/tasks/';
            params.set('session_id', sessionId);
        } else {
            // Load current user's tasks via /my endpoint
            url = '/api/tasks/my';
            params.set('include_created', 'true');
        }

        if (params.toString()) {
            url += '?' + params.toString();
        }

        const response = await getJson(url);
        _tasks = response.tasks || [];
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

    _isLoading = true;
    updateLoadingState(true);

    try {
        let url = '/api/tasks/my';
        const params = new URLSearchParams();

        if (statusFilter && statusFilter !== 'all') {
            params.set('status', statusFilter);
        }
        params.set('include_created', 'true');

        if (params.toString()) {
            url += '?' + params.toString();
        }

        const response = await getJson(url);
        _tasks = response.tasks || [];
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

/**
 * Render the task list in the panel.
 */
function renderTaskList() {
    if (!_taskListEl) return;

    // Filter tasks based on status filter
    const filteredTasks = _filterStatus === 'all'
        ? _tasks
        : _tasks.filter(t => t.status === _filterStatus);

    if (filteredTasks.length === 0) {
        _taskListEl.innerHTML = `
            <div class="task-empty-state">
                <span class="task-empty-icon">📋</span>
                <p>No tasks ${_filterStatus !== 'all' ? `with status "${_filterStatus}"` : ''}</p>
                <p class="task-empty-hint">Create a task using the form above</p>
            </div>
        `;
        return;
    }

    // Group by status for better organisation
    const grouped = groupTasksByStatus(filteredTasks);

    let html = '';

    // Render active tasks first (pending, in_progress, blocked)
    ['in_progress', 'pending', 'blocked'].forEach(status => {
        if (grouped[status] && grouped[status].length > 0) {
            html += renderTaskGroup(status, grouped[status]);
        }
    });

    // Then completed/cancelled
    ['completed', 'cancelled'].forEach(status => {
        if (grouped[status] && grouped[status].length > 0) {
            html += renderTaskGroup(status, grouped[status]);
        }
    });

    _taskListEl.innerHTML = html;

    // Attach event listeners
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
    `;

    tasks.forEach(task => {
        html += renderTaskItem(task);
    });

    html += '</div>';
    return html;
}

/**
 * Get task ID (supports both API field name task_concept_id and legacy concept_id).
 */
function getTaskId(task) {
    return task.task_concept_id || task.concept_id || '';
}

/**
 * Render a single task item.
 */
function renderTaskItem(task) {
    const taskId = getTaskId(task);
    const priorityInfo = getPriorityInfo(task.priority);
    // Status info available if needed for future enhancements
    const _statusInfo = getStatusInfo(task.status);

    // Escape HTML in title/description
    const title = escapeHtml(task.title || 'Untitled Task');
    const description = escapeHtml(task.description || '');
    const truncatedDescription = description.length > 120
        ? description.substring(0, 120) + '...'
        : description;

    // Format due date if present
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

    return `
        <div class="task-item" data-task-id="${taskId}">
            <div class="task-item-header">
                <span class="task-priority" title="Priority: ${priorityInfo.label}">${priorityInfo.icon}</span>
                <span class="task-title">${title}</span>
            </div>
            <div class="task-item-body">
                <p class="task-description">${truncatedDescription}</p>
                ${conversationLinkHtml}
            </div>
            <div class="task-item-footer">
                ${dueDateHtml}
                <div class="task-actions">
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

/**
 * Attach event listeners to task items.
 */
function attachTaskEventListeners() {
    if (!_taskListEl) return;

    // Status change dropdowns
    _taskListEl.querySelectorAll('.task-status-select').forEach(select => {
        select.addEventListener('change', async (e) => {
            const taskId = e.target.dataset.taskId;
            const newStatus = e.target.value;
            await updateTaskStatus(taskId, newStatus);
        });
    });

    // Delete buttons
    _taskListEl.querySelectorAll('.task-delete-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            const taskId = e.target.dataset.taskId;
            if (confirm('Delete this task?')) {
                await deleteTask(taskId);
            }
        });
    });

    // Conversation links (in global mode)
    _taskListEl.querySelectorAll('.task-conversation-link').forEach(link => {
        link.addEventListener('click', async (e) => {
            e.preventDefault();
            const sessionId = e.target.dataset.sessionId;
            if (sessionId) {
                // Switch to chat tab and load the conversation
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
    } else {
        await loadMyTasks();
    }
}
