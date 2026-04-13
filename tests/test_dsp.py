"""Tests for DSP pipeline - FFT, windowing, spectral processing."""
import pytest
import sys
import os
import math
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from hermitsdr.dsp import (
    DSPPipeline, DSPConfig, SpectralFrame,
    WindowFunction, ColorPalette, generate_color_palette,
    HAS_GPU,
)


class TestDSPConfig:
    def test_defaults(self):
        c = DSPConfig()
        assert c.fft_size == 4096
        assert c.sample_rate == 192000
        assert c.window == WindowFunction.BLACKMAN_HARRIS

    def test_to_dict(self):
        c = DSPConfig()
        d = c.to_dict()
        assert d['fft_size'] == 4096
        assert 'gpu_available' in d


class TestDSPPipeline:
    def test_init(self):
        p = DSPPipeline(DSPConfig(fft_size=1024))
        assert p.config.fft_size == 1024

    def test_push_iq(self):
        p = DSPPipeline(DSPConfig(fft_size=256))
        i_data = [1000] * 256
        q_data = [500] * 256
        p.push_iq(i_data, q_data)
        assert len(p._iq_buffer) == 256

    def test_compute_spectrum_sine(self):
        """Feed a known sine wave and verify FFT output has a peak."""
        n = 1024
        p = DSPPipeline(DSPConfig(fft_size=n, sample_rate=192000, averaging=1.0))

        # Generate a tone at bin 100 (approx 18.75 kHz at 192k SR)
        freq_bin = 100
        samples = []
        for i in range(n):
            phase = 2 * math.pi * freq_bin * i / n
            samples.append(complex(
                int(1000000 * math.cos(phase)),
                int(1000000 * math.sin(phase))
            ))

        frame = p._compute_spectrum(samples)
        assert frame is not None
        assert len(frame.power_dbfs) == n
        assert frame.fft_size == n
        assert frame.bandwidth == 192000

        # The peak should be near bin 100 after fftshift
        # After fftshift, DC is at center (n/2), so bin 100 maps to n/2 + 100
        peak_idx = np.argmax(frame.power_dbfs)
        expected_idx = n // 2 + freq_bin
        assert abs(peak_idx - expected_idx) <= 2, f"Peak at {peak_idx}, expected near {expected_idx}"

    def test_compute_spectrum_returns_float32(self):
        n = 256
        p = DSPPipeline(DSPConfig(fft_size=n, averaging=1.0))
        samples = [complex(1000, 500)] * n
        frame = p._compute_spectrum(samples)
        assert frame.power_dbfs.dtype == np.float32

    def test_reconfigure(self):
        p = DSPPipeline(DSPConfig(fft_size=1024))
        p.reconfigure(fft_size=2048, averaging=0.5)
        assert p.config.fft_size == 2048
        assert p.config.averaging == 0.5

    def test_reconfigure_window(self):
        p = DSPPipeline()
        p.reconfigure(window='hann')
        assert p.config.window == WindowFunction.HANN

    def test_averaging(self):
        """Verify EMA smoothing works."""
        n = 256
        p = DSPPipeline(DSPConfig(fft_size=n, averaging=0.5))
        # First frame sets the baseline
        s1 = [complex(1000, 500)] * n
        f1 = p._compute_spectrum(s1)
        power1 = f1.power_dbfs.copy()
        # Second frame with different data should be averaged
        s2 = [complex(5000, 2500)] * n
        f2 = p._compute_spectrum(s2)
        # With alpha=0.5, result should be between the two
        # (not exactly equal to either)
        assert not np.allclose(f2.power_dbfs, power1, atol=0.1)

    def test_peak_hold(self):
        n = 256
        p = DSPPipeline(DSPConfig(fft_size=n, averaging=1.0, peak_hold=True))
        # Strong signal first
        s1 = [complex(10000, 10000)] * n
        f1 = p._compute_spectrum(s1)
        peak1_max = np.max(f1.peak_dbfs)
        # Weak signal second
        s2 = [complex(100, 100)] * n
        f2 = p._compute_spectrum(s2)
        # Peak should still be close to the first strong signal (within 2 dB)
        assert np.max(f2.peak_dbfs) >= peak1_max - 2.0

    def test_stats(self):
        p = DSPPipeline()
        stats = p.get_stats()
        assert 'fft_time_ms' in stats
        assert 'config' in stats
        assert stats['config']['fft_size'] == 4096


class TestSpectralFrame:
    def test_to_binary(self):
        power = np.zeros(1024, dtype=np.float32)
        frame = SpectralFrame(
            power_dbfs=power,
            timestamp=1.0,
            center_freq=7074000,
            bandwidth=192000,
            fft_size=1024,
            frame_number=42,
        )
        data = frame.to_binary()
        # Header: 24 bytes + 1024 * 4 bytes = 4120
        assert len(data) == 24 + 1024 * 4
        assert data[:3] == b'SPC'

    def test_to_binary_with_peak(self):
        power = np.zeros(512, dtype=np.float32)
        peak = np.ones(512, dtype=np.float32)
        frame = SpectralFrame(
            power_dbfs=power,
            peak_dbfs=peak,
            fft_size=512,
            frame_number=0,
        )
        data = frame.to_binary()
        # Header + power + peak
        assert len(data) == 24 + 512 * 4 * 2


class TestColorPalette:
    def test_classic_length(self):
        colors = generate_color_palette(ColorPalette.CLASSIC)
        assert len(colors) == 256

    def test_rgb_range(self):
        for palette in ColorPalette:
            colors = generate_color_palette(palette)
            for r, g, b in colors:
                assert 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255

    def test_all_palettes(self):
        for palette in ColorPalette:
            colors = generate_color_palette(palette)
            assert len(colors) == 256

    def test_gradient_not_flat(self):
        colors = generate_color_palette(ColorPalette.CLASSIC)
        # First and last colors should be different
        assert colors[0] != colors[255]


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
