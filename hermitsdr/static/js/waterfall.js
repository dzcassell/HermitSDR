/* HermitSDR Waterfall Display
 *
 * Renders GPU-processed spectral data as:
 *   - Spectrum scope (top): real-time power vs frequency
 *   - Scrolling waterfall (bottom): time-frequency heatmap
 *
 * Receives binary SpectralFrame packets over WebSocket.
 */
'use strict';

class WaterfallDisplay {
    constructor(containerId, socket) {
        this.container = document.getElementById(containerId);
        this.socket = socket;

        // Config (synced from server)
        this.centerFreq = 7074000;
        this.bandwidth = 192000;
        this.fftSize = 4096;
        this.dbMin = -140;
        this.dbMax = -20;

        // Color palette (256 RGB entries)
        this.palette = null;
        this.paletteImageData = null;

        // Canvases
        this.specCanvas = null;
        this.specCtx = null;
        this.wfCanvas = null;
        this.wfCtx = null;
        this.axisCanvas = null;
        this.axisCtx = null;

        // Waterfall scroll buffer (offscreen)
        this.wfBuffer = null;
        this.wfBufferCtx = null;
        this.wfLine = 0;

        // Mouse state
        this.mouseX = -1;
        this.mouseFreq = 0;
        this.mouseDb = 0;

        // Stats
        this.frameCount = 0;
        this.fps = 0;
        this.fpsCounter = 0;
        this.fpsTime = performance.now();

        // Current spectral data
        this.currentPower = null;
        this.currentPeak = null;

        // Click-to-tune callback (set externally to route through VFO)
        this.onClickTune = null;

        this._build();
        this._loadPalette('classic');
        this._bindSocket();
        this._startFPSCounter();

        window.addEventListener('resize', () => this._resize());
    }

    _build() {
        this.container.innerHTML = `
            <div class="wf-header">
                <div class="wf-readout">
                    <span id="wf-freq-readout">--- MHz</span>
                    <span id="wf-db-readout">--- dB</span>
                    <span id="wf-fps">0 fps</span>
                </div>
                <div class="wf-controls">
                    <label>FFT
                        <select id="wf-fft-size">
                            <option value="1024">1024</option>
                            <option value="2048">2048</option>
                            <option value="4096" selected>4096</option>
                            <option value="8192">8192</option>
                            <option value="16384">16384</option>
                        </select>
                    </label>
                    <label>Window
                        <select id="wf-window">
                            <option value="blackman_harris" selected>B-H</option>
                            <option value="hann">Hann</option>
                            <option value="hamming">Hamming</option>
                            <option value="flat_top">Flat Top</option>
                            <option value="none">None</option>
                        </select>
                    </label>
                    <label>Avg
                        <input type="range" id="wf-avg" min="0" max="100" value="30" class="slider-sm">
                    </label>
                    <label>Palette
                        <select id="wf-palette">
                            <option value="classic" selected>Classic</option>
                            <option value="thermal">Thermal</option>
                            <option value="neon">Neon</option>
                            <option value="grayscale">Grayscale</option>
                        </select>
                    </label>
                    <label>Peak
                        <input type="checkbox" id="wf-peak">
                    </label>
                </div>
            </div>
            <div class="wf-display">
                <canvas id="wf-spectrum" class="wf-canvas-spectrum"></canvas>
                <canvas id="wf-axis" class="wf-canvas-axis"></canvas>
                <canvas id="wf-waterfall" class="wf-canvas-waterfall"></canvas>
            </div>
            <div class="wf-db-range">
                <label>Floor <input type="number" id="wf-db-min" value="-140" step="5" class="input-text input-sm"></label>
                <label>Ceil <input type="number" id="wf-db-max" value="-20" step="5" class="input-text input-sm"></label>
            </div>
        `;

        this.specCanvas = document.getElementById('wf-spectrum');
        this.specCtx = this.specCanvas.getContext('2d');
        this.wfCanvas = document.getElementById('wf-waterfall');
        this.wfCtx = this.wfCanvas.getContext('2d');
        this.axisCanvas = document.getElementById('wf-axis');
        this.axisCtx = this.axisCanvas.getContext('2d');

        this._resize();
        this._bindControls();
    }

