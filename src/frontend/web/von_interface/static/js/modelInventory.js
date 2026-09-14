// Discovery is evidence of availability, never model-pool authority.
export const TRANSCRIPTION_MODEL_KEY = 'chatRecordedTranscriptionModel';

export function selectedTranscriptionModel() {
    try { return localStorage.getItem(TRANSCRIPTION_MODEL_KEY) || ''; } catch { return ''; }
}

export async function modelSettingsRequest(url, options = {}, timeoutMs = 60000) {
    const controller = new AbortController();
    let timer;
    try {
        return await Promise.race([
            (async () => {
                const response = await fetch(url, { ...options, signal: controller.signal });
                const data = await response.json();
                if (!response.ok) throw new Error(data.message || data.error || `Request failed (${response.status}).`);
                return data;
            })(),
            new Promise((_, reject) => {
                timer = setTimeout(() => {
                    controller.abort();
                    reject(new Error('No response within 60 seconds. The save outcome is unknown; reload the model pool before retrying.'));
                }, timeoutMs);
            }),
        ]);
    } finally {
        clearTimeout(timer);
    }
}

export function renderModelInventory({ inventories, enabled, effectiveEnabled = enabled, roles, allow, busy = false, ready = true }) {
    const list = document.getElementById('additionalModelInventory');
    const selector = document.getElementById('settingsTranscriptionModelSelect');
    if (!list && !selector) return;
    const same = (a, b) => a.provider === b.provider && a.model === b.model
        && (a.host || '') === (b.host || '');
    const models = inventories.flatMap((inventory) => inventory.models || []);
    for (const entry of [...enabled, ...effectiveEnabled]) {
        if (!models.some((candidate) => same(candidate, entry))) models.push({ ...entry, available: false });
    }
    list?.replaceChildren();
    for (const inventory of inventories) {
        if (inventory.error && list) {
            const error = document.createElement('p');
            error.textContent = inventory.error;
            list.append(error);
        }
    }
    for (const entry of models) {
        const allowed = ready && enabled.some((candidate) => same(candidate, entry));
        const assigned = roles.filter((role) => same(role.entry || {}, entry)).map((role) => role.label);
        if (entry.provider === 'openai' && selectedTranscriptionModel() === entry.model) assigned.push('Recorded transcription preference');
        const row = document.createElement('div');
        row.className = 'settings-model-pool-entry';
        const label = document.createElement('span');
        label.className = 'settings-model-pool-entry-label';
        label.textContent = `${entry.provider}: ${entry.model}${entry.host ? ` (${entry.host})` : ''} · ${entry.available ? 'Provider available' : 'Provider/model unavailable'} · ${allowed ? 'Allowed' : 'Not allowed'} · ${assigned.join(', ') || 'No role assigned'}`;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn-secondary';
        button.textContent = allowed ? 'Allowed' : 'Allow and save';
        button.disabled = busy || !ready || allowed || !entry.available;
        button.addEventListener('click', () => allow(entry));
        row.append(label, button);
        list?.append(row);
    }
    if (selector) {
        selector.replaceChildren(new Option('Automatic from enabled transcription models', ''));
        const eligible = models.filter((entry) => entry.audio_transcription === true);
        for (const entry of eligible) {
            const allowed = ready && effectiveEnabled.some((candidate) => same(candidate, entry));
            const option = new Option(`${entry.provider}: ${entry.model} · ${!entry.available ? 'unavailable' : allowed ? 'enabled' : 'enable in model pool'}`, entry.model);
            option.disabled = !entry.available || !allowed;
            selector.append(option);
        }
        const selected = selectedTranscriptionModel();
        if (selected && !eligible.some((entry) => entry.model === selected)) {
            const stale = new Option(`${selected} · unavailable or not eligible; choose another model`, selected);
            stale.disabled = true;
            selector.append(stale);
        }
        selector.value = selected;
        selector.disabled = !ready || busy;
        selector.onchange = () => {
            if (selector.selectedOptions[0]?.disabled) return;
            localStorage.setItem(TRANSCRIPTION_MODEL_KEY, selector.value);
            window.dispatchEvent(new CustomEvent('von-preferences-changed'));
            window.parent?.dispatchEvent(new CustomEvent('von-preferences-changed'));
            renderModelInventory({ inventories, enabled, effectiveEnabled, roles, allow, busy, ready });
        };
    }
}
