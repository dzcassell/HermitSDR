"""Tests for audio demodulator."""
import pytest
import sys
import os
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from hermitsdr.demod import (
    Demodulator, DemodConfig, DemodMode,
    AUDIO_RATE, IQ_RATE, DECIMATION, CHUNK_SAMPLES, MODE_PARAMS,
)


class TestDemodConfig:
    def test_defaults(self):
        c = DemodConfig()
        assert c.mode == DemodMode.USB
        assert c.volume == 0.7
        assert c.agc_target == 0.3

    def test_to_dict(self):
        c = DemodConfig()
        d = c.to_dict()
        assert d['mode'] == 'usb'
        assert d['audio_rate'] == 48000
        assert 'volume' in d


class TestDemodMode:
    def test_all_modes_have_params(self):
        for mode in DemodMode:
            assert mode in MODE_PARAMS

    def test_mode_params_structure(self):
        for mode, (shift, bw) in MODE_PARAMS.items():
            assert isinstance(shift, (int, float))
            assert isinstance(bw, (int, float))
            assert bw > 0


class TestDemodConstants:
    def test_decimation_ratio(self):
        assert DECIMATION == IQ_RATE // AUDIO_RATE
        assert DECIMATION == 4

    def test_chunk_size(self):
        assert CHUNK_SAMPLES == 1024

    def test_audio_rate(self):
        assert AUDIO_RATE == 48000