    _resize() {
        const dpr = window.devicePixelRatio || 1;
        const displayW = this.container.querySelector('.wf-display');
        if (!displayW) return;
        const w = displayW.clientWidth;
        if (w < 1) return;  // container not visible yet
        const specH = 150;
        const axisH = 28;
        const wfH = Math.max(200, displayW.clientHeight - specH - axisH);

        for (const [canvas, h] of [[this.specCanvas, specH], [this.axisCanvas, axisH], [this.wfCanvas, wfH]]) {
            canvas.style.width = w + 'px';
            canvas.style.height = h + 'px';
            canvas.width = w * dpr;
            canvas.height = h * dpr;
            canvas.getContext('2d').setTransform(dpr, 0, 0, dpr, 0, 0);
        }

        // Rebuild waterfall scroll buffer
        this.wfBuffer = document.createElement('canvas');
        this.wfBuffer.width = w;
        this.wfBuffer.height = wfH;
        this.wfBufferCtx = this.wfBuffer.getContext('2d', { willReadFrequently: true });
        this.wfLine = 0;

        this._drawAxis();
    }

    _bindControls() {
        const post = (data) => fetch('/api/dsp', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });

        document.getElementById('wf-fft-size').addEventListener('change', (e) => {
            this.fftSize = parseInt(e.target.value);
            post({ fft_size: this.fftSize });
        });

        document.getElementById('wf-window').addEventListener('change', (e) => {
            post({ window: e.target.value });
        });

        document.getElementById('wf-avg').addEventListener('input', (e) => {
            post({ averaging: parseInt(e.target.value) / 100 });
        });

        document.getElementById('wf-palette').addEventListener('change', (e) => {
            this._loadPalette(e.target.value);
        });

        document.getElementById('wf-peak').addEventListener('change', (e) => {
            post({ peak_hold: e.target.checked });
        });

        document.getElementById('wf-db-min').addEventListener('change', (e) => {
            this.dbMin = parseFloat(e.target.value);
            post({ db_min: this.dbMin });
        });

        document.getElementById('wf-db-max').addEventListener('change', (e) => {
            this.dbMax = parseFloat(e.target.value);
            post({ db_max: this.dbMax });
        });

        // Mouse tracking on spectrum canvas
        this.specCanvas.addEventListener('mousemove', (e) => {
            const rect = this.specCanvas.getBoundingClientRect();
            this.mouseX = e.clientX - rect.left;
            const w = rect.width;
            const freqOffset = (this.mouseX / w - 0.5) * this.bandwidth;
            this.mouseFreq = this.centerFreq + freqOffset;
            // Approximate dB from Y position
            const y = e.clientY - rect.top;
            this.mouseDb = this.dbMax - (y / rect.height) * (this.dbMax - this.dbMin);
            document.getElementById('wf-freq-readout').textContent = this._formatFreq(this.mouseFreq);
            document.getElementById('wf-db-readout').textContent = this.mouseDb.toFixed(1) + ' dB';
        });

        this.specCanvas.addEventListener('mouseleave', () => { this.mouseX = -1; });

