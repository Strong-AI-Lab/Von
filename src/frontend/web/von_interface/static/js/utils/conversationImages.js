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
        if ([...parent.querySelectorAll('[data-image-id]')].some(node => node.dataset.imageId === item.concept_id)) continue;
        const figure = document.createElement('figure');
        figure.dataset.imageId = item.concept_id;
        figure.style.cssText = 'margin:0;max-width:100%;min-width:0';
        const url = `/von/api/images/${encodeURIComponent(item.concept_id)}/original`;
        const link = document.createElement('a');
        link.href = url; link.target = '_blank'; link.rel = 'noopener';
        const img = document.createElement('img');
        img.src = url; img.alt = item.caption || item.alt || (item.provenance?.kind === 'generated' ? 'Generated image' : item.filename || 'Attached image');
        img.style.cssText = 'max-width:min(100%,640px);max-height:420px;height:auto;object-fit:contain';
        img.addEventListener('error', () => {
            const message = 'Image unavailable or access denied. Open the original to inspect the error.';
            img.alt = message;
            if (!figure.querySelector('.image-unavailable')) {
                const notice = document.createElement('p');
                notice.className = 'image-unavailable'; notice.setAttribute('role', 'status');
                notice.textContent = message; figure.appendChild(notice);
            }
        });
        link.setAttribute('aria-label', `Open original: ${img.alt}`);
        link.appendChild(img); figure.appendChild(link); gallery.appendChild(figure);
        const caption = document.createElement('figcaption');
        const generated = item.provenance?.kind === 'generated';
        caption.textContent = item.caption || (generated ? 'Generated image' : '');
        if (item.provenance?.parent_concept_ids?.length || item.provenance?.parent_concept_id) {
            const original = document.createElement('a');
            const id = item.provenance.parent_concept_id || item.provenance.parent_concept_ids[0];
            original.href = `/von/api/images/${encodeURIComponent(id)}/original`;
            original.target = '_blank'; original.rel = 'noopener'; original.textContent = 'View source image';
            caption.append(' · ', original);
        }
        figure.appendChild(caption);
        const source = item.provenance;
        if (source?.kind === 'otter_archive' && source.artifact_id) {
            const citation = document.createElement('a');
            citation.href = `/von/api/otter-archive/artifacts/${encodeURIComponent(source.artifact_id)}`;
            citation.target = '_blank'; citation.rel = 'noopener';
            citation.textContent = 'View archive source and extraction provenance';
            gallery.appendChild(citation);
        }
    }
    if (gallery.childNodes.length) parent.appendChild(gallery);
}
export function renderImageComposer(parent, sessionId, changed) {
    if (!parent) return;
    parent.replaceChildren();
    for (const item of imageItems(sessionId)) {
        const card = document.createElement('div');
        card.style.cssText = 'display:inline-flex;flex-direction:column;gap:4px;margin:6px;max-width:180px';
        if (item.descriptor) renderImageAttachments(card, [item.descriptor]);
        const label = document.createElement('span');
        label.textContent = item.error || (item.descriptor ? item.name : `Uploading ${item.name}…`);
        label.setAttribute('role', item.error ? 'alert' : 'status');
        card.appendChild(label);
        const remove = document.createElement('button');
        remove.type = 'button'; remove.textContent = 'Remove'; remove.setAttribute('aria-label', `Remove ${item.name}`);
        remove.onclick = () => { pending.set(sessionId, imageItems(sessionId).filter(x => x !== item)); changed(); };
        card.appendChild(remove); parent.appendChild(card);
    }
}
export async function uploadConversationImage(file, sessionId, headers, changed) {
    const item = {name: file.name};
    pending.set(sessionId, [...imageItems(sessionId), item]); changed();
    try {
        if (imageItems(sessionId).length > 8) throw new Error('At most eight images per message. Remove an image before sending.');
        const form = new FormData(); form.append('file', file, file.name || 'image.png');
        const response = await fetch('/von/api/images/upload', {method:'POST', headers, body:form});
        const result = await response.json();
        if (!response.ok || !result.image_attachment) throw new Error(result.message || result.error || 'Image upload failed');
        item.descriptor = result.image_attachment; known.set(item.descriptor.concept_id, item);
    } catch (error) { item.error = String(error.message || error); }
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
