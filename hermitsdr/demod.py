"""
HermitSDR - Audio Demodulator
==============================

Demodulates IQ samples into audio for browser playback.

Pipeline:
    IQ (192kHz complex) → Anti-alias FIR (stateful) → Decimate (÷4 → 48kHz) →
    Freq Shift → Bandpass FIR (stateful) → Mode Demod → AGC → float32 PCM

Modes:
    USB  - Upper sideband: shift -1500Hz, LPF ±1200Hz, real part
    LSB  - Lower sideband: shift +1500Hz, LPF ±1200Hz, real part
    CW   - Morse/CW: shift -700Hz, LPF ±250Hz, real part
    AM   - Amplitude mod: LPF ±5000Hz, envelope (|·|)
"""

import struct
import time
import logging
import threading
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable

import numpy as np
from scipy.signal import firwin, lfilter, lfilter_zi

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

AUDIO_RATE = 48000          # Output sample rate (Hz)
IQ_RATE = 192000            # Input IQ sample rate (Hz)
DECIMATION = IQ_RATE // AUDIO_RATE  # = 4
CHUNK_SAMPLES = 1024        # Samples per audio chunk (~21ms at 48kHz)
AUDIO_CHUNK_BYTES = CHUNK_SAMPLES * 4  # float32


class DemodMode(Enum):
    USB = 'usb'
    LSB = 'lsb'
    CW = 'cw'
    AM = 'am'


# Mode-specific parameters: (shift_hz, filter_bandwidth_hz)
# shift_hz moves the desired passband center to 0 Hz before LPF
MODE_PARAMS = {
    DemodMode.USB: (-1500, 1200),   # passband 300–2700 Hz
    DemodMode.LSB: (1500, 1200),    # passband -2700 to -300 Hz
    DemodMode.CW:  (-700, 250),     # passband 450–950 Hz
    DemodMode.AM:  (0, 5000),       # passband ±5000 Hz
}


@dataclass
class DemodConfig:
    """Demodulator configuration."""
    mode: DemodMode = DemodMode.USB
    agc_speed: float = 0.05     # AGC attack/decay (0=slow, 1=fast)
    agc_target: float = 0.3     # Target audio level (0..1)
    volume: float = 0.7         # Output volume (0..1)
    squelch_db: float = -140.0  # Squelch threshold (dBFS, -140 = off)
    filter_taps: int = 201      # FIR filter order (odd)
    bfo_offset: int = 700       # CW beat frequency offset (Hz)

    def to_dict(self) -> dict:
        return {
            'mode': self.mode.value,
            'agc_speed': self.agc_speed,
            'agc_target': self.agc_target,
            'volume': self.volume,
            'squelch_db': self.squelch_db,
            'audio_rate': AUDIO_RATE,
        }


