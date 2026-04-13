"""
HermitSDR - Radio Connection Manager
=====================================

Manages the UDP connection to a Hermes Lite 2, handling:
- Start/stop IQ streaming
- C&C command scheduling
- IQ sample collection and dispatch to processing pipeline
- Telemetry extraction (temp, power, current)
"""

import socket
import select
import struct
import time
import threading
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Callable, List

from .protocol import (
    METIS_PORT, IQ_PACKET_LEN, SAMPLES_PER_FRAME,
    SampleRate,
    build_start_packet, build_stop_packet, build_iq_packet,
    parse_iq_packet, parse_wideband_packet,
    CCCommand, cc_set_sample_rate, cc_set_frequency, cc_set_lna_gain,
    DiscoveryReply, hex_dump,
)

logger = logging.getLogger(__name__)


@dataclass
class RadioTelemetry:
    """Live telemetry from the HL2.

    Raw values are ADC readings. Scaled values use HL2-standard
    conversion formulas (may need per-device calibration):
      - Temperature: I2C sensor in LM75 format → raw / 16.0 °C
      - FWD/REV power: ADC counts (meaningful only when transmitting)
      - Current: ADC counts → raw * 3.26 / 4096 / 0.04 mA (50mV/A sense)
    """
    firmware_version: int = 0
    adc_overload: bool = False
    temperature_raw: int = 0
    forward_power_raw: int = 0
    reverse_power_raw: int = 0
    current_raw: int = 0
    ptt: bool = False
    dot: bool = False
    tx_fifo_count: int = 0
    last_update: float = 0.0

    # Packet stats
    rx_packets: int = 0
    rx_bytes: int = 0
    rx_sequence_errors: int = 0
    last_sequence: int = -1

    @property
    def temperature_c(self) -> float:
        """Temperature in °C (LM75-format I2C sensor: raw / 16.0)."""
        if self.temperature_raw == 0:
            return 0.0
        return self.temperature_raw / 16.0

    @property
    def current_ma(self) -> float:
        """Board current in mA (HL2 50mV/A current sense via ADC).

        Formula: ADC_voltage = raw * 3.26 / 4096
        Current = ADC_voltage / 0.050  (50mV per amp)
        """
        if self.current_raw == 0:
            return 0.0
        return (self.current_raw * 3.26 / 4096.0) / 0.050

    def to_dict(self) -> dict:
        return {
            'firmware_version': self.firmware_version,
            'adc_overload': self.adc_overload,
            'temperature_raw': self.temperature_raw,
            'temperature_c': round(self.temperature_c, 1),
            'forward_power_raw': self.forward_power_raw,
            'reverse_power_raw': self.reverse_power_raw,
            'current_raw': self.current_raw,
            'current_ma': round(self.current_ma, 0),
            'ptt': self.ptt,
            'dot': self.dot,
            'rx_packets': self.rx_packets,
            'rx_bytes': self.rx_bytes,
            'rx_sequence_errors': self.rx_sequence_errors,
            'packets_per_sec': 0,  # calculated by caller
        }


@dataclass
class RadioState:
    """Current radio configuration."""
    sample_rate: SampleRate = SampleRate.SR_192K
    frequency_hz: int = 7_074_000  # 40m FT8
    lna_gain_db: int = 20
    num_receivers: int = 1
    duplex: bool = False
    connected: bool = False
    streaming: bool = False


