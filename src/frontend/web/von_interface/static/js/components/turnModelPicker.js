// In-memory composer choice. Only a submitted turn receives these request fields.
export function createTurnModelPicker({ select, status, clearButton, loadModels, reasoningSelect, loadCapabilities }) {
    let choice = null;
    let parameters = {};
    let loaded = false;
    let loading = false;
    let generation = 0;
    let capabilityGeneration = 0;
    const resetReasoning = (label = 'Choose a model first') => {
        parameters = {};
        if (!reasoningSelect) return;
        reasoningSelect.replaceChildren(new Option(label, ''));
        reasoningSelect.disabled = true;
    };
    const render = () => {
        status.textContent = choice
            ? `Next turn only: ${choice.model} (${choice.model_provider})${parameters.reasoning_effort ? ` · reasoning: ${parameters.reasoning_effort}` : ''}. Default resumes after sending.`
            : '';
        status.hidden = !choice;
        clearButton.hidden = !choice;
        select.value = choice ? JSON.stringify(choice) : '';
    };
    const clear = () => { capabilityGeneration++; choice = null; resetReasoning(); render(); };
    const updateReasoning = async () => {
        const requestGeneration = ++capabilityGeneration;
        resetReasoning(choice ? 'Loading reasoning options…' : undefined);
        if (!choice || !reasoningSelect || !loadCapabilities) return;
        try {
            const result = await loadCapabilities(choice.model_provider, choice.model);
            if (requestGeneration !== capabilityGeneration) return;
            const capability = result?.parameters?.reasoning_effort;
            if (!capability?.supported) {
                resetReasoning('Reasoning level unavailable for this model');
                return;
            }
            const values = [...new Set([...(capability.allowed_values || []), ...(capability.fixed_value ? [capability.fixed_value] : [])])];
            resetReasoning('Provider default');
            values.forEach(value => reasoningSelect.add(new Option(value, value)));
            reasoningSelect.disabled = !!capability.read_only || !!capability.fixed_value || !values.length;
            if (capability.fixed_value) {
                reasoningSelect.value = capability.fixed_value;
                parameters = { reasoning_effort: capability.fixed_value };
            }
            render();
        } catch (_) {
            if (requestGeneration === capabilityGeneration) resetReasoning('Reasoning options unavailable; reselect model to retry');
        }
    };
    select.addEventListener('change', () => {
        choice = select.value ? JSON.parse(select.value) : null;
        void updateReasoning();
        render();
    });
    reasoningSelect?.addEventListener('change', () => {
        parameters = reasoningSelect.value ? { reasoning_effort: reasoningSelect.value } : {};
        render();
    });
    clearButton.addEventListener('click', clear);
    const load = async () => {
        if (loaded || loading) return;
        const requestGeneration = generation;
        loading = true;
        select.disabled = true;
        const failures = [];
        const providers = ['openai', 'gemini', 'openrouter', 'meta', 'ollama'];
        const results = await Promise.allSettled(providers.map(provider => loadModels(provider)));
        if (requestGeneration !== generation) return;
        select.querySelectorAll('optgroup').forEach(group => group.remove());
        results.forEach((result, index) => {
            const provider = providers[index];
            if (result.status !== 'fulfilled' || !Array.isArray(result.value)) {
                failures.push(provider);
                return;
            }
            const group = document.createElement('optgroup');
            group.label = provider;
            result.value.forEach(model => {
                if (typeof model !== 'string' || !model.trim()) return;
                const option = document.createElement('option');
                option.textContent = model;
                option.value = JSON.stringify({ model, model_provider: provider });
                group.append(option);
            });
            if (group.children.length) select.append(group);
        });
        loading = false;
        loaded = failures.length === 0;
        select.disabled = false;
        select.title = failures.length ? `Unavailable: ${failures.join(', ')}. Reopen to retry.` : '';
        if (choice && ![...select.options].some(option => option.value === JSON.stringify(choice))) clear();
        render();
    };
    return {
        load,
        invalidate: () => {
            generation++;
            loading = false;
            loaded = false;
            select.disabled = false;
            select.querySelectorAll('optgroup').forEach(group => group.remove());
            clear();
        },
        peek: () => choice ? { ...choice, model_parameters: { ...parameters } } : null,
        take: () => { const fields = choice ? { ...choice, model_parameters: { ...parameters } } : null; clear(); return fields; },
        clear
    };
}
