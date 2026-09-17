/** Share a task through the existing Tasks tab; identifiers stay losslessly encoded. */
export function buildTaskLink(taskId) {
    const url = new URL(window.location.pathname, window.location.origin);
    url.searchParams.set('task', taskId);
    url.hash = 'globalTasksTab';
    return url.href;
}

export function readTaskLink() {
    return new URL(window.location.href).searchParams.get('task') || '';
}
