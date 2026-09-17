import { getSubmitMode, setSubmitMode, selectedSubmitMode, presentSubmitMode, subscribeSubmitMode, submitArrowMarkup } from './submitMode.js';

// This route currently has no verified live-turn transport. Never relabel queue
// acceptance as steering; retain the draft so the sender can choose separate work.
export function createMessageSubmitControls({ button, optionsPanel, getActor, onSubmit, onUnavailable, onSteer }) {
    button.innerHTML = submitArrowMarkup;
    const menu = optionsPanel?.closest('details') || document.createElement('details');
    const optionsName = optionsPanel ? 'More actions' : 'Submit options';
    const summary = menu.querySelector('summary') || document.createElement('summary');
    const panel = document.createElement(optionsPanel ? 'section' : 'div');
    panel.className = optionsPanel ? 'composer-options-section message-submit-options' : 'conversation-actions-panel';
    if (!optionsPanel) {
        menu.className = 'message-submit-actions conversation-actions';
        summary.textContent = 'Submit options';
        menu.append(summary, panel);
        button.parentElement.after(menu);
    } else {
        optionsPanel.append(panel);
    }
    const label = document.createElement('label');
    label.textContent = 'Default action (shared with chat in this browser): ';
    const select = document.createElement('select');
    select.add(new Option('Queue', 'queue'));
    select.add(new Option('Steering', 'steer'));
    label.append(select);
    const help = document.createElement('p');
    help.id = `${button.id}ModeHelp`;
    help.textContent = onSteer ? 'Queue sends separate work. Steering checks the recipient’s active coding turn. If unavailable, your draft stays here. Shift-click chooses the opposite action once.' : 'Queue sends a separate message. Active coding-agent steering is not available on this route yet. Choosing Steering retains your draft. Shift-click the arrow for the opposite action once; Shift+Enter inserts a newline. Choose either action here with keyboard or touch.';
    select.setAttribute('aria-describedby', help.id);
    button.setAttribute('aria-describedby', help.id);
    const actions = [];
    for (const [mode, text] of [['queue', 'Queue this message'], ['steer', onSteer ? 'Steer active coding turn' : 'Steer (unavailable)']]) {
        const action = document.createElement('button');
        action.type = 'button';
        action.textContent = text;
        action.onclick = () => activate({}, mode);
        panel.append(action);
        actions.push(action);
    }
    panel.prepend(label, help);
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
        const description = mode === 'steer' ? (onSteer ? 'Steer active coding turn' : 'Steering unavailable; draft will be retained') : 'Submit separate message';
        presentSubmitMode(button, mode, {
            label: pending ? 'Submitting message' : description,
            help: `${description}. Shift-click for the opposite action once. ${optionsName} contains both actions and the shared default.`,
            pending
        });
    }
    function activate(event = {}, explicitMode) {
        if (pending || button.disabled) return;
        const mode = explicitMode || selectedSubmitMode(getActor(), event);
        if (mode === 'steer' && onSteer) {
            menu.open = false;
            onSteer();
            return;
        }
        if (mode === 'steer') {
            status.textContent = `Active steering is unavailable on this route. Draft and attachments retained; choose Queue this message in ${optionsName} for separate work.`;
            onUnavailable(status.textContent);
            return;
        }
        status.textContent = '';
        menu.open = false;
        button.focus();
        onSubmit();
    }
    const onKeyDown = event => {
        if (event.key === 'Escape') { event.preventDefault(); menu.open = false; summary.focus(); }
    };
    menu.addEventListener('keydown', onKeyDown);
    const unsubscribe = subscribeSubmitMode(() => update());
    update();
    return { activate, update, dispose() {
        unsubscribe();
        menu.removeEventListener('keydown', onKeyDown);
        if (optionsPanel) panel.remove(); else menu.remove();
        status.remove();
    } };
}
