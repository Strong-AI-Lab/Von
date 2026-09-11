import { getJson, postJson, ensureUniqueWindowSessionId } from '../apiService.js';
import { getCurrentUserConceptId } from '../domUtils.js';
import { getSessionScopedOrgId } from '../utils/sessionScopedStorage.js';

const identityProfiles = new Map();
document.addEventListener('von:participant-profile-changed', event => {
    identityProfiles.clear();
    document.querySelectorAll('.participant-avatar[data-participant-id]').forEach(el => {
        if (el.dataset.participantId === event.detail?.concept_id) {
            el.replaceWith(participantIdentityAvatar(el.dataset.participantId, event.detail.display_name || el.dataset.displayName));
        }
    });
});

/** A cached identity portrait for contribution headers across conversation kinds. */
export function participantIdentityAvatar(conceptId, displayName) {
    const el = participantAvatar({ display_name: displayName });
    if (!conceptId) return el;
    el.dataset.participantId = conceptId;
    el.dataset.displayName = displayName || '';
    const context = `${getCurrentUserConceptId()}:${getSessionScopedOrgId()}`;
    const key = `${context}:${conceptId}`;
    let cached = identityProfiles.get(key);
    if (!cached || Date.now() - cached.at > 60000) {
        cached = { at: Date.now(), promise: Promise.resolve(getJson(`/von/api/participants/profile?concept_id=${encodeURIComponent(conceptId)}`)) };
        identityProfiles.set(key, cached);
        if (identityProfiles.size > 100) identityProfiles.delete(identityProfiles.keys().next().value);
    }
    void cached.promise.then(({ profile }) => {
        if (!el.isConnected || context !== `${getCurrentUserConceptId()}:${getSessionScopedOrgId()}`) return;
        el.replaceChildren(...participantAvatar(profile).childNodes);
    }).catch(() => {});
    el.title = `${displayName || 'Participant'} profile and avatar`;
    el.setAttribute('role', 'button'); el.tabIndex = 0;
    el.onclick = () => void openParticipantProfile(conceptId);
    el.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); el.click(); } };
    return el;
}

function button(label, action) {
    const el = document.createElement('button');
    el.type = 'button';
    el.textContent = label;
    el.addEventListener('click', action);
    return el;
}

export function participantAvatar(profile = {}) {
    const el = document.createElement('span');
    el.className = 'participant-avatar';
    el.textContent = Array.from(profile.display_name || 'V')[0].toLocaleUpperCase();
    if (profile.avatar_url) {
        const img = document.createElement('img');
        // Image elements cannot carry the normal API window-context header.
        // The server validates this window ID against the authenticated actor.
        void ensureUniqueWindowSessionId().then(id => {
            img.src = `${profile.avatar_url}${profile.avatar_url.includes('?') ? '&' : '?'}window_session_id=${encodeURIComponent(id)}`;
        });
        img.alt = '';
        img.addEventListener('error', () => img.remove(), { once: true });
        el.append(img);
    }
    return el;
}

export function profileButton(conceptId = null, label = 'Profile and avatar') {
    return button(label, () => void openParticipantProfile(conceptId));
}

