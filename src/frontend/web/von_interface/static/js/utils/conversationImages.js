// Durable image descriptors only; originals remain behind authenticated routes.
const pending = new Map();
const known = new Map();
export function imageItems(sessionId) { return pending.get(sessionId) || []; }
export function imagesBlocked(sessionId) { return imageItems(sessionId).some(x => !x.descriptor); }
export function renderImageAttachments(parent, images) {
    if (!parent || !Array.isArray(images) || !images.length) return;
    const gallery = document.createElement('div');
    gallery.className = 'conversation-image-gallery';
    gallery.style.cssText = 'display:flex;flex-wrap:wrap;gap:12px;margin:8px 0';
    for (const item of images) {
        if (!item?.concept_id) continue;
        const url = `/von/api/images/${encodeURIComponent(item.concept_id)}/original`;
        const link = document.createElement('a');
        link.href = url; link.target = '_blank'; link.rel = 'noopener';
        const img = document.createElement('img');
        img.src = url; img.alt = item.filename || 'Attached image';
        img.style.cssText = 'max-width:min(100%,640px);max-height:420px;object-fit:contain';
        img.addEventListener('error', () => { img.alt = 'Image unavailable or access denied. Open the original to inspect the error.'; });
        link.appendChild(img); gallery.appendChild(link);
        const source = item.provenance;
        if (source?.kind === 'otter_archive' && source.artifact_id) {
            const citation = document.createElement('a');
            citation.href = `/von/api/otter-archive/artifacts/${encodeURIComponent(source.artifact_id)}`;
            citation.target = '_blank'; citation.rel = 'noopener';
            citation.textContent = 'View archive source and extraction provenance';
            gallery.appendChild(citation);
        }
    }
    parent.appendChild(gallery);
}
export function renderImageComposer(parent, sessionId, changed) {
    if (!parent) return;
    parent.replaceChildren();
    for (const item of imageItems(sessionId)) {
        const card = document.createElement('div');
        card.style.cssText = 'display:inline-flex;flex-direction:column;gap:4px;margin:6px;max-width:180px;min-width:0;overflow-wrap:anywhere';
        if (item.descriptor) renderImageAttachments(card, [item.descriptor]);
        else if (item.previewUrl) {
            const preview = document.createElement('img');
            preview.src = item.previewUrl;
            preview.alt = item.name;
            preview.style.cssText = 'max-width:100%;max-height:140px;object-fit:contain';
            card.appendChild(preview);
        }
        const label = document.createElement('span');
        label.textContent = item.error ? `${item.name}: ${item.error}` : (item.descriptor ? item.name : `Uploading ${item.name}…`);
        label.setAttribute('role', item.error ? 'alert' : 'status');
        card.appendChild(label);
        const details = document.createElement('span');
        details.textContent = `${item.type || 'Unknown type'} · ${item.size.toLocaleString()} bytes`;
        card.appendChild(details);
        if (item.error) {
            const retry = document.createElement('button');
            retry.type = 'button'; retry.textContent = 'Retry upload';
            retry.setAttribute('aria-label', `Retry upload of ${item.name}`);
            retry.onclick = () => { void item.retry(); };
            card.appendChild(retry);
        }
        const remove = document.createElement('button');
        remove.type = 'button'; remove.textContent = 'Remove'; remove.setAttribute('aria-label', `Remove ${item.name}`);
        remove.onclick = () => {
            pending.set(sessionId, imageItems(sessionId).filter(x => x !== item));
            releasePreview(item);
            changed();
        };
        card.appendChild(remove); parent.appendChild(card);
    }
}
export async function uploadConversationImage(file, sessionId, headers, changed) {
    const item = {
        name: file.name, type: file.type, size: file.size,
        previewUrl: typeof URL.createObjectURL === 'function' ? URL.createObjectURL(file) : null,
    };
    item.retry = () => uploadItem(item, file, sessionId, headers, changed);
    pending.set(sessionId, [...imageItems(sessionId), item]); changed();
    return item.retry();
}
function releasePreview(item) {
    if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
    item.previewUrl = null;
}
async function uploadItem(item, file, sessionId, headers, changed) {
    if (item.uploading || !imageItems(sessionId).includes(item)) return false;
    item.uploading = true;
    item.error = null;
    changed();
    try {
        if (imageItems(sessionId).length > 8) throw new Error('At most eight images per message. Remove an image before sending.');
        const form = new FormData(); form.append('file', file, file.name || 'image.png');
        const response = await fetch('/von/api/images/upload', {method:'POST', headers, body:form});
        const result = await response.json();
        if (!response.ok || !result.image_attachment) throw new Error(result.message || result.error || 'Image upload failed');
        // Removing an in-flight upload must not restore it when the response arrives.
        if (imageItems(sessionId).includes(item)) {
            item.descriptor = result.image_attachment;
            item.retry = () => Promise.resolve(true);
            known.set(item.descriptor.concept_id, item);
        }
        releasePreview(item);
    } catch (error) { item.error = String(error.message || error); }
    item.uploading = false;
    changed();
    return Boolean(item.descriptor);
}
export function takeImages(sessionId) {
    if (imagesBlocked(sessionId)) throw new Error('Image preparation failed or is still running. Remove or finish the upload before sending.');
    const ids = imageItems(sessionId).map(x => x.descriptor.concept_id);
    pending.delete(sessionId); return ids;
}
export function restoreImages(sessionId, ids) {
    const existing = imageItems(sessionId);
    for (const id of ids || []) {
        const item = known.get(id);
        if (item && !existing.includes(item)) existing.push(item);
    }
    pending.set(sessionId, existing);
}
export function descriptorsForIds(ids) { return (ids || []).map(id => known.get(id)?.descriptor).filter(Boolean); }
