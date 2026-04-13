"""
HermitSDR - GPU-Accelerated DSP Pipeline
==========================================

CuPy FFT pipeline with automatic numpy fallback.
Processes IQ samples from the HL2 into power spectral density
for waterfall and spectrum display.

Pipeline: IQ → Window → FFT → |·|² → dBFS → Averaging → Output
"""

import time
import struct
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable

import numpy as np

logger = logging.getLogger(__name__)

# Try CuPy (GPU), fall back to numpy (CPU)
# Import alone isn't enough — the 5070 Ti (Blackwell/sm_120) needs
# a runtime kernel test since precompiled binaries may not exist.
HAS_GPU = False
try:
    import cupy as cp
    import cupy.fft as fft_lib
    # Runtime test: actually execute a small kernel
    _test = cp.array([1.0, 2.0, 3.0])
    _test_result = cp.fft.fft(_test)
    assert _test_result is not None
    del _test, _test_result
    HAS_GPU = True
    xp = cp
    logger.info("CuPy GPU runtime test passed")
except Exception as e:
    import numpy.fft as fft_lib
    HAS_GPU = False
    xp = None
    logger.warning(f"GPU unavailable ({type(e).__name__}: {e}), using numpy fallback")


class WindowFunction(Enum):
    BLACKMAN_HARRIS = 'blackman_harris'
    HANN = 'hann'
    HAMMING = 'hamming'
    FLAT_TOP = 'flat_top'
    NONE = 'none'


class ColorPalette(Enum):
    CLASSIC = 'classic'        # blue → cyan → green → yellow → red
    GRAYSCALE = 'grayscale'
    THERMAL = 'thermal'        # black → red → yellow → white
    NEON = 'neon'              # dark purple → magenta → cyan → white


@dataclass
class DSPConfig:
    """DSP pipeline configuration."""
    fft_size: int = 4096
    sample_rate: int = 192000
    window: WindowFunction = WindowFunction.BLACKMAN_HARRIS
    averaging: float = 0.3         # EMA alpha (0=frozen, 1=instant)
    peak_hold: bool = False
    peak_decay: float = 0.995      # Peak decay per frame
    db_min: float = -140.0
    db_max: float = -20.0
    fps_target: int = 30
    overlap: float = 0.5           # FFT overlap ratio (0..0.9)
    center_freq: int = 7_074_000

    def to_dict(self) -> dict:
        return {
            'fft_size': self.fft_size,
            'sample_rate': self.sample_rate,
            'window': self.window.value,
            'averaging': self.averaging,
            'peak_hold': self.peak_hold,
            'db_min': self.db_min,
            'db_max': self.db_max,
            'fps_target': self.fps_target,
            'overlap': self.overlap,
            'center_freq': self.center_freq,
            'gpu_available': HAS_GPU,
            'gpu_active': HAS_GPU,
        }


@dataclass
class SpectralFrame:
    """One frame of processed spectral data."""
    power_dbfs: np.ndarray       # FFT bins in dBFS (float32)
    peak_dbfs: Optional[np.ndarray] = None
    timestamp: float = 0.0
    center_freq: int = 0
    bandwidth: int = 0
    fft_size: int = 0
    frame_number: int = 0

    def to_binary(self) -> bytes:
        """Pack as binary for WebSocket transport.

        Format:
            [0:3]    Magic 'SPC\\x00'
            [4:7]    Frame number (u32 LE)
            [8:11]   Center freq Hz (u32 LE)
            [12:15]  Bandwidth Hz (u32 LE)
            [16:17]  FFT size (u16 LE)
            [18:19]  Flags (u16 LE) - bit0: has peak data
            [20:23]  Timestamp (f32 LE)
            [24:]    Power data (float32 LE array)
            [24+N*4:] Peak data if present
        """
        flags = 0x01 if self.peak_dbfs is not None else 0x00
        header = struct.pack('<4sIIIHHf',
            b'SPC\x00',
            self.frame_number,
            self.center_freq,
            self.bandwidth,
            self.fft_size,
            flags,
            self.timestamp,
        )
        data = self.power_dbfs.astype(np.float32).tobytes()
        if self.peak_dbfs is not None:
            data += self.peak_dbfs.astype(np.float32).tobytes()
        return header + data


