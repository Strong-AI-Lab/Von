import { getSubmitMode, setSubmitMode, selectedSubmitMode, presentSubmitMode, subscribeSubmitMode } from './submitMode.js';

/** Steering is bound to the displayed actor/session and exact active attempt. */
export function createChatSteeringControls({ sendButton, request, getDraft, clearDraft, createId, onQueue, onModeChange = () => {} }) {
    const button = document.createElement('button');
    button.type = 'button';
    button.id = 'steerButton';
    button.className = 'btn btn-secondary';
    button.textContent = 'Steer';
    button.title = 'Guide the active turn at its next model boundary. Running tools are not undone. Text only; use Queue Prompt for attachments.';
    const composer = sendButton.closest('.chat-composer') || sendButton.parentElement;
    const actions = composer.querySelector('.chat-composer-more-actions .button-row') || composer;
    const modeLabel = document.createElement('label');
    modeLabel.textContent = 'When Von is working, submit defaults to (this browser): ';
    const modeSelect = document.createElement('select');
    modeSelect.id = 'chatSubmitMode';
    for (const [value, text] of [['queue', 'Queue'], ['steer', 'Steering']]) {
        modeSelect.add(new Option(text, value));
    }
    modeLabel.append(modeSelect);
    const help = document.createElement('p');
    help.id = 'chatSubmitModeHelp';
    help.className = 'compact-speech-help';
    help.textContent = 'Queue starts separate work afterwards. Steering guides the active turn at its next opportunity; it does not undo tools. Shift-click the submit arrow for the opposite action once. Shift+Enter still inserts a newline. Both actions are available here.';
    modeSelect.setAttribute('aria-describedby', help.id);
    sendButton.setAttribute('aria-describedby', help.id);
    const queueButton = document.createElement('button');
    queueButton.type = 'button';
    queueButton.id = 'queueDraftButton';
    queueButton.className = 'btn btn-secondary';
    queueButton.textContent = 'Queue this message';
    queueButton.onclick = () => { if (!queueButton.disabled) onQueue?.(); };
    actions.prepend(modeLabel, help, button, queueButton);
    let actorKey;
    let defaultMode = 'queue';
    modeSelect.onchange = () => {
        defaultMode = modeSelect.value === 'steer' ? 'steer' : 'queue';
        setSubmitMode(actorKey, defaultMode);
        onModeChange();
    };
    const activity = document.createElement('details');
    activity.className = 'chat-steering-activity';
    const activityTitle = document.createElement('summary');
    activityTitle.textContent = 'Steering activity';
    activity.append(activityTitle);
    actions.append(activity);
    const toast = document.createElement('div');
    toast.className = 'chat-steering-toast';
    toast.setAttribute('role', 'status');
    toast.hidden = true;
    composer.before(toast);
    function positionToast() {
        toast.style.bottom = `${Math.max(8, Math.min(window.innerHeight - toast.offsetHeight - 8, window.innerHeight - composer.getBoundingClientRect().top + 8))}px`;
    }
    window.addEventListener('resize', positionToast);
    window.visualViewport?.addEventListener('resize', positionToast);
    const composerObserver = typeof ResizeObserver === 'function' ? new ResizeObserver(positionToast) : null;
    composerObserver?.observe(composer);
    let toastTimer;
    let announced = new Map();
    function announce(text) {
        clearTimeout(toastTimer);
        toast.textContent = text;
        toast.hidden = false;
        positionToast();
        toastTimer = setTimeout(() => { toast.hidden = true; }, 5000);
    }
    const feedback = document.createElement('div');
    feedback.className = 'chat-steering-feedback';
    activity.append(feedback);
    let current = {};
    let scopeKey;
    let records = new Map();
    const recordsByScope = new Map();
    let timer;
    let sending = false;
    let retry = null;
    let notice = '';
    let generation = 0;
    let renderedReceipts;
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
        queueButton.hidden = !current.busy;
        queueButton.disabled = sending || current.queueDisabled;
        modeSelect.value = defaultMode;
        const receiptSignature = JSON.stringify([scopeKey, notice, [...records]]);
        if (receiptSignature === renderedReceipts) return;
        renderedReceipts = receiptSignature;
        const focusId = feedback.contains(document.activeElement) ? document.activeElement.dataset.submissionId : null;
        feedback.replaceChildren();
        const pending = [...records.values()].flat().filter(item => item.status === 'pending').length;
        activityTitle.textContent = `Steering activity${pending ? ` (${pending} pending)` : ''}${notice ? ' — needs attention' : ''}`;
        if (notice && announced.get('notice') !== notice) { announce(notice); announced.set('notice', notice); }
        if (!notice) announced.delete('notice');
        if (notice) {
            const status = document.createElement('p');
            status.textContent = notice;
            feedback.append(status);
        }
        for (const [queueId, items] of records) {
            for (const item of items) {
                const receiptKey = `${queueId}:${item.id}`;
                if (announced.get(receiptKey) !== item.status) {
                    announce(labels[item.status] || item.status);
                    announced.set(receiptKey, item.status);
                }
                const row = document.createElement('div');
                const label = document.createElement('span');
                label.textContent = `${labels[item.status] || item.status}: ${item.text}`;
                row.append(label);
                if (item.status === 'pending') {
                    const cancel = document.createElement('button');
                    cancel.type = 'button';
                    cancel.dataset.submissionId = item.id;
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
        activity.hidden = !feedback.childElementCount;
        composer.dataset.steeringAttention = String(Boolean(notice) || pending > 0);
        if (notice) activity.open = true;
        if (focusId) {
            const replacement = [...feedback.querySelectorAll('button')].find(node => node.dataset.submissionId === focusId);
            (replacement || activityTitle).focus();
        }
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
    async function submit() {
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
    }
    button.onclick = submit;
    const unsubscribe = subscribeSubmitMode(() => {
        defaultMode = getSubmitMode(actorKey);
        render();
        onModeChange();
    });
    return {
        update(options) {
            if (actorKey !== options.actorKey) {
                actorKey = options.actorKey;
                defaultMode = getSubmitMode(actorKey);
            }
            const changed = scopeKey !== options.scopeKey;
            const targetChanged = current.target?.queueId !== options.target?.queueId;
            if (changed) {
                generation += 1;
                clearTimeout(toastTimer);
                toast.hidden = true;
                announced = new Map();
                activity.open = false;
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
        // Only user composer activation calls this; background queue dispatch is unchanged.
        activate(event = {}) {
            if (!current.busy) return false;
            const mode = selectedSubmitMode(actorKey, event);
            if (mode !== 'steer') return false;
            if (!current.target || current.disabled) {
                notice = 'Steering is unavailable for this draft or turn. Draft retained; choose Queue this message in More actions to send separate work.';
                render();
            } else void submit();
            return true;
        },
        present() {
            const mode = current.busy ? defaultMode : 'send';
            presentSubmitMode(sendButton, mode);
            if (!current.busy || current.queueDisabled) return;
            const label = mode === 'steer' ? 'Guide active turn' : 'Queue message';
            sendButton.setAttribute('aria-label', label);
            const text = sendButton.querySelector('.button-label');
            if (text) text.textContent = label;
            sendButton.title = `${label}. Shift-click for ${mode === 'steer' ? 'Queue' : 'Steering'} once. Change the default or choose either action in More actions.`;
        },
        isSending() { return sending; },
        dispose() { unsubscribe(); generation += 1; clearTimeout(timer); clearTimeout(toastTimer); composerObserver?.disconnect(); window.removeEventListener('resize', positionToast); window.visualViewport?.removeEventListener('resize', positionToast); button.remove(); activity.remove(); toast.remove(); modeLabel.remove(); help.remove(); queueButton.remove(); }
    };
}