class Demodulator:
    """Audio demodulator for IQ streams.

    Consumes complex IQ samples at 192kHz, produces 48kHz mono
    float32 audio chunks for WebSocket streaming to the browser.
    """

    def __init__(self, config: Optional[DemodConfig] = None):
        self.config = config or DemodConfig()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # IQ input buffer (complex128, at IQ_RATE)
        self._iq_buffer: deque = deque(maxlen=IQ_RATE * 2)

        # Audio output callbacks
        self._callbacks: list[Callable] = []

        # Filter state
        self._filter_coeffs = None
        self._filter_state = None

        # Anti-alias decimation filter (stateful, runs at IQ_RATE)
        self._aa_coeffs = None
        self._aa_state = None
        self._rebuild_filter()

        # AGC state
        self._agc_gain = 1.0

        # Mixer phase accumulator (continuous across chunks)
        self._mixer_phase = 0.0

        # Stats
        self._chunks_produced = 0
        self._audio_level = 0.0
        self._last_stats = 0.0

        logger.info(f"Demodulator initialized: mode={self.config.mode.value} "
                     f"audio_rate={AUDIO_RATE} decimation={DECIMATION}")

    def on_audio(self, callback: Callable):
        """Register callback: callback(audio_bytes, level_db)."""
        self._callbacks.append(callback)

    def push_iq(self, i_samples, q_samples):
        """Push IQ samples into the demodulator buffer.

        Accepts lists or numpy arrays of integer I/Q samples.
        """
        iq = np.asarray(i_samples, dtype=np.float64) + \
             1j * np.asarray(q_samples, dtype=np.float64)
        self._iq_buffer.extend(iq)

    def start(self):
        """Start the demodulator processing thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._process_loop, daemon=True, name="demod"
        )
        self._thread.start()
        logger.info("Demodulator started")

    def stop(self):
        """Stop the demodulator."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        logger.info("Demodulator stopped")

    def set_mode(self, mode: str):
        """Change demodulation mode."""
        try:
            new_mode = DemodMode(mode.lower())
        except ValueError:
            logger.warning(f"Unknown mode: {mode}")
            return
        with self._lock:
            self.config.mode = new_mode
            self._rebuild_filter()
            self._agc_gain = 1.0  # Reset AGC on mode change
        logger.info(f"Demod mode → {new_mode.value}")

    def set_volume(self, volume: float):
        """Set output volume (0..1)."""
        self.config.volume = max(0.0, min(1.0, volume))

    def set_squelch(self, db: float):
        """Set squelch threshold in dBFS (-140 = off)."""
        self.config.squelch_db = db

    def set_agc_speed(self, speed: float):
        """Set AGC speed (0=slow, 1=fast)."""
        self.config.agc_speed = max(0.001, min(1.0, speed))

    def reconfigure(self, **kwargs):
        """Update multiple config values."""
        with self._lock:
            rebuild = False
            for key, val in kwargs.items():
                if key == 'mode':
                    self.config.mode = DemodMode(val.lower())
                    rebuild = True
                elif hasattr(self.config, key):
                    setattr(self.config, key, val)
            if rebuild:
                self._rebuild_filter()
                self._agc_gain = 1.0

    def get_stats(self) -> dict:
        return {
            'config': self.config.to_dict(),
            'chunks_produced': self._chunks_produced,
            'audio_level_db': round(self._audio_level, 1),
            'agc_gain': round(self._agc_gain, 2),
            'buffer_depth': len(self._iq_buffer),
            'running': self._running,
        }

    # ── Internal ──

    def _rebuild_filter(self):
        """Design FIR filters for the current mode.

        Two filters are maintained with persistent state:
        1. Anti-alias LPF at IQ_RATE for decimation (prevents aliasing)
        2. Mode bandpass at AUDIO_RATE for signal selection
        """
        _, bandwidth = MODE_PARAMS[self.config.mode]

        # ── Anti-alias decimation filter (at IQ_RATE) ──
        # Cutoff at decimated Nyquist with transition guard band
        aa_cutoff = AUDIO_RATE * 0.45  # 21.6 kHz — below 24 kHz Nyquist
        aa_taps = 65  # moderate length for anti-alias
        self._aa_coeffs = firwin(aa_taps, aa_cutoff, fs=IQ_RATE, window='blackman')
        self._aa_state = lfilter_zi(self._aa_coeffs, 1.0) * 0.0

        # ── Mode filter (at AUDIO_RATE, post-decimation) ──
        # Cutoff in Hz — firwin with fs= interprets directly
        mode_cutoff = min(bandwidth, AUDIO_RATE / 2 - 100)  # guard Nyquist

        num_taps = self.config.filter_taps
        if num_taps % 2 == 0:
            num_taps += 1  # odd for type I FIR

        self._filter_coeffs = firwin(num_taps, mode_cutoff, fs=AUDIO_RATE,
                                     window='blackman')
        self._filter_state = lfilter_zi(self._filter_coeffs, 1.0) * 0.0

        logger.info(f"Filter rebuilt: mode={self.config.mode.value} "
                     f"bw={bandwidth}Hz cutoff={mode_cutoff}Hz taps={num_taps} "
                     f"aa_taps={aa_taps}")

    def _process_loop(self):
        """Main demodulator loop.

        Waits for enough IQ samples to produce one audio chunk,
        then processes and dispatches it.
        """
        # We need CHUNK_SAMPLES * DECIMATION IQ samples per audio chunk
        iq_needed = CHUNK_SAMPLES * DECIMATION  # 1024 * 4 = 4096

        while self._running:
            if len(self._iq_buffer) < iq_needed:
                time.sleep(0.005)
                continue

            # Extract IQ samples
            with self._lock:
                samples = [self._iq_buffer.popleft() for _ in
                           range(min(iq_needed, len(self._iq_buffer)))]

            if len(samples) < iq_needed:
                time.sleep(0.005)
                continue

            try:
                audio = self._demodulate(np.array(samples, dtype=np.complex128))
                if audio is not None:
                    self._chunks_produced += 1
                    audio_bytes = audio.astype(np.float32).tobytes()
                    for cb in self._callbacks:
                        try:
                            cb(audio_bytes, self._audio_level)
                        except Exception as e:
                            logger.error(f"Audio callback error: {e}")
            except Exception as e:
                logger.error(f"Demod error: {e}")

    def _demodulate(self, iq: np.ndarray) -> Optional[np.ndarray]:
        """Full demodulation pipeline for one audio chunk.

        Args:
            iq: Complex IQ samples at IQ_RATE (length = CHUNK_SAMPLES * DECIMATION)

        Returns:
            float32 audio array of length CHUNK_SAMPLES, or None if squelched
        """
        mode = self.config.mode
        shift_hz, bandwidth = MODE_PARAMS[mode]

        # ── Step 1: Anti-alias filter + decimate 192k → 48k ──
        # Stateful FIR filter prevents block-boundary transients
        aa_filtered, self._aa_state = lfilter(
            self._aa_coeffs, 1.0, iq, zi=self._aa_state
        )
        decimated = aa_filtered[::DECIMATION]

        # ── Step 2: Frequency shift to center desired passband at 0 ──
        if shift_hz != 0:
            n = len(decimated)
            t = np.arange(n) / AUDIO_RATE + self._mixer_phase
            mixer = np.exp(2j * np.pi * shift_hz * t)
            decimated = decimated * mixer
            # Update phase accumulator (avoid float drift)
            self._mixer_phase += n / AUDIO_RATE
            self._mixer_phase %= 1.0 / abs(shift_hz) if shift_hz != 0 else 1.0

        # ── Step 3: Bandpass FIR filter ──
        filtered, self._filter_state = lfilter(
            self._filter_coeffs, 1.0, decimated, zi=self._filter_state
        )

        # ── Step 4: Mode-specific demodulation ──
        if mode in (DemodMode.USB, DemodMode.LSB, DemodMode.CW):
            audio = np.real(filtered)
        elif mode == DemodMode.AM:
            audio = np.abs(filtered)
            # Remove DC component from AM envelope
            audio = audio - np.mean(audio)
        else:
            audio = np.real(filtered)

        # ── Step 5: Squelch ──
        rms = np.sqrt(np.mean(audio ** 2))
        if rms > 0:
            level_db = 20 * np.log10(rms / 8388608.0)  # relative to 24-bit FS
        else:
            level_db = -160.0
        self._audio_level = level_db

        if level_db < self.config.squelch_db:
            return np.zeros(CHUNK_SAMPLES, dtype=np.float32)

        # ── Step 6: AGC ──
        audio = self._apply_agc(audio)

        # ── Step 7: Volume ──
        audio = audio * self.config.volume

        # ── Step 8: Clip to [-1, 1] ──
        audio = np.clip(audio, -1.0, 1.0)

        return audio.astype(np.float32)

    def _apply_agc(self, audio: np.ndarray) -> np.ndarray:
        """Simple AGC — adjusts gain to keep audio near target level.

        Uses a peak-based approach with configurable attack/decay speed.
        """
        target = self.config.agc_target
        speed = self.config.agc_speed

        peak = np.max(np.abs(audio))
        if peak < 1e-10:
            return audio

        # Desired gain to hit target
        desired_gain = target / peak

        # Smooth gain change (attack faster than decay)
        if desired_gain < self._agc_gain:
            # Attack (signal getting louder) — react faster
            alpha = min(1.0, speed * 4)
        else:
            # Decay (signal getting quieter) — react slower
            alpha = speed

        self._agc_gain += alpha * (desired_gain - self._agc_gain)

        # Clamp gain range (don't amplify noise to insane levels)
        self._agc_gain = max(0.001, min(100.0, self._agc_gain))

        return audio * self._agc_gain