class DSPPipeline:
    """GPU-accelerated spectral processing pipeline.

    Consumes IQ samples, produces SpectralFrame objects at the
    configured frame rate.
    """

    def __init__(self, config: Optional[DSPConfig] = None):
        self.config = config or DSPConfig()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Sample input buffer
        self._iq_buffer = deque(maxlen=self.config.fft_size * 8)

        # Precomputed window
        self._window = None
        self._window_correction = 1.0
        self._rebuild_window()

        # Averaging state
        self._avg_power = None
        self._peak_power = None
        self._frame_count = 0

        # Output callbacks
        self._callbacks: list[Callable] = []

        # Stats
        self._fft_time_ms = 0.0
        self._frames_produced = 0
        self._last_stats_time = 0.0
        self._fps_actual = 0.0

        logger.info(f"DSP pipeline initialized: FFT={self.config.fft_size} "
                     f"SR={self.config.sample_rate} GPU={'YES' if HAS_GPU else 'NO (numpy fallback)'}")

    def on_frame(self, callback: Callable):
        """Register callback: callback(SpectralFrame)."""
        self._callbacks.append(callback)

    def push_iq(self, i_samples: list, q_samples: list):
        """Push IQ samples into the pipeline buffer.

        Converts integer I/Q sample lists to complex and extends the
        buffer. Uses batch conversion (not per-sample) for throughput.
        """
        # Batch convert to complex — much faster than per-sample loop
        iq = np.array(i_samples, dtype=np.float64) + 1j * np.array(q_samples, dtype=np.float64)
        self._iq_buffer.extend(iq)

    def start(self):
        """Start the DSP processing thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._process_loop, daemon=True, name="dsp-pipeline"
        )
        self._thread.start()
        logger.info("DSP pipeline started")

    def stop(self):
        """Stop the DSP processing thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        logger.info("DSP pipeline stopped")

    def reconfigure(self, **kwargs):
        """Update configuration. Rebuilds window if fft_size changes."""
        rebuild = False
        with self._lock:
            for key, val in kwargs.items():
                if key == 'window':
                    val = WindowFunction(val)
                if hasattr(self.config, key):
                    old = getattr(self.config, key)
                    setattr(self.config, key, val)
                    if key in ('fft_size', 'window'):
                        rebuild = True
                    if key == 'fft_size' and val != old:
                        self._avg_power = None
                        self._peak_power = None
        if rebuild:
            self._rebuild_window()

    def get_stats(self) -> dict:
        return {
            'gpu_active': HAS_GPU,
            'fft_time_ms': round(self._fft_time_ms, 2),
            'fps_actual': round(self._fps_actual, 1),
            'frames_produced': self._frames_produced,
            'buffer_depth': len(self._iq_buffer),
            'config': self.config.to_dict(),
        }

    # ── Internal ──

    def _rebuild_window(self):
        """Precompute the FFT window function."""
        n = self.config.fft_size
        if self.config.window == WindowFunction.BLACKMAN_HARRIS:
            a0, a1, a2, a3 = 0.35875, 0.48829, 0.14128, 0.01168
            t = np.arange(n) / n
            w = a0 - a1*np.cos(2*np.pi*t) + a2*np.cos(4*np.pi*t) - a3*np.cos(6*np.pi*t)
        elif self.config.window == WindowFunction.HANN:
            w = 0.5 * (1 - np.cos(2 * np.pi * np.arange(n) / n))
        elif self.config.window == WindowFunction.HAMMING:
            w = 0.54 - 0.46 * np.cos(2 * np.pi * np.arange(n) / n)
        elif self.config.window == WindowFunction.FLAT_TOP:
            a0, a1, a2, a3, a4 = 0.21557895, 0.41663158, 0.277263158, 0.083578947, 0.006947368
            t = np.arange(n) / n
            w = a0 - a1*np.cos(2*np.pi*t) + a2*np.cos(4*np.pi*t) - a3*np.cos(6*np.pi*t) + a4*np.cos(8*np.pi*t)
        else:
            w = np.ones(n)

        # Coherent gain correction factor
        self._window_correction = np.sum(w) / n
        if self._window_correction == 0:
            self._window_correction = 1.0

        if HAS_GPU:
            self._window = cp.asarray(w, dtype=cp.float64)
        else:
            self._window = w.astype(np.float64)

        logger.info(f"Window rebuilt: {self.config.window.value} "
                     f"N={n} correction={self._window_correction:.4f}")

    def _process_loop(self):
        """Main DSP processing loop."""
        frame_interval = 1.0 / self.config.fps_target
        self._last_stats_time = time.monotonic()
        fps_counter = 0

        while self._running:
            t_start = time.monotonic()

            # Wait for enough samples
            needed = self.config.fft_size
            if len(self._iq_buffer) < needed:
                time.sleep(0.005)
                continue

            # Extract samples from buffer (batch, not per-sample)
            with self._lock:
                hop = max(1, int(needed * (1 - self.config.overlap)))
                # Batch pop — convert deque slice to list in one shot
                samples = [self._iq_buffer.popleft() for _ in
                           range(min(needed, len(self._iq_buffer)))]
                # Put back overlap portion for next FFT window
                overlap_count = needed - hop
                if overlap_count > 0 and len(samples) == needed:
                    overlap = samples[-overlap_count:]
                    # extendleft reverses, so reverse first
                    self._iq_buffer.extendleft(reversed(overlap))

            if len(samples) < needed:
                time.sleep(0.005)
                continue

            # Process on GPU or CPU
            try:
                frame = self._compute_spectrum(samples)
                if frame is not None:
                    self._frame_count += 1
                    self._frames_produced += 1
                    fps_counter += 1

                    # Notify callbacks
                    for cb in self._callbacks:
                        try:
                            cb(frame)
                        except Exception as e:
                            logger.error(f"Frame callback error: {e}")
            except Exception as e:
                logger.error(f"DSP error: {e}")

            # FPS tracking
            now = time.monotonic()
            if now - self._last_stats_time >= 1.0:
                self._fps_actual = fps_counter / (now - self._last_stats_time)
                fps_counter = 0
                self._last_stats_time = now

            # Rate limit to target FPS
            elapsed = time.monotonic() - t_start
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)

    def _compute_spectrum(self, samples: list) -> Optional[SpectralFrame]:
        """Compute power spectrum from IQ samples.

        Pipeline: IQ → Window → FFT → fftshift → |·|² → dBFS → Avg
        """
        n = self.config.fft_size
        t0 = time.monotonic()

        # Convert to complex array
        iq_np = np.array(samples, dtype=np.complex128)

        if HAS_GPU:
            iq = cp.asarray(iq_np)
        else:
            iq = iq_np

        # Apply window
        iq_windowed = iq * self._window

        # FFT
        spectrum = fft_lib.fft(iq_windowed, n=n)
        spectrum = fft_lib.fftshift(spectrum)

        # Power spectral density (magnitude squared)
        # Normalize by FFT size and window correction
        psd = (spectrum.real**2 + spectrum.imag**2) / (n * self._window_correction)**2

        # Prevent log(0)
        if HAS_GPU:
            psd = cp.maximum(psd, cp.finfo(cp.float64).tiny)
        else:
            psd = np.maximum(psd, np.finfo(np.float64).tiny)

        # Convert to dBFS (relative to full-scale 24-bit)
        # Full scale for 24-bit signed: 2^23 = 8388608
        full_scale = 8388608.0
        if HAS_GPU:
            power_dbfs = 10 * cp.log10(psd) - 20 * cp.log10(cp.float64(full_scale))
        else:
            power_dbfs = 10 * np.log10(psd) - 20 * np.log10(full_scale)

        # Transfer back to CPU if on GPU
        if HAS_GPU:
            power_dbfs_cpu = cp.asnumpy(power_dbfs).astype(np.float32)
        else:
            power_dbfs_cpu = power_dbfs.astype(np.float32)

        self._fft_time_ms = (time.monotonic() - t0) * 1000

        # Exponential moving average
        alpha = self.config.averaging
        if self._avg_power is None or len(self._avg_power) != n:
            self._avg_power = power_dbfs_cpu.copy()
        else:
            self._avg_power = alpha * power_dbfs_cpu + (1 - alpha) * self._avg_power

        # Peak hold
        peak_out = None
        if self.config.peak_hold:
            if self._peak_power is None or len(self._peak_power) != n:
                self._peak_power = power_dbfs_cpu.copy()
            else:
                # Decay toward noise floor (subtract dB per frame)
                decay_db = (1.0 - self.config.peak_decay) * 200  # ~1 dB/frame at 0.995
                self._peak_power = np.maximum(
                    power_dbfs_cpu,
                    self._peak_power - decay_db
                )
            peak_out = self._peak_power.copy()

        return SpectralFrame(
            power_dbfs=self._avg_power.copy(),
            peak_dbfs=peak_out,
            timestamp=time.monotonic(),
            center_freq=self.config.center_freq,
            bandwidth=self.config.sample_rate,
            fft_size=n,
            frame_number=self._frame_count,
        )


