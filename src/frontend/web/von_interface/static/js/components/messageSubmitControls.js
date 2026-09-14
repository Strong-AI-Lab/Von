import { getSubmitMode, setSubmitMode, selectedSubmitMode, presentSubmitMode, subscribeSubmitMode, submitArrowMarkup } from './submitMode.js';

// This route currently has no verified live-turn transport. Never relabel queue
// acceptance as steering; retain the draft so the sender can choose separate work.
export function createMessageSubmitControls({ button, getActor, onSubmit, onUnavailable }) {
    button.innerHTML = submitArrowMarkup;
    const menu = document.createElement('details');
    menu.className = 'message-submit-actions conversation-actions';
    const summary = document.createElement('summary');
    summary.textContent = 'Submit options';
    const panel = document.createElement('div');
    panel.className = 'conversation-actions-panel';
    const label = document.createElement('label');
    label.textContent = 'Default action (shared with chat in this browser): ';
    const select = document.createElement('select');
    select.add(new Option('Queue', 'queue'));
    select.add(new Option('Steering', 'steer'));
    label.append(select);
    const help = document.createElement('p');
    help.id = `${button.id}ModeHelp`;
    help.textContent = 'Queue sends a separate message. Active coding-agent steering is not available on this route yet. Choosing Steering retains your draft. Shift-click the arrow for the opposite action once; Shift+Enter inserts a newline. Choose either action here with keyboard or touch.';
    select.setAttribute('aria-describedby', help.id);
    button.setAttribute('aria-describedby', help.id);
    const actions = [];
    for (const [mode, text] of [['queue', 'Queue this message'], ['steer', 'Steer (unavailable)']]) {
        const action = document.createElement('button');
        action.type = 'button';
        action.textContent = text;
        action.onclick = () => activate({}, mode);
        panel.append(action);
        actions.push(action);
    }
    panel.prepend(label, help);
    menu.append(summary, panel);
    button.parentElement.after(menu);
    const status = document.createElement('span');
    status.className = 'sr-only';
    status.setAttribute('role', 'status');
    menu.after(status);
    select.onchange = () => setSubmitMode(getActor(), select.value);
    let pending = false;
    function update(isPending = pending) {
        pending = isPending;
        const mode = getSubmitMode(getActor());
        select.value = mode;
        select.disabled = pending;
        for (const action of actions) action.disabled = button.disabled;
        const description = mode === 'steer' ? 'Steering unavailable; draft will be retained' : 'Submit separate message';
        presentSubmitMode(button, mode, {
            label: pending ? 'Submitting message' : description,
            help: `${description}. Shift-click for the opposite action once. Submit options contains both actions and the shared default.`,
            pending
        });
    }
    function activate(event = {}, explicitMode) {
        if (pending || button.disabled) return;
        const mode = explicitMode || selectedSubmitMode(getActor(), event);
        if (mode === 'steer') {
            status.textContent = 'Active steering is unavailable on this route. Draft and attachments retained; choose Queue this message in Submit options for separate work.';
            onUnavailable(status.textContent);
            return;
        }
        status.textContent = '';
        menu.open = false;
        button.focus();
        onSubmit();
    }
    menu.addEventListener('keydown', event => {
        if (event.key === 'Escape') { event.preventDefault(); menu.open = false; summary.focus(); }
    });
    const unsubscribe = subscribeSubmitMode(() => update());
    update();
    return { activate, update, dispose() { unsubscribe(); menu.remove(); status.remove(); } };
}
