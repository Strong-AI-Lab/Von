function normaliseJsonFilename(name) {
    const raw = typeof name === 'string' ? name.trim() : '';
    if (!raw) {
        return 'export.json';
    }
    return raw.toLowerCase().endsWith('.json') ? raw : `${raw}.json`;
}

export function isJSDOMEnvironment() {
    try {
        return typeof navigator !== 'undefined' && /jsdom/i.test(navigator.userAgent || '');
    } catch (_) {
        return false;
    }
}

export function triggerBlobDownload(blob, filename) {
    if (!(blob instanceof Blob)) {
        throw new TypeError('triggerBlobDownload expects a Blob instance.');
    }

    const urlApi = typeof window !== 'undefined' ? window.URL : null;
    if (!urlApi || typeof urlApi.createObjectURL !== 'function') {
        throw new Error('Blob download is not supported in this browser environment.');
    }

    const url = urlApi.createObjectURL(blob);
    try {
        if (!isJSDOMEnvironment()) {
            const anchor = document.createElement('a');
            anchor.href = url;
            if (filename) {
                anchor.download = filename;
            }
            document.body.appendChild(anchor);
            anchor.click();
            document.body.removeChild(anchor);
        }
    } finally {
        if (typeof urlApi.revokeObjectURL === 'function') {
            urlApi.revokeObjectURL(url);
        }
    }
}

export async function saveJsonTextViaDialog(jsonText, options = {}) {
    const text = typeof jsonText === 'string' ? jsonText : JSON.stringify(jsonText ?? null, null, 2);
    const suggestedName = normaliseJsonFilename(options?.suggestedName);
    const filePicker = (typeof window !== 'undefined' && typeof window.showSaveFilePicker === 'function')
        ? window.showSaveFilePicker.bind(window)
        : null;

    if (filePicker) {
        try {
            const handle = await filePicker({
                suggestedName,
                types: [
                    {
                        description: 'JSON file',
                        accept: { 'application/json': ['.json'] }
                    }
                ]
            });
            const writable = await handle.createWritable();
            await writable.write(text);
            await writable.close();
            return { saved: true, cancelled: false, method: 'file-picker', filename: suggestedName };
        } catch (error) {
            if (error?.name === 'AbortError') {
                return { saved: false, cancelled: true, method: 'file-picker', filename: suggestedName };
            }
            console.warn('[fileSave] Save picker failed; using download fallback.', error);
        }
    }

    try {
        const blob = new Blob([text], { type: 'application/json' });
        triggerBlobDownload(blob, suggestedName);
        return { saved: true, cancelled: false, method: 'download', filename: suggestedName };
    } catch (error) {
        return { saved: false, cancelled: false, method: 'download', filename: suggestedName, error };
    }
}