def generate_color_palette(palette: ColorPalette, steps: int = 256) -> list:
    """Generate an RGB color palette for waterfall rendering.

    Returns list of [r, g, b] values (0-255) for dB mapping.
    Index 0 = weakest signal (db_min), index 255 = strongest (db_max).
    """
    colors = []
    for i in range(steps):
        t = i / (steps - 1)  # 0..1

        if palette == ColorPalette.CLASSIC:
            # Blue → Cyan → Green → Yellow → Red
            if t < 0.25:
                s = t / 0.25
                r, g, b = 0, int(s * 255), 255
            elif t < 0.5:
                s = (t - 0.25) / 0.25
                r, g, b = 0, 255, int((1 - s) * 255)
            elif t < 0.75:
                s = (t - 0.5) / 0.25
                r, g, b = int(s * 255), 255, 0
            else:
                s = (t - 0.75) / 0.25
                r, g, b = 255, int((1 - s) * 255), 0

        elif palette == ColorPalette.THERMAL:
            if t < 0.33:
                s = t / 0.33
                r, g, b = int(s * 200), 0, 0
            elif t < 0.66:
                s = (t - 0.33) / 0.33
                r, g, b = 200 + int(s * 55), int(s * 200), 0
            else:
                s = (t - 0.66) / 0.34
                r, g, b = 255, 200 + int(s * 55), int(s * 255)

        elif palette == ColorPalette.NEON:
            if t < 0.33:
                s = t / 0.33
                r, g, b = int(40 + s * 120), 0, int(60 + s * 140)
            elif t < 0.66:
                s = (t - 0.33) / 0.33
                r, g, b = int(160 - s * 100), int(s * 255), int(200 + s * 55)
            else:
                s = (t - 0.66) / 0.34
                r, g, b = int(60 + s * 195), 255, 255

        else:  # GRAYSCALE
            v = int(t * 255)
            r, g, b = v, v, v

        colors.append([min(255, max(0, r)), min(255, max(0, g)), min(255, max(0, b))])

    return colors
