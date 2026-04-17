/* HermitSDR VFO Widget
 *
 * Interactive frequency display with:
 *   - Clickable digit positions for step-size selection
 *   - Scroll-wheel tuning on VFO and waterfall
 *   - Keyboard tuning (arrow keys, Page Up/Down)
 *   - Band memory (frequency + mode per amateur band)
 *   - Step-size selector bar
 *   - Direct frequency entry
 */
'use strict';

class VFO {
    constructor(containerId, socket) {
        this.container = document.getElementById(containerId);
        this.socket = socket;

        this.frequency = 7074000;
        this.stepHz = 1000;
        this.mode = 'usb';

        // Band definitions: name, edges, default freq, default mode
        this.bands = [
            { name: 'MW',   low: 530000,   high: 1710000,  freq: 1030000,  mode: 'am'  },
            { name: '160m', low: 1800000,  high: 2000000,  freq: 1840000,  mode: 'lsb' },
            { name: '80m',  low: 3500000,  high: 4000000,  freq: 3573000,  mode: 'lsb' },
            { name: '60m',  low: 5330000,  high: 5410000,  freq: 5357000,  mode: 'usb' },
            { name: 'WWV',  low: 4990000,  high: 5010000,  freq: 5000000,  mode: 'am'  },
            { name: '40m',  low: 7000000,  high: 7300000,  freq: 7074000,  mode: 'lsb' },
            { name: '30m',  low: 10100000, high: 10150000, freq: 10136000, mode: 'usb' },
            { name: 'WWV10',low: 9990000,  high: 10010000, freq: 10000000, mode: 'am'  },
            { name: '20m',  low: 14000000, high: 14350000, freq: 14074000, mode: 'usb' },
            { name: '17m',  low: 18068000, high: 18168000, freq: 18100000, mode: 'usb' },
            { name: 'WWV15',low: 14990000, high: 15010000, freq: 15000000, mode: 'am'  },
            { name: '15m',  low: 21000000, high: 21450000, freq: 21074000, mode: 'usb' },
            { name: '12m',  low: 24890000, high: 24990000, freq: 24915000, mode: 'usb' },
            { name: '10m',  low: 28000000, high: 29700000, freq: 28074000, mode: 'usb' },
            { name: '6m',   low: 50000000, high: 54000000, freq: 50313000, mode: 'usb' },
        ];

        // Band memory: stores last-used {freq, mode} per band name
        this.bandMemory = {};
        this.bands.forEach(b => {
            this.bandMemory[b.name] = { freq: b.freq, mode: b.mode };
        });

        // Step sizes available
        this.steps = [
            { hz: 1,      label: '1' },
            { hz: 10,     label: '10' },
            { hz: 100,    label: '100' },
            { hz: 1000,   label: '1k' },
            { hz: 5000,   label: '5k' },
            { hz: 10000,  label: '10k' },
            { hz: 100000, label: '100k' },
        ];

        // Callbacks
        this._onFreqChange = null;
        this._onModeChange = null;

        this._build();
        this._bindEvents();
        this._updateDisplay();
    }

    /** Register callback: fn(freqHz) */
    onFrequencyChange(fn) { this._onFreqChange = fn; }

    /** Register callback: fn(mode) */
    onModeChange(fn) { this._onModeChange = fn; }

    /** Set frequency externally (e.g., from waterfall click) — snaps to step */
    setFrequency(hz) {
        this._setFrequency(hz);
    }

    /** Set exact frequency without step snapping (for direct entry, band recall) */
    setFrequencyExact(hz) {
        hz = this._clamp(hz);
        if (hz === this.frequency) return;
        this.frequency = hz;
        this._saveBandMemory();
        this._updateDisplay();
        this._emitFrequency();
    }

    /** Get current band object (or null if out-of-band) */
    getCurrentBand() {
        return this.bands.find(b =>
            this.frequency >= b.low && this.frequency <= b.high
        ) || null;
    }

    // ── Build DOM ──

