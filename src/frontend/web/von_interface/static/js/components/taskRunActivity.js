/** Private, inert coding-run records. No markdown/HTML execution of history. */
import { getJson, getUserContext, ensureUniqueWindowSessionId, WINDOW_SESSION_HEADER } from '../apiService.js';

export async function openTaskRunActivity(taskId) {
    const scope = JSON.stringify(getUserContext());
    const dialog = document.createElement('dialog');
    dialog.style.cssText = 'width:min(900px,92vw);max-height:85vh;overflow:auto;box-sizing:border-box';
    const heading = document.createElement('h3');
    heading.textContent = 'Coding run activity';
    const close = document.createElement('button');
    close.textContent = 'Close';
    close.addEventListener('click', () => dialog.close());
    const refresh = document.createElement('button');
    refresh.textContent = 'Refresh runs';
    const status = document.createElement('p');
    status.setAttribute('role', 'status');
    const runs = document.createElement('div');
    const activity = document.createElement('div');
    dialog.append(heading, close, refresh, status, runs, activity);
    const clearForScopeChange = () => dialog.close();
    document.addEventListener('orgSwitched', clearForScopeChange);
    document.addEventListener('authStatusChanged', clearForScopeChange);
    dialog.addEventListener('close', () => {
        document.removeEventListener('orgSwitched', clearForScopeChange);
        document.removeEventListener('authStatusChanged', clearForScopeChange);
        dialog.remove();
    }, { once: true });
    document.body.append(dialog);
    dialog.showModal();
    const base = `/api/tasks/${encodeURIComponent(taskId)}/runs`;
    let generation = 0;
    function current(requestGeneration) {
        if (scope !== JSON.stringify(getUserContext())) dialog.close();
        return dialog.isConnected && requestGeneration === generation;
    }
    function button(label, action, parent) {
        const element = document.createElement('button');
        element.textContent = label;
        element.addEventListener('click', action);
        parent.append(element);
    }
    async function showRun(runId, cursor = 0) {
        const requestGeneration = ++generation;
        status.textContent = 'Loading captured activity…';
        try {
            const result = await getJson(`${base}/${encodeURIComponent(runId)}?cursor=${cursor}`);
            if (!current(requestGeneration)) return;
            activity.replaceChildren();
            const run = result.run;
            status.textContent = `${run.capture_status} · Last captured ${run.updated_at} · ${run.event_count} records. ${run.capture_error ? `Capture needs retry (${run.capture_error}).` : ''}`;
            const info = document.createElement('p');
            info.textContent = `Run ${runId} · ${run.provenance?.execution_settings?.model || 'Model not recorded'} · reasoning effort ${run.provenance?.execution_settings?.reasoning_effort || 'not recorded'}. ${result.projection} Reasoning summaries: ${run.manifest?.reasoning_summaries?.availability || 'availability recorded at finalisation'}.`;
            activity.append(info);
            if (run.manifest?.source_complete === false) {
                const warning = document.createElement('p');
                warning.textContent = 'Archive stored; source capture is incomplete. See the manifest for missing or malformed records.';
                activity.append(warning);
            }
            if (run.archive_url) button('Download archive', async () => {
                try {
                    const response = await fetch(run.archive_url, { headers: {
                        [WINDOW_SESSION_HEADER]: await ensureUniqueWindowSessionId()
                    }});
                    if (!response.ok) throw new Error('Download unavailable');
                    const blob = await response.blob();
                    if (!current(requestGeneration)) return;
                    const url = URL.createObjectURL(blob);
                    const link = document.createElement('a');
                    link.href = url;
                    link.download = `coding-run-${runId}.zip`;
                    link.click();
                    setTimeout(() => URL.revokeObjectURL(url), 1000);
                } catch {
                    if (current(requestGeneration)) status.textContent = 'Archive download unavailable; refresh and retry.';
                }
            }, activity);
            button('Refresh this page', () => showRun(runId, cursor), activity);
            if (cursor) button('First records', () => showRun(runId), activity);
            for (const record of result.records) {
                const pre = document.createElement('pre');
                pre.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere;max-width:100%';
                pre.textContent = record.text + (record.display_truncated ? '\n[Display shortened; full redacted output in archive]' : '');
                activity.append(pre);
            }
            if (result.next_cursor > cursor) button(result.has_more ? 'Next records' : 'Check for newer records', () => showRun(runId, result.next_cursor), activity);
        } catch {
            if (current(requestGeneration)) status.textContent = 'Activity unavailable in this scope or capture storage is offline. Refresh to retry.';
        }
    }
    async function loadRuns(offset = 0) {
        const requestGeneration = ++generation;
        status.textContent = 'Loading runs…';
        try {
            const result = await getJson(`${base}?offset=${offset}`);
            if (!current(requestGeneration)) return;
            runs.replaceChildren();
            activity.replaceChildren();
            status.textContent = result.runs.length ? 'Select a run. Refresh to see the latest capture.' : 'No runs visible in this scope. Capture may not yet be registered.';
            for (const run of result.runs) button(`${run.started_at} · ${run.capture_status} · ${run.run_id}`, () => showRun(run.run_id), runs);
            if (result.next_offset !== null) button('Older runs', () => loadRuns(result.next_offset), runs);
        } catch {
            if (current(requestGeneration)) status.textContent = 'Run history unavailable. Refresh to retry.';
        }
    }
    refresh.addEventListener('click', () => loadRuns());
    await loadRuns();
}
