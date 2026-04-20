"""Tests for HPSDR Protocol 1 packet construction and parsing."""
import pytest, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from hermitsdr.protocol import *
from hermitsdr.protocol import _sign_extend_24

class TestDiscovery:
    def test_discovery_packet_length(self):
        assert len(build_discovery_packet()) == 63

    def test_discovery_packet_header(self):
        pkt = build_discovery_packet()
        assert pkt[0:2] == METIS_SIGNATURE and pkt[2] == 0x02

    def test_parse_valid_hl2_reply(self):
        reply = bytearray(60)
        reply[0:2] = METIS_SIGNATURE; reply[2] = 0x02
        reply[3:9] = b'\x00\x1c\xc0\xa8\x28\x0a'
        reply[9] = 73; reply[10] = BOARD_ID_HL2; reply[19] = 4; reply[21] = 2
        r = parse_discovery_reply(bytes(reply), ('192.168.40.10', 1024))
        assert r and r.is_hl2 and r.board_name == 'Hermes-Lite2'
        assert r.firmware_version == '73.2' and r.num_hw_receivers == 4

    def test_parse_streaming(self):
        reply = bytearray(60); reply[0:2] = METIS_SIGNATURE; reply[2] = 0x03; reply[10] = BOARD_ID_HL2
        assert parse_discovery_reply(bytes(reply)).is_streaming

    def test_parse_invalid(self):
        assert parse_discovery_reply(b'\x00' * 60) is None
        assert parse_discovery_reply(b'\xef\xfe\x02') is None

class TestStartStop:
    def test_start_packet(self):
        pkt = build_start_packet(); assert len(pkt) == 63 and pkt[3] & 0x01
    def test_start_wideband(self):
        assert build_start_packet(start_wideband=True)[3] & 0x02
    def test_watchdog_disable(self):
        assert build_start_packet(disable_watchdog=True)[3] & 0x80
    def test_stop_packet(self):
        pkt = build_stop_packet(); assert len(pkt) == 63 and pkt[3] == 0x00

class TestCC:
    def test_encode_length(self):
        assert len(CCCommand(addr=0, data=0).encode()) == 5
    def test_addr(self):
        assert (CCCommand(addr=0x0A, data=0).encode()[0] >> 1) & 0x3F == 0x0A
    def test_mox(self):
        assert CCCommand(addr=0, data=0, mox=True).encode()[0] & 0x01
    def test_rqst(self):
        assert CCCommand(addr=0, data=0, rqst=True).encode()[0] & 0x80
    def test_freq_rx1(self):
        cc = cc_set_frequency(1, 7074000); assert cc.addr == 0x02 and cc.data == 7074000
    def test_freq_tx(self):
        assert cc_set_frequency(0, 14074000).addr == 0x01
    def test_lna_gain(self):
        e = cc_set_lna_gain(20).encode()
        d = (e[1]<<24)|(e[2]<<16)|(e[3]<<8)|e[4]
        assert d & 0x40 and (d & 0x3F) == 32

    def test_lna_gain_addr_is_0x0a(self):
        """HL2 LNA gain uses ADDR 0x0A in direct mode (bit 6 set)."""
        cmd = cc_set_lna_gain(20)
        assert cmd.addr == 0x0A, f"Expected ADDR 0x0A, got 0x{cmd.addr:02x}"
    def test_sample_rate_192k(self):
        e = cc_set_sample_rate(SampleRate.SR_192K).encode()
        d = (e[1]<<24)|(e[2]<<16)|(e[3]<<8)|e[4]
        assert (d >> 24) & 0x03 == 0b10

class TestIQPackets:
    def _make_pkt(self):
        import struct
        pkt = bytearray(1032)
        pkt[0:2] = METIS_SIGNATURE; pkt[2] = 0x01; pkt[3] = 0x06
        struct.pack_into('>I', pkt, 4, 42)
        pkt[8:11] = SYNC_BYTES
        for i in range(63):
            o = 16 + i*8; pkt[o:o+3] = b'\x00\x01\x00'; pkt[o+3:o+6] = b'\x00\x02\x00'
        pkt[520:523] = SYNC_BYTES
        return bytes(pkt)

    def test_parse_header(self):
        h = parse_metis_header(self._make_pkt()); assert h and h.sequence == 42 and h.is_iq
    def test_parse_full(self):
        r = parse_iq_packet(self._make_pkt()); assert r
        _, f1, f2 = r; assert len(f1.i_samples) == 63 and f1.i_samples[0] == 256
    def test_build_length(self):
        assert len(build_iq_packet(CCCommand(0,0), CCCommand(0,0))) == 1032

class TestUtils:
    def test_sign_extend(self):
        assert _sign_extend_24(0x000100) == 256 and _sign_extend_24(0xFFFF00) == -256
    def test_sample_rate(self):
        assert SampleRate.SR_192K.hz == 192000 and SampleRate.from_hz(192000) == SampleRate.SR_192K

if __name__ == '__main__': pytest.main([__file__, '-v'])


class TestTelemetry:
    """Test telemetry scaling in RadioTelemetry."""

    def test_temperature_scaling(self):
        from hermitsdr.radio import RadioTelemetry
        t = RadioTelemetry(temperature_raw=1051)
        assert abs(t.temperature_c - 65.7) < 0.1  # 1051 / 16.0

    def test_temperature_zero(self):
        from hermitsdr.radio import RadioTelemetry
        t = RadioTelemetry(temperature_raw=0)
        assert t.temperature_c == 0.0

    def test_current_scaling(self):
        from hermitsdr.radio import RadioTelemetry
        t = RadioTelemetry(current_raw=100)
        # (100 * 3.26 / 4096) / 0.050 ≈ 1592 mA
        assert t.current_ma > 0

    def test_to_dict_has_scaled(self):
        from hermitsdr.radio import RadioTelemetry
        t = RadioTelemetry(temperature_raw=800, current_raw=50)
        d = t.to_dict()
        assert 'temperature_c' in d
        assert 'current_ma' in d
        assert d['temperature_c'] == 50.0  # 800 / 16.0