    _build() {
        this.container.innerHTML = `
            <div class="vfo-widget" tabindex="0" id="vfo-focus-target">
                <div class="vfo-display" id="vfo-display"></div>
                <div class="vfo-step-bar" id="vfo-steps"></div>
                <div class="vfo-direct">
                    <input type="number" id="vfo-direct-input" class="input-text"
                           min="100000" max="54000000" step="1000"
                           placeholder="Hz" value="${this.frequency}">
                    <span class="unit">Hz</span>
                    <button id="vfo-direct-go" class="btn btn-primary btn-tiny">Set</button>
                </div>
                <div class="vfo-bands" id="vfo-bands"></div>
            </div>
        `;

        this._buildStepBar();
        this._buildBandBar();
    }

    _buildStepBar() {
        const bar = document.getElementById('vfo-steps');
        bar.innerHTML = '<span class="vfo-step-label">Step:</span>' +
            this.steps.map(s =>
                `<button class="btn-step${s.hz === this.stepHz ? ' active' : ''}" data-hz="${s.hz}">${s.label}</button>`
            ).join('');
    }

    _buildBandBar() {
        const bar = document.getElementById('vfo-bands');
        const currentBand = this.getCurrentBand();
        bar.innerHTML = this.bands.map(b =>
            `<button class="btn-band${currentBand && currentBand.name === b.name ? ' active' : ''}" data-band="${b.name}">${b.name}</button>`
        ).join('');
    }

    // ── Display ──

    _updateDisplay() {
        const el = document.getElementById('vfo-display');
        if (!el) return;

        // Format: XX.XXX.XXX with digit spans
        const hz = this.frequency;
        const mhz = Math.floor(hz / 1000000);
        const khz = Math.floor((hz % 1000000) / 1000);
        const ones = hz % 1000;

        const mhzStr = String(mhz).padStart(2, ' ');
        const khzStr = String(khz).padStart(3, '0');
        const onesStr = String(ones).padStart(3, '0');
        const digits = mhzStr + khzStr + onesStr;  // 8 chars

        // Map digit positions to step sizes (index 0 = 10MHz, 7 = 1Hz)
        const digitSteps = [10000000, 1000000, 100000, 10000, 1000, 100, 10, 1];

        let html = '';
        for (let i = 0; i < digits.length; i++) {
            const ch = digits[i];
            const isActive = digitSteps[i] === this.stepHz;
            const cls = 'vfo-digit' + (isActive ? ' vfo-digit-active' : '');
            // Separators after positions 1 (MHz dot) and 4 (kHz dot)
            if (i === 2) html += '<span class="vfo-sep">.</span>';
            if (i === 5) html += '<span class="vfo-sep">.</span>';
            if (ch === ' ') {
                html += `<span class="${cls}" data-step="${digitSteps[i]}">&nbsp;</span>`;
            } else {
                html += `<span class="${cls}" data-step="${digitSteps[i]}">${ch}</span>`;
            }
        }

        html += `<span class="vfo-unit">MHz</span>`;
        el.innerHTML = html;

        // Update direct input
        const input = document.getElementById('vfo-direct-input');
        if (input && document.activeElement !== input) {
            input.value = this.frequency;
        }

        // Update active band highlight
        this._updateBandHighlight();
    }

    _updateBandHighlight() {
        const currentBand = this.getCurrentBand();
        document.querySelectorAll('#vfo-bands .btn-band').forEach(btn => {
            btn.classList.toggle('active', currentBand && btn.dataset.band === currentBand.name);
        });
    }

    // ── Events ──

