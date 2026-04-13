/* HermitSDR Audio Player
 *
 * Plays streaming float32 PCM audio from the demodulator via Web Audio API.
 * Uses scheduled AudioBufferSourceNodes for gapless, low-latency playback.
 *
 * Architecture:
 *   SocketIO binary → Float32Array → AudioBuffer → scheduled source node
 *   Each chunk (~21ms at 48kHz/1024 samples) is queued with sample-accurate timing.
 */
'use strict';

class AudioPlayer {
    constructor(socket) {
        this.socket = socket;
        this.ctx = null;         // AudioContext (created on user gesture)
        this.gainNode = null;
        this.analyser = null;

        this.sampleRate = 48000;
        this.chunkSamples = 1024;

        // Scheduling state
        this.nextStartTime = 0;
        this.isPlaying = false;
        this.chunksPlayed = 0;
        this.chunksDropped = 0;
        this.bufferDepth = 0;    // scheduled but not yet played

        // Buffer queue for chunks that arrive before we're ready
        this._queue = [];
        this._maxQueue = 20;     // ~420ms of audio

        // Level metering
        this.audioLevel = -160;
        this.squelched = false;

        this._bindSocket();
    }

    /**
     * Initialize AudioContext. Must be called from a user gesture (click/tap)
     * due to browser autoplay policies.
     */
    init() {
        if (this.ctx) return;

        this.ctx = new (window.AudioContext || window.webkitAudioContext)({
            sampleRate: this.sampleRate,
            latencyHint: 'interactive',
        });

        this.gainNode = this.ctx.createGain();
        this.gainNode.gain.value = 1.0;

        // Analyser for VU meter
        this.analyser = this.ctx.createAnalyser();
        this.analyser.fftSize = 256;
        this.analyser.smoothingTimeConstant = 0.8;

        this.gainNode.connect(this.analyser);
        this.analyser.connect(this.ctx.destination);

        this.nextStartTime = this.ctx.currentTime;
        this.isPlaying = true;

        console.log('[AudioPlayer] initialized, sampleRate=' + this.ctx.sampleRate);
    }

    /**
     * Set output volume (0..1).
     */
    setVolume(vol) {
        if (this.gainNode) {
            this.gainNode.gain.setTargetAtTime(
                Math.max(0, Math.min(1, vol)),
                this.ctx.currentTime,
                0.02  // 20ms smoothing
            );
        }
    }

    /**
     * Mute/unmute.
     */
    setMute(muted) {
        if (this.gainNode) {
            this.gainNode.gain.setTargetAtTime(
                muted ? 0 : 1, this.ctx.currentTime, 0.01
            );
        }
    }

    /**
     * Get current VU meter level (0..1).
     */
    getLevel() {
        if (!this.analyser) return 0;
        const data = new Uint8Array(this.analyser.frequencyBinCount);
        this.analyser.getByteTimeDomainData(data);
        let peak = 0;
        for (let i = 0; i < data.length; i++) {
            const v = Math.abs(data[i] - 128) / 128;
            if (v > peak) peak = v;
        }
        return peak;
    }

    /**
     * Stop playback and close AudioContext.
     */
    destroy() {
        this.isPlaying = false;
        this._queue = [];
        if (this.ctx) {
            this.ctx.close();
            this.ctx = null;
        }
    }

    // ── Internal ──

    _bindSocket() {
        this.socket.on('audio_pcm', (data) => {
            if (!this.ctx || !this.isPlaying) return;

            // Resume context if suspended (autoplay policy)
            if (this.ctx.state === 'suspended') {
                this.ctx.resume();
            }

            this._scheduleChunk(data);
        });

        this.socket.on('audio_level', (data) => {
            this.audioLevel = data.level_db;
            this.squelched = data.squelched;
        });
    }

    _scheduleChunk(buffer) {
        let float32;

        // Handle various binary formats from SocketIO
        if (buffer instanceof ArrayBuffer) {
            float32 = new Float32Array(buffer);
        } else if (buffer instanceof Blob) {
            // Blob needs async conversion — queue it
            buffer.arrayBuffer().then(ab => {
                this._scheduleFloat32(new Float32Array(ab));
            });
            return;
        } else if (buffer.buffer instanceof ArrayBuffer) {
            // TypedArray or DataView
            float32 = new Float32Array(buffer.buffer, buffer.byteOffset, buffer.byteLength / 4);
        } else {
            console.warn('[AudioPlayer] unknown buffer type:', typeof buffer);
            return;
        }

        this._scheduleFloat32(float32);
    }

    _scheduleFloat32(samples) {
        const numSamples = samples.length;
        if (numSamples === 0) return;

        // Create AudioBuffer
        const audioBuffer = this.ctx.createBuffer(1, numSamples, this.sampleRate);
        audioBuffer.getChannelData(0).set(samples);

        // Create source node
        const source = this.ctx.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(this.gainNode);

        // Schedule playback
        const now = this.ctx.currentTime;
        const chunkDuration = numSamples / this.sampleRate;

        // If we've fallen behind (gap in data), jump ahead
        if (this.nextStartTime < now) {
            // We're late — skip ahead with a small buffer to prevent underrun
            this.nextStartTime = now + 0.020;  // 20ms lead time
            this.chunksDropped++;
        }

        // Don't let buffer build up too far (>200ms ahead = too much latency)
        const ahead = this.nextStartTime - now;
        if (ahead > 0.200) {
            // Drop this chunk — we're too far ahead
            this.chunksDropped++;
            return;
        }

        source.start(this.nextStartTime);
        this.nextStartTime += chunkDuration;
        this.chunksPlayed++;
        this.bufferDepth = Math.round((this.nextStartTime - now) * 1000);  // ms
    }
}

// Export
window.AudioPlayer = AudioPlayer;