class RadioConnection:
    """Manages a live connection to a Hermes Lite 2."""

    def __init__(self, device: DiscoveryReply):
        self.device = device
        self.state = RadioState()
        self.telemetry = RadioTelemetry()
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._rx_thread: Optional[threading.Thread] = None
        self._tx_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # IQ sample buffer — consumers (FFT pipeline) read from here
        self._iq_buffer: deque = deque(maxlen=65536)

        # Pending C&C commands to interleave into TX frames
        self._cc_queue: deque = deque(maxlen=256)

        # Callbacks
        self._iq_callbacks: list[Callable] = []
        self._telemetry_callbacks: list[Callable] = []
        self._wideband_callbacks: list[Callable] = []

        # Packet rate tracking
        self._pps_counter = 0
        self._pps_last_time = 0.0
        self._pps_rate = 0.0

        # Callback throttle
        self._telem_last_emit = 0.0

    def on_iq_data(self, callback: Callable):
        """Register callback for IQ data: callback(i_samples, q_samples)."""
        self._iq_callbacks.append(callback)

    def on_telemetry(self, callback: Callable):
        """Register callback for telemetry updates: callback(telemetry)."""
        self._telemetry_callbacks.append(callback)

    def on_wideband(self, callback: Callable):
        """Register callback for wideband data: callback(samples)."""
        self._wideband_callbacks.append(callback)

    def connect(self) -> bool:
        """Open UDP socket and prepare for streaming."""
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Increase receive buffer for high sample rates
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            self._sock.bind(('', 0))
            self._sock.settimeout(1.0)
            self.state.connected = True
            logger.info(f"Connected to {self.device.board_name} at "
                        f"{self.device.source_ip}:{METIS_PORT}")
            return True
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            return False

    def start_streaming(self) -> bool:
        """Start IQ data streaming from the radio."""
        if not self.state.connected or self._running:
            return False

        # Send start command
        start_pkt = build_start_packet(
            start_iq=True, start_wideband=False, disable_watchdog=True
        )
        self._sock.sendto(start_pkt, (self.device.source_ip, METIS_PORT))
        logger.info("Start command sent")

        # Queue initial configuration
        self._cc_queue.append(cc_set_sample_rate(
            self.state.sample_rate, self.state.num_receivers, self.state.duplex
        ))
        self._cc_queue.append(cc_set_frequency(1, self.state.frequency_hz))
        self._cc_queue.append(cc_set_lna_gain(self.state.lna_gain_db))

        self._running = True
        self.state.streaming = True
        self._pps_last_time = time.monotonic()

        # Start RX thread
        self._rx_thread = threading.Thread(
            target=self._rx_loop, daemon=True, name="hl2-rx"
        )
        self._rx_thread.start()

        # Start TX thread (sends keepalive + C&C commands)
        self._tx_thread = threading.Thread(
            target=self._tx_loop, daemon=True, name="hl2-tx"
        )
        self._tx_thread.start()

        return True

    def stop_streaming(self):
        """Stop IQ streaming."""
        self._running = False
        if self._sock and self.device.source_ip:
            try:
                stop_pkt = build_stop_packet()
                self._sock.sendto(stop_pkt, (self.device.source_ip, METIS_PORT))
                logger.info("Stop command sent")
            except Exception as e:
                logger.warning(f"Error sending stop: {e}")

        self.state.streaming = False
        if self._rx_thread:
            self._rx_thread.join(timeout=3.0)
        if self._tx_thread:
            self._tx_thread.join(timeout=3.0)

    def disconnect(self):
        """Stop streaming and close socket."""
        if self.state.streaming:
            self.stop_streaming()
        if self._sock:
            self._sock.close()
            self._sock = None
        self.state.connected = False

    def set_frequency(self, freq_hz: int):
        """Queue a frequency change."""
        freq_hz = int(freq_hz)
        self.state.frequency_hz = freq_hz
        self._cc_queue.append(cc_set_frequency(1, freq_hz))

    def set_lna_gain(self, gain_db: int):
        """Queue an LNA gain change."""
        self.state.lna_gain_db = gain_db
        self._cc_queue.append(cc_set_lna_gain(gain_db))

    def set_sample_rate(self, rate_hz: int):
        """Queue a sample rate change. Requires restart to take effect."""
        self.state.sample_rate = SampleRate.from_hz(rate_hz)
        self._cc_queue.append(cc_set_sample_rate(
            self.state.sample_rate, self.state.num_receivers, self.state.duplex
        ))

    def get_iq_samples(self, count: int) -> Optional[tuple]:
        """Pull IQ samples from the buffer.

        Returns:
            (i_array, q_array) of length up to `count`, or None if empty
        """
        i_out = []
        q_out = []
        with self._lock:
            while self._iq_buffer and len(i_out) < count:
                i_val, q_val = self._iq_buffer.popleft()
                i_out.append(i_val)
                q_out.append(q_val)
        return (i_out, q_out) if i_out else None

    # ──── Internal loops ────

    def _rx_loop(self):
        """Receive and parse IQ packets from the radio."""
        logger.info("RX loop started")
        diag_time = time.monotonic()
        diag_packets = 0
        while self._running:
            try:
                ready, _, _ = select.select([self._sock], [], [], 0.5)
                if not ready:
                    if self._running:
                        logger.warning("RX: no data received in 500ms")
                    continue
                data, addr = self._sock.recvfrom(2048)
                self._process_rx_packet(data)
                diag_packets += 1

                # Periodic diagnostic log
                now = time.monotonic()
                if now - diag_time >= 5.0:
                    logger.info(
                        f"RX: {diag_packets} pkts in 5s "
                        f"({diag_packets/5:.0f}/s), "
                        f"total={self.telemetry.rx_packets}, "
                        f"seq_err={self.telemetry.rx_sequence_errors}, "
                        f"buf={len(self._iq_buffer)}"
                    )
                    diag_packets = 0
                    diag_time = now
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"RX error: {e}", exc_info=True)
        logger.info("RX loop stopped")

    def _process_rx_packet(self, data: bytes):
        """Process a received UDP packet."""
        self.telemetry.rx_packets += 1
        self.telemetry.rx_bytes += len(data)

        # Track packet rate
        self._pps_counter += 1
        now = time.monotonic()
        if now - self._pps_last_time >= 1.0:
            self._pps_rate = self._pps_counter / (now - self._pps_last_time)
            self._pps_counter = 0
            self._pps_last_time = now

        result = parse_iq_packet(data)
        if result is None:
            # Try wideband
            wb = parse_wideband_packet(data)
            if wb:
                header, samples = wb
                for cb in self._wideband_callbacks:
                    try:
                        cb(samples)
                    except Exception as e:
                        logger.error(f"Wideband callback error: {e}")
            return

        header, frame1, frame2 = result

        # Sequence check
        if self.telemetry.last_sequence >= 0:
            expected = (self.telemetry.last_sequence + 1) & 0xFFFFFFFF
            if header.sequence != expected:
                self.telemetry.rx_sequence_errors += 1
                logger.debug(f"Sequence gap: expected {expected}, got {header.sequence}")
        self.telemetry.last_sequence = header.sequence

        # Extract telemetry from C&C status
        for frame in (frame1, frame2):
            self._update_telemetry(frame.cc)

            # Notify IQ callbacks (DSP pipeline consumes via callback)
            for cb in self._iq_callbacks:
                try:
                    cb(frame.i_samples, frame.q_samples)
                except Exception as e:
                    logger.error(f"IQ callback error: {e}")

    def _update_telemetry(self, cc):
        """Update telemetry from a parsed C&C status."""
        self.telemetry.ptt = cc.ptt
        self.telemetry.dot = cc.dot
        self.telemetry.last_update = time.monotonic()

        if not cc.ack:
            if cc.addr == 0x00:
                self.telemetry.firmware_version = cc.data & 0xFF
                self.telemetry.adc_overload = bool((cc.data >> 24) & 0x01)
                self.telemetry.tx_fifo_count = (cc.data >> 8) & 0x7F
            elif cc.addr == 0x01:
                self.telemetry.temperature_raw = (cc.data >> 16) & 0xFFFF
                self.telemetry.forward_power_raw = cc.data & 0xFFFF
            elif cc.addr == 0x02:
                self.telemetry.reverse_power_raw = (cc.data >> 16) & 0xFFFF
                self.telemetry.current_raw = cc.data & 0xFFFF

        # Throttle telemetry callbacks to ~4/sec (avoid overwhelming consumers)
        now = time.monotonic()
        if now - self._telem_last_emit >= 0.25:
            self._telem_last_emit = now
            for cb in self._telemetry_callbacks:
                try:
                    cb(self.telemetry)
                except Exception as e:
                    logger.error(f"Telemetry callback error: {e}")

    def _tx_loop(self):
        """Send IQ packets to the radio at the required cadence.

        The HL2 expects 1032-byte packets every ~2.625ms to maintain
        the streaming connection. We send silence (zeros) with C&C
        commands interleaved.

        Default C&C commands cycle through the current radio state
        (sample rate, frequency, gain). They're rebuilt each iteration
        from self.state so that user changes (frequency, gain) are
        reflected immediately — not stale from startup.
        """
        logger.info(f"TX loop started → {self.device.source_ip}:{METIS_PORT}")
        interval = 0.002625  # ~2.625ms per packet
        tx_count = 0
        cc_idx = 0

        while self._running:
            try:
                cc1 = None
                cc2 = None

                if self._cc_queue:
                    cc1 = self._cc_queue.popleft()
                if self._cc_queue:
                    cc2 = self._cc_queue.popleft()

                # Build default commands from LIVE state (not a snapshot)
                if cc1 is None or cc2 is None:
                    defaults = [
                        cc_set_sample_rate(self.state.sample_rate,
                                           self.state.num_receivers,
                                           self.state.duplex),
                        cc_set_frequency(1, self.state.frequency_hz),
                        cc_set_lna_gain(self.state.lna_gain_db),
                    ]
                    if cc1 is None:
                        cc1 = defaults[cc_idx % len(defaults)]
                        cc_idx += 1
                    if cc2 is None:
                        cc2 = defaults[cc_idx % len(defaults)]
                        cc_idx += 1

                pkt = build_iq_packet(cc1, cc2)
                self._sock.sendto(pkt, (self.device.source_ip, METIS_PORT))
                tx_count += 1

                if tx_count == 1:
                    logger.info(f"TX: first keepalive sent ({len(pkt)} bytes)")
                elif tx_count == 100:
                    logger.info(f"TX: 100 keepalive packets sent OK")

                time.sleep(interval)
            except Exception as e:
                if self._running:
                    logger.error(f"TX error: {e}", exc_info=True)
                    time.sleep(0.1)

        logger.info(f"TX loop stopped (sent {tx_count} packets)")