export async function openParticipantProfile(conceptId = null) {
    document.getElementById('participantProfileDialog')?.remove();
    const dialog = document.createElement('dialog');
    dialog.id = 'participantProfileDialog';
    dialog.className = 'participant-profile-dialog';
    const title = document.createElement('h2');
    title.id = 'participantProfileTitle';
    title.textContent = 'Participant profile';
    dialog.setAttribute('aria-labelledby', title.id);
    const status = document.createElement('p');
    status.setAttribute('role', 'status');
    status.textContent = 'Loading profile…';
    const body = document.createElement('div');
    dialog.append(title, body, status, button('Close', () => dialog.close()));
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
    try {
        let { profile } = await getJson(`/von/api/participants/profile${conceptId ? `?concept_id=${encodeURIComponent(conceptId)}` : ''}`);
        if (!dialog.isConnected) return;
        title.textContent = profile.display_name;
        let avatar = participantAvatar(profile);
        body.append(avatar);
        body.append(button('Open concept details', () => {
            dialog.close();
            (window.parent || window).document.dispatchEvent(new CustomEvent('von:selectConceptById', { detail: { conceptId: profile.concept_id, createConceptTab: true, kind: profile.can_edit ? 'individual' : null, modifierKeys: { shiftKey: true } } }));
        }));
        status.textContent = '';
        if (!profile.can_edit) return;
        const audience = document.createElement('p');
        audience.textContent = 'Generation makes one image request and may incur a provider charge. The preview stays private until you choose Use avatar. Choose who can see this avatar using its scope. More specific avatars take precedence in the matching context. Uploaded images are centred and cropped to a square.';
        const scope = document.createElement('select');
        scope.setAttribute('aria-label', 'Avatar scope');
        const scopeNames = { user_org_default: 'User and organisation', user_only_default: 'User only', organisation_general: 'Organisation', global_general: 'Global' };
        (profile.available_scopes || Object.keys(scopeNames)).forEach(value => { const option = document.createElement('option'); option.value = value; option.textContent = scopeNames[value]; scope.append(option); });
        scope.value = profile.avatar_scope || 'global_general';
        body.append(audience, scope);
        const input = document.createElement('input');
        input.type = 'file';
        input.accept = 'image/png,image/jpeg,image/webp';
        input.setAttribute('aria-label', 'Upload avatar image');
        const preview = document.createElement('img');
        preview.className = 'participant-avatar-preview';
        preview.alt = 'Avatar preview';
        preview.hidden = true;
        const prompt = document.createElement('textarea');
        prompt.placeholder = 'Describe an avatar to generate';
        prompt.setAttribute('aria-label', 'Avatar image description');
        prompt.maxLength = 4000;
        let candidate = null;
        let objectUrl = null;
        let pending = false;
        const run = async (message, action) => {
            if (pending) return;
            pending = true;
            status.textContent = message;
            body.querySelectorAll('button, input, textarea, select').forEach(el => { el.disabled = true; });
            try { await action(); } catch (error) { status.textContent = error.message || 'Could not save the avatar. Try again.'; }
            finally {
                pending = false;
                body.querySelectorAll('button, input, textarea, select').forEach(el => { el.disabled = false; });
                use.disabled = !candidate;
            }
        };
        input.addEventListener('change', () => {
            if (!input.files[0]) return;
            if (objectUrl) URL.revokeObjectURL(objectUrl);
            objectUrl = URL.createObjectURL(input.files[0]);
            candidate = { file: input.files[0] };
            preview.src = objectUrl;
            preview.hidden = false;
            use.disabled = false;
        });
        const generate = button('Generate preview', () => run('Generating an image…', async () => {
            const result = await postJson('/von/api/participants/avatar/generate', { concept_id: profile.concept_id, prompt: prompt.value });
            candidate = { image_concept_id: result.image.concept_id };
            preview.src = result.image.url;
            preview.hidden = false;
            status.textContent = 'Preview ready. Choose Use avatar to publish it.';
        }));
        const saved = result => {
            identityProfiles.clear();
            profile = result.profile;
            const replacement = participantAvatar(profile);
            avatar.replaceWith(replacement);
            avatar = replacement;
            document.dispatchEvent(new CustomEvent('von:participant-profile-changed', { detail: profile }));
            if (window.parent !== window) window.parent.document.dispatchEvent(new CustomEvent('von:participant-profile-changed', { detail: profile }));
            status.textContent = 'Avatar saved.';
        };
        const use = button('Use avatar', () => run('Saving avatar…', async () => {
            if (candidate.file) {
                const form = new FormData();
                form.append('file', candidate.file);
                form.append('concept_id', profile.concept_id);
                form.append('scope', scope.value);
                const response = await fetch('/von/api/participants/avatar', { method: 'POST', headers: { 'X-Von-Window-Session': await ensureUniqueWindowSessionId() }, body: form });
                const result = await response.json();
                if (!response.ok) throw new Error(result.message || 'Upload failed.');
                saved(result);
            } else saved(await postJson('/von/api/participants/avatar', { concept_id: profile.concept_id, scope: scope.value, ...candidate }));
        }));
        use.disabled = true;
        const remove = button('Remove avatar', () => run('Removing avatar…', async () => {
            saved(await postJson('/von/api/participants/avatar', { concept_id: profile.concept_id, scope: scope.value, remove: true }));
            candidate = null;
            preview.hidden = true;
        }));
        body.append(input, prompt, generate, preview, use, remove);
        dialog.addEventListener('close', () => { if (objectUrl) URL.revokeObjectURL(objectUrl); });
    } catch (error) { status.textContent = error.message || 'Profile unavailable.'; }
}
