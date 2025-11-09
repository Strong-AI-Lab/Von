// Phase-based progress manager for Vontology tree loading
// Exported for testing

export const ProgressPhase = Object.freeze({
    FETCH: 'fetch',
    PROCESS: 'process',
    RENDER: 'render',
    COMPLETE: 'complete'
});

const PHASE_RANGES = {
    [ProgressPhase.FETCH]: [0, 50],
    [ProgressPhase.PROCESS]: [50, 85],
    [ProgressPhase.RENDER]: [85, 99],
    [ProgressPhase.COMPLETE]: [100, 100]
};

export function computePhasePercent(phase, phaseProgressFraction) {
    if (phase === ProgressPhase.COMPLETE) return 100;
    const frac = Math.max(0, Math.min(1, phaseProgressFraction == null ? 0 : phaseProgressFraction));
    const range = PHASE_RANGES[phase] || [0, 0];
    const [start, end] = range;
    return Math.round(start + (end - start) * frac);
}

export class ProgressManager {
    constructor(onUpdate) {
        this.onUpdate = typeof onUpdate === 'function' ? onUpdate : () => { };
        this.currentPhase = ProgressPhase.FETCH;
        this.lastPercent = 0;
    }

    setPhase(phase) {
        if (!PHASE_RANGES[phase]) return;
        this.currentPhase = phase;
        // Emit minimum of phase start to avoid visual backward jumps
        const pct = this._phaseBasePercent();
        this._emit(Math.max(this.lastPercent, pct), `${this._labelForPhase()}…`);
    }

    updateProgress(fraction, extraLabel = '') {
        const pct = computePhasePercent(this.currentPhase, fraction);
        const safePct = Math.max(this.lastPercent, pct); // enforce monotonic
        const label = `${this._labelForPhase()}${extraLabel ? ' ' + extraLabel : ''}`;
        this._emit(safePct, label);
    }

    complete(summary = '') {
        this.currentPhase = ProgressPhase.COMPLETE;
        this._emit(100, `Complete${summary ? ' ' + summary : ''}`);
    }

    _phaseBasePercent() {
        const [start] = PHASE_RANGES[this.currentPhase] || [0, 0];
        return start;
    }

    _labelForPhase() {
        switch (this.currentPhase) {
            case ProgressPhase.FETCH: return 'Contacting backend';
            case ProgressPhase.PROCESS: return 'Processing ontology';
            case ProgressPhase.RENDER: return 'Rendering tree';
            case ProgressPhase.COMPLETE: return 'Complete';
            default: return 'Working';
        }
    }

    _emit(pct, text) {
        if (pct > this.lastPercent) this.lastPercent = pct;
        this.onUpdate(pct, text);
    }
}
