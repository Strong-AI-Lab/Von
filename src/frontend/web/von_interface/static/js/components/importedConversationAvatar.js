const PROVIDERS = {
    codex: { label: 'Codex', file: 'codex.png' },
    claude_code: { label: 'Claude Code', file: 'claude.svg' },
    copilot: { label: 'GitHub Copilot', file: 'copilot.svg' },
    gemini: { label: 'Gemini', file: 'gemini.svg' }
};

// Provider branding describes the imported source, not a verified Von identity.
export function importedConversationAvatar(providerOrActor, displayName, { sidebar = false } = {}) {
    const provider = String(providerOrActor || '').split(':')[0];
    const brand = Object.hasOwn(PROVIDERS, provider) ? PROVIDERS[provider] : null;
    const avatar = document.createElement('span');
    avatar.className = sidebar ? 'participant-avatar imported-provider-avatar' : 'chat-imported-actor-avatar imported-provider-avatar';
    avatar.setAttribute('role', 'img');
    avatar.setAttribute('aria-label', `${brand?.label || displayName || 'Agent'} (imported)`);
    avatar.title = `${brand?.label || displayName || 'Agent'} · Imported conversation`;
    avatar.textContent = String(displayName || brand?.label || 'Agent').trim().slice(0, 1).toUpperCase();
    if (brand) {
        const image = document.createElement('img');
        image.src = `/static/images/providers/${brand.file}`;
        image.alt = '';
        image.addEventListener('load', () => { avatar.classList.add('has-provider-logo'); });
        image.addEventListener('error', () => { image.remove(); avatar.classList.remove('has-provider-logo'); });
        avatar.append(image);
    }
    return avatar;
}
