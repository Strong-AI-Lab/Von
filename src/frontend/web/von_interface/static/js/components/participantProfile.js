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
        audience.textContent = 'Your source photo and previews stay private. Use avatar publishes only the cropped avatar to the selected scope.';
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
        const dropZone = document.createElement('label');
        dropZone.className = 'participant-avatar-drop';
        dropZone.textContent = 'Drop a PNG, JPEG or WebP photo here, or choose a file (up to 8 MiB).';
        dropZone.append(input);
        const preview = document.createElement('img');
        preview.className = 'participant-avatar-preview';
        preview.alt = 'Avatar preview';
        preview.hidden = true;
        const prompt = document.createElement('textarea');
        prompt.placeholder = 'Describe the desired avatar or photo style, e.g. a watercolour portrait';
        prompt.setAttribute('aria-label', 'Avatar image description');
        prompt.maxLength = 4000;
        const generationNote = document.createElement('p');
        generationNote.textContent = 'Generation sends your source photo and style description to the configured OpenAI image provider. One image request may incur a charge. Preview the result before choosing Use avatar.';
        let candidate = null;
        let source = null;
        let pending = false;
        const cropControls = document.createElement('fieldset');
        cropControls.className = 'participant-avatar-crop';
        cropControls.hidden = true;
        const legend = document.createElement('legend');
        legend.textContent = 'Adjust framing';
        cropControls.append(legend);
        const sliders = {};
        for (const [key, name, min, max, value] of [
            ['zoom', 'Zoom', 1, 8, 1], ['x', 'Horizontal position', 0, 100, 50], ['y', 'Vertical position', 0, 100, 50]
        ]) {
            const label = document.createElement('label');
            label.textContent = name;
            const slider = document.createElement('input');
            slider.type = 'range'; slider.min = min; slider.max = max; slider.step = '0.01'; slider.value = value;
            slider.setAttribute('aria-label', name);
            slider.addEventListener('input', () => renderCrop());
            sliders[key] = slider;
            label.append(slider); cropControls.append(label);
        }
        const renderCrop = () => {
            if (!source) return;
            const { width, height, image, bitmap } = source;
            const size = Math.min(width, height) / Number(sliders.zoom.value);
            const x = (width - size) * Number(sliders.x.value) / 100;
            const y = (height - size) * Number(sliders.y.value) / 100;
            const canvas = document.createElement('canvas');
            canvas.width = canvas.height = 256;
            canvas.getContext('2d').drawImage(bitmap, x, y, size, size, 0, 0, 256, 256);
            preview.src = canvas.toDataURL('image/png');
            preview.hidden = false;
            candidate = { image_concept_id: image.concept_id, crop: { x, y, size } };
            cropControls.hidden = false;
            use.disabled = pending;
        };
        const restoreSource = () => {
            if (!source) return;
            const { width, height, crop } = source;
            sliders.zoom.value = Math.min(width, height) / crop.size;
            const size = Math.min(width, height) / Number(sliders.zoom.value);
            sliders.x.value = width === size ? 50 : crop.x / (width - size) * 100;
            sliders.y.value = height === size ? 50 : crop.y / (height - size) * 100;
            renderCrop();
        };
        const restore = button('Return to source photo', restoreSource);
        restore.hidden = true;
        const run = async (message, action) => {
            if (pending) return;
            pending = true;
            status.textContent = message;
            body.querySelectorAll('button, input, textarea, select').forEach(el => { el.disabled = true; });
            try { await action(); } catch (error) { status.textContent = error.payload?.message || error.message || 'Could not save the avatar. Try again.'; }
            finally {
                pending = false;
                body.querySelectorAll('button, input, textarea, select').forEach(el => { el.disabled = false; });
                use.disabled = !candidate;
            }
        };
        const chooseFile = file => run('Preparing your photo…', async () => {
            if (!file) return;
            if (file.size > 8 * 1024 * 1024 || !/\.(png|jpe?g|webp)$/i.test(file.name)) {
                throw new Error('Choose a still PNG, JPEG or WebP image up to 8 MiB.');
            }
            const form = new FormData();
            form.append('file', file);
            form.append('concept_id', profile.concept_id);
            const response = await fetch('/von/api/participants/avatar/prepare', {
                method: 'POST', headers: { 'X-Von-Window-Session': await ensureUniqueWindowSessionId() }, body: form
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.message || 'Could not prepare this photo.');
            const bitmap = new Image();
            bitmap.src = result.image.url;
            await bitmap.decode();
            if (!dialog.isConnected) return;
            source = { ...result, bitmap };
            restore.hidden = false;
            generate.textContent = 'Generate from photo';
            restoreSource();
            status.textContent = result.face_detection_available === false ? 'Automatic face framing is unavailable. You can still adjust the crop and use your photo.'
                : result.face_count === 1 ? 'Face framed. Adjust the crop if needed, then choose Use avatar.'
                : result.face_count > 1 ? 'Multiple faces found. Adjust the crop to choose the person you want.'
                    : 'No face found. Adjust the crop to frame your photo.';
        });
        input.addEventListener('change', () => void chooseFile(input.files[0]));
        for (const name of ['dragover', 'drop']) dropZone.addEventListener(name, event => {
            event.preventDefault();
            if (name === 'drop' && !pending) {
                if (event.dataTransfer.files.length !== 1) { status.textContent = 'Drop one image at a time.'; return; }
                void chooseFile(event.dataTransfer.files[0]);
            }
        });
        const generate = button('Generate preview', () => run('Generating an image…', async () => {
            const result = await postJson('/von/api/participants/avatar/generate', {
                concept_id: profile.concept_id, prompt: prompt.value,
                ...(source ? { source_image_concept_id: source.image.concept_id } : {})
            });
            candidate = { image_concept_id: result.image.concept_id };
            preview.src = result.image.url;
            preview.hidden = false;
            cropControls.hidden = true;
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
            saved(await postJson('/von/api/participants/avatar', { concept_id: profile.concept_id, scope: scope.value, ...candidate }));
        }));
        use.disabled = true;
        const remove = button('Remove avatar', () => run('Removing avatar…', async () => {
            saved(await postJson('/von/api/participants/avatar', { concept_id: profile.concept_id, scope: scope.value, remove: true }));
            candidate = null;
            preview.hidden = true;
            cropControls.hidden = true;
        }));
        body.append(dropZone, preview, cropControls, restore, prompt, generationNote, generate, use, remove);
    } catch (error) { status.textContent = error.message || 'Profile unavailable.'; }
}
