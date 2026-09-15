// In-memory composer choice. Only a submitted turn receives these request fields.
export function createTurnModelPicker({ select, status, clearButton, loadModels }) {
    let choice = null;
    let loaded = false;
    let loading = false;
    const render = () => {
        status.textContent = choice
            ? `Next turn only: ${choice.model} (${choice.model_provider}). Default resumes after sending.`
            : '';
        status.hidden = !choice;
        clearButton.hidden = !choice;
        select.value = choice ? JSON.stringify(choice) : '';
    };
    const clear = () => { choice = null; render(); };
    select.addEventListener('change', () => {
        choice = select.value ? JSON.parse(select.value) : null;
        render();
    });
    clearButton.addEventListener('click', clear);
    const load = async () => {
        if (loaded || loading) return;
        loading = true;
        select.disabled = true;
        const failures = [];
        const providers = ['openai', 'gemini', 'openrouter', 'meta', 'ollama'];
        const results = await Promise.allSettled(providers.map(provider => loadModels(provider)));
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
        render();
    };
    return {
        load: async () => {
            // Retry unavailable catalogues without duplicating successful options.
            if (!loaded && !loading) select.querySelectorAll('optgroup').forEach(group => group.remove());
            await load();
        },
        peek: () => choice ? { ...choice } : null,
        take: () => { const fields = choice ? { ...choice, model_parameters: {} } : null; clear(); return fields; },
        clear
    };
}
