/** Steering is bound to the displayed actor/session and exact active attempt. */
export function createChatSteeringControls({ sendButton, request, getDraft, clearDraft, createId }) {
    const button = document.createElement('button');
    button.type = 'button';
    button.id = 'steerButton';
    button.className = 'btn btn-secondary';
    button.textContent = 'Steer';
    button.title = 'Guide the active turn at its next model boundary. Running tools are not undone. Text only; use Queue Prompt for attachments.';
    sendButton.before(button);
    const feedback = document.createElement('div');
    feedback.className = 'chat-steering-feedback';
    feedback.setAttribute('aria-live', 'polite');
    sendButton.parentElement.after(feedback);
    let current = {};
    let scopeKey;
    let records = new Map();
    const recordsByScope = new Map();
    let timer;
    let sending = false;
    let retry = null;
    let notice = '';
    let generation = 0;
    function receiptItems(data) {
        if (!Array.isArray(data?.items)) throw new Error('Invalid steering status response');
        return data.items;
    }
    const path = (id) => `/${encodeURIComponent(id)}/steering`;
    const labels = {
        pending: 'Pending steer — waiting for the next model boundary',
        delivered: 'Delivered to active turn',
        cancelled: 'Steer cancelled',
        not_applied: 'Not applied — turn ended or stopped; copy this text to queue it'
    };
    function render() {
        button.hidden = !current.target;
        button.disabled = sending || current.disabled;
        button.textContent = sending ? 'Steering…' : 'Steer';
        feedback.replaceChildren();
        if (notice) {
            const status = document.createElement('p');
            status.textContent = notice;
            feedback.append(status);
        }
        for (const [queueId, items] of records) {
            for (const item of items) {
                const row = document.createElement('div');
                const label = document.createElement('span');
                label.textContent = `${labels[item.status] || item.status}: ${item.text}`;
                row.append(label);
                if (item.status === 'pending') {
                    const cancel = document.createElement('button');
                    cancel.type = 'button';
                    cancel.textContent = 'Cancel steer';
                    cancel.className = 'btn-mini';
                    cancel.onclick = async () => {
                        const version = generation;
                        cancel.disabled = true;
                        try {
                            const data = await request(path(queueId), { method: 'DELETE', body: JSON.stringify({ submission_id: item.id }) });
                            if (version !== generation) return;
                            records.set(queueId, receiptItems(data));
                        } catch (error) {
                            if (version !== generation) return;
                            notice = error.message;
                        }
                        render();
                    };
                    row.append(cancel);
                }
                feedback.append(row);
            }
        }
        feedback.hidden = !feedback.childElementCount;
    }
    async function refresh() {
        clearTimeout(timer);
        if (!button.isConnected) return;
        const version = generation;
        const ids = new Set([...records].filter(([, items]) => items.some(i => i.status === 'pending')).map(([id]) => id));
        if (current.target) ids.add(current.target.queueId);
        for (const id of ids) {
            try {
                const data = await request(path(id), { method: 'GET' });
                if (version !== generation) return;
                records.set(id, receiptItems(data));
                if (notice.startsWith('Steering status unavailable')) notice = '';
            } catch (_) {
                if (version !== generation) return;
                notice = 'Steering status unavailable; delivery has not been confirmed.';
            }
        }
        render();
        if (current.target || [...records.values()].some(items => items.some(i => i.status === 'pending'))) {
            timer = setTimeout(refresh, 1500);
        }
    }
    button.onclick = async () => {
        if (!current.target || current.disabled || sending) return;
        const text = getDraft();
        if (!text.trim()) return;
        const target = { ...current.target };
        const version = generation;
        // An uncertain POST is retried with the same ID, never duplicated.
        if (!retry || retry.text !== text || retry.queueId !== target.queueId || retry.attemptId !== target.attemptId) {
            retry = { ...target, text, id: createId() };
        }
        const submission = retry;
        sending = true;
        notice = '';
        render();
        try {
            const data = await request(path(target.queueId), { method: 'POST', body: JSON.stringify({
                text, submission_id: submission.id, attempt_id: target.attemptId
            }) });
            if (version !== generation) return;
            records.set(target.queueId, receiptItems(data));
            clearDraft(text);
            retry = null;
        } catch (error) {
            if (version !== generation) return;
            notice = `${error.message} Draft retained. Retry Steer to check an uncertain submission before queueing it.`;
        } finally {
            if (version === generation) {
                sending = false;
                render();
                void refresh();
            }
        }
    };
    return {
        update(options) {
            const changed = scopeKey !== options.scopeKey;
            const targetChanged = current.target?.queueId !== options.target?.queueId;
            if (changed) {
                generation += 1;
                records = recordsByScope.get(options.scopeKey) || new Map();
                recordsByScope.set(options.scopeKey, records);
                retry = null;
                sending = false;
                notice = '';
                scopeKey = options.scopeKey;
            }
            current = options;
            render();
            if (changed || targetChanged) void refresh();
        },
        isSending() { return sending; },
        dispose() { generation += 1; clearTimeout(timer); button.remove(); feedback.remove(); }
    };
}