    _bindEvents() {
        const widget = document.getElementById('vfo-focus-target');

        // Digit click → set step size
        document.getElementById('vfo-display').addEventListener('click', (e) => {
            const digit = e.target.closest('.vfo-digit');
            if (digit && digit.dataset.step) {
                this.stepHz = parseInt(digit.dataset.step);
                this._buildStepBar();
                this._updateDisplay();
            }
        });

        // Step bar clicks
        document.getElementById('vfo-steps').addEventListener('click', (e) => {
            const btn = e.target.closest('.btn-step');
            if (btn) {
                this.stepHz = parseInt(btn.dataset.hz);
                document.querySelectorAll('.btn-step').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this._updateDisplay();
            }
        });

        // Band buttons
        document.getElementById('vfo-bands').addEventListener('click', (e) => {
            const btn = e.target.closest('.btn-band');
            if (btn) this._recallBand(btn.dataset.band);
        });

        // Scroll wheel on VFO
        widget.addEventListener('wheel', (e) => {
            e.preventDefault();
            const dir = e.deltaY < 0 ? 1 : -1;
            this._tune(dir * this.stepHz);
        }, { passive: false });

        // Keyboard tuning (when VFO widget is focused)
        widget.addEventListener('keydown', (e) => {
            switch (e.key) {
                case 'ArrowUp':
                    e.preventDefault();
                    this._tune(this.stepHz);
                    break;
                case 'ArrowDown':
                    e.preventDefault();
                    this._tune(-this.stepHz);
                    break;
                case 'PageUp':
                    e.preventDefault();
                    this._tune(this.stepHz * 10);
                    break;
                case 'PageDown':
                    e.preventDefault();
                    this._tune(-this.stepHz * 10);
                    break;
            }
        });

        // Direct frequency entry — exact, no step snap
        document.getElementById('vfo-direct-go').addEventListener('click', () => {
            const hz = parseInt(document.getElementById('vfo-direct-input').value);
            if (!isNaN(hz)) this.setFrequencyExact(hz);
        });
        document.getElementById('vfo-direct-input').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                const hz = parseInt(e.target.value);
                if (!isNaN(hz)) this.setFrequencyExact(hz);
            }
        });

        // Auto-focus VFO widget on click
        widget.addEventListener('click', () => widget.focus());
    }

    /**
     * Bind scroll-wheel tuning to an external element (e.g., waterfall canvas).
     */
    bindScrollTarget(element) {
        element.addEventListener('wheel', (e) => {
            e.preventDefault();
            const dir = e.deltaY < 0 ? 1 : -1;
            this._tune(dir * this.stepHz);
        }, { passive: false });
    }

    // ── Tuning Logic ──

    _tune(deltaHz) {
        this._setFrequency(this.frequency + deltaHz);
    }

    _setFrequency(hz) {
        hz = this._clamp(hz);
        // Snap to step grid
        hz = Math.round(hz / this.stepHz) * this.stepHz;
        hz = this._clamp(hz);

        if (hz === this.frequency) return;
        this.frequency = hz;

        this._saveBandMemory();
        this._updateDisplay();
        this._emitFrequency();
    }

    _clamp(hz) {
        return Math.max(100000, Math.min(54000000, Math.round(hz)));
    }

    _emitFrequency() {
        console.log(`[VFO] set_frequency → ${this.frequency} Hz (${(this.frequency/1e6).toFixed(6)} MHz)`);
        this.socket.emit('set_frequency', { frequency: this.frequency });
        if (this._onFreqChange) this._onFreqChange(this.frequency);
    }

    _emitMode(mode) {
        this.mode = mode;
        this.socket.emit('set_demod_mode', { mode });
        if (this._onModeChange) this._onModeChange(mode);
        // Update mode buttons in audio panel
        document.querySelectorAll('#mode-buttons .btn-mode').forEach(b => {
            b.classList.toggle('active', b.dataset.mode === mode);
        });
    }

    // ── Band Memory ──

    _saveBandMemory() {
        const band = this.getCurrentBand();
        if (band) {
            this.bandMemory[band.name] = {
                freq: this.frequency,
                mode: this.mode,
            };
        }
    }

    _recallBand(bandName) {
        // Save current band state before leaving
        this._saveBandMemory();

        const mem = this.bandMemory[bandName];
        if (!mem) return;

        // Use exact frequency — don't snap to current step size
        this.frequency = this._clamp(mem.freq);
        this._updateDisplay();
        this._emitFrequency();

        // Switch mode if band memory has a different mode
        if (mem.mode !== this.mode) {
            this._emitMode(mem.mode);
        }
    }

    /**
     * Called when mode changes externally (from audio panel mode buttons).
     * Keeps band memory in sync.
     */
    syncMode(mode) {
        this.mode = mode;
        this._saveBandMemory();
    }
}

window.VFO = VFO;