        // Click to tune
        this.specCanvas.addEventListener('click', (e) => {
            const rect = this.specCanvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const freqOffset = (x / rect.width - 0.5) * this.bandwidth;
            const newFreq = Math.round(this.centerFreq + freqOffset);
            if (this.onClickTune) {
                this.onClickTune(newFreq);
            } else {
                this.socket.emit('set_frequency', { frequency: newFreq });
            }
        });
    }

    _bindSocket() {
        this.socket.on('spectral_frame', (data) => {
            this._parseFrame(data);
            this.fpsCounter++;
        });
    }

    _parseFrame(buffer) {
        // Handle both ArrayBuffer and Blob
        if (buffer instanceof Blob) {
            buffer.arrayBuffer().then(ab => this._processBuffer(ab));
        } else if (buffer instanceof ArrayBuffer) {
            this._processBuffer(buffer);
        } else if (buffer.buffer) {
            this._processBuffer(buffer.buffer);
        }
    }

    _processBuffer(ab) {
        const view = new DataView(ab);
        // Header: magic(4) + frame(4) + center(4) + bw(4) + fftsize(2) + flags(2) + time(4) = 24 bytes
        if (ab.byteLength < 24) return;

        const frameNum = view.getUint32(4, true);
        const newCenter = view.getUint32(8, true);
        const newBw = view.getUint32(12, true);
        // Redraw axis if center or bandwidth changed (e.g., user retuned)
        const axisChanged = (newCenter !== this.centerFreq || newBw !== this.bandwidth);
        this.centerFreq = newCenter;
        this.bandwidth = newBw;
        this.fftSize = view.getUint16(16, true);
        const flags = view.getUint16(18, true);
        const hasPeak = flags & 0x01;

        // Power data starts at offset 24
        const numBins = this.fftSize;
        const powerBytes = numBins * 4;
        if (ab.byteLength < 24 + powerBytes) return;

        this.currentPower = new Float32Array(ab, 24, numBins);

        if (hasPeak && ab.byteLength >= 24 + powerBytes * 2) {
            this.currentPeak = new Float32Array(ab, 24 + powerBytes, numBins);
        } else {
            this.currentPeak = null;
        }

        this.frameCount = frameNum;
        if (axisChanged) {
            this._drawAxis();
            // Clear waterfall buffer — old content was at a different frequency
            // and would mislead the eye if left behind
            if (this.wfBufferCtx && this.wfBuffer) {
                this.wfBufferCtx.fillStyle = '#000';
                this.wfBufferCtx.fillRect(0, 0, this.wfBuffer.width, this.wfBuffer.height);
            }
        }
        this._drawSpectrum();
        this._drawWaterfallLine();
    }

    _drawSpectrum() {
        if (!this.currentPower || !this.palette) return;
        const w = this.specCanvas.clientWidth;
        const h = this.specCanvas.clientHeight;
        const ctx = this.specCtx;
        const power = this.currentPower;
        const numBins = power.length;
        const dbRange = this.dbMax - this.dbMin;

        ctx.clearRect(0, 0, w, h);

        // Background
        ctx.fillStyle = '#0a0e14';
        ctx.fillRect(0, 0, w, h);

        // Grid lines (every 20 dB)
        ctx.strokeStyle = '#1a2230';
        ctx.lineWidth = 0.5;
        for (let db = Math.ceil(this.dbMin / 20) * 20; db <= this.dbMax; db += 20) {
            const y = h - ((db - this.dbMin) / dbRange) * h;
            ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
            ctx.fillStyle = '#3a4a5a';
            ctx.font = '9px monospace';
            ctx.fillText(db + ' dB', 3, y - 2);
        }

        // Spectrum trace (filled + line)
        const step = w / numBins;

        // Fill
        ctx.beginPath();
        ctx.moveTo(0, h);
        for (let i = 0; i < numBins; i++) {
            const db = Math.max(this.dbMin, Math.min(this.dbMax, power[i]));
            const y = h - ((db - this.dbMin) / dbRange) * h;
            ctx.lineTo(i * step, y);
        }
        ctx.lineTo(w, h);
        ctx.closePath();
        const grad = ctx.createLinearGradient(0, 0, 0, h);
        grad.addColorStop(0, 'rgba(0, 204, 136, 0.25)');
        grad.addColorStop(1, 'rgba(0, 204, 136, 0.02)');
        ctx.fillStyle = grad;
        ctx.fill();

        // Line
        ctx.beginPath();
        for (let i = 0; i < numBins; i++) {
            const db = Math.max(this.dbMin, Math.min(this.dbMax, power[i]));
            const x = i * step;
            const y = h - ((db - this.dbMin) / dbRange) * h;
            i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
        }
        ctx.strokeStyle = '#00cc88';
        ctx.lineWidth = 1;
        ctx.stroke();

        // Peak trace
        if (this.currentPeak) {
            ctx.beginPath();
            for (let i = 0; i < numBins; i++) {
                const db = Math.max(this.dbMin, Math.min(this.dbMax, this.currentPeak[i]));
                const x = i * step;
                const y = h - ((db - this.dbMin) / dbRange) * h;
                i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
            }
            ctx.strokeStyle = 'rgba(255, 136, 68, 0.6)';
            ctx.lineWidth = 0.8;
            ctx.stroke();
        }

        // Center frequency marker
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.15)';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(w / 2, 0);
        ctx.lineTo(w / 2, h);
        ctx.stroke();
        ctx.setLineDash([]);

        // Mouse cursor crosshair
        if (this.mouseX >= 0) {
            ctx.strokeStyle = 'rgba(255, 255, 255, 0.3)';
            ctx.lineWidth = 0.5;
            ctx.beginPath();
            ctx.moveTo(this.mouseX, 0);
            ctx.lineTo(this.mouseX, h);
            ctx.stroke();
        }
    }

    _drawWaterfallLine() {
        if (!this.currentPower || !this.palette || !this.wfBuffer) return;
        const power = this.currentPower;
        const numBins = power.length;
        const w = this.wfBuffer.width;
        const h = this.wfBuffer.height;
        if (w < 1 || h < 2) return;  // canvas not laid out yet
        const dbRange = this.dbMax - this.dbMin;

        // Scroll existing waterfall down by 1 pixel
        const existing = this.wfBufferCtx.getImageData(0, 0, w, h - 1);
        this.wfBufferCtx.putImageData(existing, 0, 1);

        // Draw new line at top
        const lineData = this.wfBufferCtx.createImageData(w, 1);
        const pixels = lineData.data;

        for (let x = 0; x < w; x++) {
            // Map pixel X to FFT bin
            const bin = Math.floor((x / w) * numBins);
            const db = power[Math.min(bin, numBins - 1)];

            // Map dB to palette index (0..255)
            let idx = Math.floor(((db - this.dbMin) / dbRange) * 255);
            idx = Math.max(0, Math.min(255, idx));

            const color = this.palette[idx];
            const px = x * 4;
            pixels[px] = color[0];
            pixels[px + 1] = color[1];
            pixels[px + 2] = color[2];
            pixels[px + 3] = 255;
        }

        this.wfBufferCtx.putImageData(lineData, 0, 0);

        // Copy buffer to display canvas
        this.wfCtx.drawImage(this.wfBuffer, 0, 0,
            this.wfCanvas.clientWidth, this.wfCanvas.clientHeight);
    }

    _drawAxis() {
        const w = this.axisCanvas.clientWidth;
        const h = this.axisCanvas.clientHeight;
        const ctx = this.axisCtx;

        ctx.clearRect(0, 0, w, h);
        ctx.fillStyle = '#111820';
        ctx.fillRect(0, 0, w, h);

        if (!this.bandwidth) return;

        ctx.fillStyle = '#6b7a8d';
        ctx.font = '10px monospace';
        ctx.textAlign = 'center';

        const startFreq = this.centerFreq - this.bandwidth / 2;
        const endFreq = this.centerFreq + this.bandwidth / 2;

        // Pick nice tick spacing
        const tickOptions = [1000, 2000, 5000, 10000, 20000, 25000, 50000];
        const desiredTicks = w / 80;
        let tickSpacing = tickOptions[0];
        for (const t of tickOptions) {
            if (this.bandwidth / t <= desiredTicks) { tickSpacing = t; break; }
        }

        const firstTick = Math.ceil(startFreq / tickSpacing) * tickSpacing;
        for (let freq = firstTick; freq <= endFreq; freq += tickSpacing) {
            const x = ((freq - startFreq) / this.bandwidth) * w;
            ctx.strokeStyle = '#2a3a4a';
            ctx.lineWidth = 0.5;
            ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, 5); ctx.stroke();

            const label = (freq / 1000).toFixed(freq % 1000 === 0 ? 0 : 1);
            ctx.fillText(label, x, 18);
        }

        // Center marker
        ctx.strokeStyle = '#00cc8866';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(w / 2, 0);
        ctx.lineTo(w / 2, h);
        ctx.stroke();
    }

    async _loadPalette(name) {
        try {
            const res = await fetch(`/api/palette?name=${name}`);
            const data = await res.json();
            this.palette = data.colors;
        } catch (e) {
            // Fallback: generate simple grayscale
            this.palette = Array.from({length: 256}, (_, i) => [i, i, i]);
        }
    }

    _formatFreq(hz) {
        const mhz = hz / 1000000;
        return mhz.toFixed(3) + ' MHz';
    }

    _startFPSCounter() {
        setInterval(() => {
            const now = performance.now();
            const elapsed = (now - this.fpsTime) / 1000;
            this.fps = Math.round(this.fpsCounter / elapsed);
            this.fpsCounter = 0;
            this.fpsTime = now;
            const el = document.getElementById('wf-fps');
            if (el) el.textContent = this.fps + ' fps';
        }, 1000);
    }

    // Public: call when frequency changes externally
    updateCenterFreq(freq) {
        this.centerFreq = freq;
        this._drawAxis();
    }
}

// Export for use in main JS
window.WaterfallDisplay = WaterfallDisplay;