class TestDemodulator:
    def test_init(self):
        d = Demodulator()
        assert d.config.mode == DemodMode.USB
        assert not d._running

    def test_push_iq(self):
        d = Demodulator()
        i = [1000, 2000, 3000]
        q = [4000, 5000, 6000]
        d.push_iq(i, q)
        assert len(d._iq_buffer) == 3

    def test_push_iq_numpy(self):
        d = Demodulator()
        i = np.array([100, 200, 300])
        q = np.array([400, 500, 600])
        d.push_iq(i, q)
        assert len(d._iq_buffer) == 3

    def test_set_mode_usb(self):
        d = Demodulator()
        d.set_mode('usb')
        assert d.config.mode == DemodMode.USB

    def test_set_mode_lsb(self):
        d = Demodulator()
        d.set_mode('lsb')
        assert d.config.mode == DemodMode.LSB

    def test_set_mode_cw(self):
        d = Demodulator()
        d.set_mode('cw')
        assert d.config.mode == DemodMode.CW

    def test_set_mode_am(self):
        d = Demodulator()
        d.set_mode('am')
        assert d.config.mode == DemodMode.AM

    def test_set_mode_invalid(self):
        d = Demodulator()
        d.set_mode('invalid')  # should warn but not crash
        assert d.config.mode == DemodMode.USB  # unchanged

    def test_set_volume(self):
        d = Demodulator()
        d.set_volume(0.5)
        assert d.config.volume == 0.5

    def test_set_volume_clamped(self):
        d = Demodulator()
        d.set_volume(2.0)
        assert d.config.volume == 1.0
        d.set_volume(-1.0)
        assert d.config.volume == 0.0

    def test_set_squelch(self):
        d = Demodulator()
        d.set_squelch(-80.0)
        assert d.config.squelch_db == -80.0

    def test_reconfigure(self):
        d = Demodulator()
        d.reconfigure(volume=0.3, agc_speed=0.1)
        assert d.config.volume == 0.3
        assert d.config.agc_speed == 0.1

    def test_reconfigure_mode(self):
        d = Demodulator()
        d.reconfigure(mode='cw')
        assert d.config.mode == DemodMode.CW

    def test_get_stats(self):
        d = Demodulator()
        s = d.get_stats()
        assert 'config' in s
        assert 'chunks_produced' in s
        assert 'buffer_depth' in s
        assert s['running'] is False

    def test_filter_rebuilt_on_mode_change(self):
        d = Demodulator()
        old_coeffs = d._filter_coeffs.copy()
        d.set_mode('cw')
        # CW filter should be different from USB
        assert len(d._filter_coeffs) == d.config.filter_taps + (1 if d.config.filter_taps % 2 == 0 else 0)

    def test_filter_bandwidth_usb(self):
        """USB filter must pass 1200 Hz, not 600 Hz (firwin fs= regression)."""
        d = Demodulator(DemodConfig(mode=DemodMode.USB))
        # Check filter response at 1000 Hz (should pass) and 2000 Hz (should reject)
        from scipy.signal import freqz
        w, h = freqz(d._filter_coeffs, worN=8192, fs=AUDIO_RATE)
        mag = np.abs(h)
        # Find magnitude at ~1000 Hz (should be near 1.0, within passband)
        idx_1k = np.argmin(np.abs(w - 1000))
        assert mag[idx_1k] > 0.5, f"USB filter rejects 1000 Hz (mag={mag[idx_1k]:.3f})"

    def test_filter_bandwidth_am(self):
        """AM filter must pass 5000 Hz, not 2500 Hz."""
        d = Demodulator(DemodConfig(mode=DemodMode.AM))
        from scipy.signal import freqz
        w, h = freqz(d._filter_coeffs, worN=8192, fs=AUDIO_RATE)
        mag = np.abs(h)
        idx_4k = np.argmin(np.abs(w - 4000))
        assert mag[idx_4k] > 0.5, f"AM filter rejects 4000 Hz (mag={mag[idx_4k]:.3f})"

    def test_demodulate_usb(self):
        """USB demod should produce real audio from a test tone."""
        d = Demodulator(DemodConfig(mode=DemodMode.USB, squelch_db=-200.0))
        # Generate a 1kHz tone at IQ rate (should be in USB passband)
        # Scale to realistic 24-bit amplitude
        n = CHUNK_SAMPLES * DECIMATION
        t = np.arange(n) / IQ_RATE
        tone = np.exp(2j * np.pi * 1000 * t) * 100000  # audible signal level
        audio = d._demodulate(tone)
        assert audio is not None
        assert len(audio) == CHUNK_SAMPLES
        assert audio.dtype == np.float32
        # Should have nonzero output (tone is in passband)
        assert np.max(np.abs(audio)) > 0

    def test_demodulate_am(self):
        """AM demod should recover envelope."""
        d = Demodulator(DemodConfig(mode=DemodMode.AM))
        n = CHUNK_SAMPLES * DECIMATION
        t = np.arange(n) / IQ_RATE
        # AM: carrier with 500 Hz modulation
        carrier = np.exp(2j * np.pi * 0 * t)
        modulation = 1.0 + 0.5 * np.cos(2 * np.pi * 500 * t)
        iq = carrier * modulation
        audio = d._demodulate(iq)
        assert audio is not None
        assert len(audio) == CHUNK_SAMPLES

    def test_demodulate_cw(self):
        """CW demod should produce a beat note."""
        d = Demodulator(DemodConfig(mode=DemodMode.CW))
        n = CHUNK_SAMPLES * DECIMATION
        t = np.arange(n) / IQ_RATE
        # Pure carrier at DC (CW signal)
        iq = np.ones(n, dtype=np.complex128)
        audio = d._demodulate(iq)
        assert audio is not None
        assert len(audio) == CHUNK_SAMPLES

    def test_agc(self):
        """AGC should normalize audio level."""
        d = Demodulator()
        # Loud signal
        audio = np.ones(1024) * 0.9
        result = d._apply_agc(audio)
        # AGC should bring it toward target (0.3)
        assert np.max(np.abs(result)) < 0.95

    def test_agc_quiet_signal(self):
        """AGC should amplify quiet signals."""
        d = Demodulator()
        d._agc_gain = 1.0
        audio = np.ones(1024) * 0.001
        result = d._apply_agc(audio)
        # Should be amplified
        assert np.max(np.abs(result)) > 0.001

    def test_squelch_closes(self):
        """Squelch should return silence for signals below threshold."""
        d = Demodulator(DemodConfig(squelch_db=-40.0))
        n = CHUNK_SAMPLES * DECIMATION
        # Very weak signal (well below -40 dBFS)
        iq = np.ones(n, dtype=np.complex128) * 1e-8
        audio = d._demodulate(iq)
        # Should be all zeros (squelched)
        assert np.allclose(audio, 0.0)

    def test_start_stop(self):
        """Demod thread starts and stops cleanly."""
        d = Demodulator()
        d.start()
        assert d._running is True
        assert d._thread is not None
        d.stop()
        assert d._running is False

    def test_callback_registration(self):
        d = Demodulator()
        called = []
        d.on_audio(lambda data, level: called.append(len(data)))
        assert len(d._callbacks) == 1
