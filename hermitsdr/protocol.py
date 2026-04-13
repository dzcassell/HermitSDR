"""
HermitSDR - HPSDR Protocol 1 Implementation for Hermes Lite 2
=============================================================

Implements packet construction and parsing per the HL2 protocol wiki:
https://github.com/softerhardware/Hermes-Lite2/wiki/Protocol

Key concepts:
- All communication is UDP on port 1024
- Discovery: 63-byte broadcast packet → 60-byte reply
- IQ data: 1032-byte UDP frames (8-byte Metis header + 2 × 512-byte USB frames)
- Each USB frame: 3-byte sync (0x7F7F7F) + 5 C&C bytes + 63 × (3I + 3Q + 2mic) samples
- C&C bytes carry command/control and status in every frame
"""

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

import numpy as np


# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

METIS_PORT = 1024
METIS_SIGNATURE = b'\xef\xfe'
SYNC_BYTES = b'\x7f\x7f\x7f'

BOARD_ID_HL2 = 0x06
BOARD_ID_HERMES = 0x01

# IQ frame geometry
METIS_HEADER_LEN = 8
USB_FRAME_LEN = 512
IQ_PACKET_LEN = METIS_HEADER_LEN + 2 * USB_FRAME_LEN  # 1032
CC_LEN = 5           # C0..C4
SAMPLES_PER_FRAME = 63
SAMPLE_BYTES = 8     # 3 I + 3 Q + 2 mic/audio

# Endpoint IDs in Metis header byte[3]
EP_IQ_DATA = 0x06    # IQ data from radio
EP_WIDEBAND = 0x04   # Wideband ADC data from radio
EP_IQ_TO_RADIO = 0x02  # IQ data to radio


class SampleRate(IntEnum):
    """Sample rate encoding in ADDR 0x00 bits [25:24]."""
    SR_48K = 0b00
    SR_96K = 0b01
    SR_192K = 0b10
    SR_384K = 0b11

    @property
    def hz(self) -> int:
        return {0: 48000, 1: 96000, 2: 192000, 3: 384000}[self.value]

    @classmethod
    def from_hz(cls, hz: int) -> 'SampleRate':
        mapping = {48000: cls.SR_48K, 96000: cls.SR_96K,
                   192000: cls.SR_192K, 384000: cls.SR_384K}
        if hz not in mapping:
            raise ValueError(f"Unsupported sample rate: {hz}")
        return mapping[hz]


# ──────────────────────────────────────────────
# Discovery
# ──────────────────────────────────────────────

def build_discovery_packet() -> bytes:
    """Build a 63-byte Metis discovery packet.

    Format: <0xEF><0xFE><0x02><60 bytes of 0x00>
    """
    return METIS_SIGNATURE + b'\x02' + (b'\x00' * 60)


def build_directed_discovery_packet() -> bytes:
    """Same as broadcast discovery, but sent to a known IP.

    The HL2 responds to both broadcast and directed discovery.
    """
    return build_discovery_packet()


@dataclass
class DiscoveryReply:
    """Parsed Metis discovery reply from an HL2.

    Reply is 60 bytes per the HL2 protocol wiki.
    """
    raw: bytes
    status: int = 0           # 0x02 = idle, 0x03 = streaming
    mac_address: str = ""
    gateware_major: int = 0
    board_id: int = 0
    config_bits: int = 0
    fixed_ip: str = ""
    num_hw_receivers: int = 0
    wideband_type: int = 0    # 0=12-bit sign-ext, 1=16-bit
    board_id_ext: int = 0     # lower 6 bits of 0x14
    gateware_minor: int = 0
    firmware_version: str = ""
    source_ip: str = ""
    source_port: int = 0

    @property
    def is_hl2(self) -> bool:
        return self.board_id == BOARD_ID_HL2

    @property
    def is_streaming(self) -> bool:
        return self.status == 0x03

    @property
    def board_name(self) -> str:
        names = {
            0x00: "Metis",
            0x01: "Hermes",
            0x02: "Griffin",
            0x04: "Angelia",
            0x05: "Orion",
            0x06: "Hermes-Lite2",
        }
        return names.get(self.board_id, f"Unknown (0x{self.board_id:02x})")

    def to_dict(self) -> dict:
        is_apipa = self.source_ip.startswith('169.254.')
        return {
            'mac_address': self.mac_address,
            'board_name': self.board_name,
            'board_id': self.board_id,
            'is_hl2': self.is_hl2,
            'is_streaming': self.is_streaming,
            'gateware_version': self.firmware_version,
            'num_receivers': self.num_hw_receivers,
            'wideband_type': 'u16' if self.wideband_type else 's12',
            'fixed_ip': self.fixed_ip,
            'source_ip': self.source_ip,
            'source_port': self.source_port,
            'config_bits': self.config_bits,
            'is_apipa': is_apipa,
            'needs_setup': is_apipa or not (self.config_bits & 0x80),
        }


def parse_discovery_reply(data: bytes, addr: tuple = ('', 0)) -> Optional[DiscoveryReply]:
    """Parse a Metis discovery reply.

    Args:
        data: Raw UDP payload (should be >= 60 bytes)
        addr: (ip, port) tuple from recvfrom

    Returns:
        DiscoveryReply or None if not a valid reply
    """
    if len(data) < 60:
        return None
    if data[0:2] != METIS_SIGNATURE:
        return None
    status = data[2]
    if status not in (0x02, 0x03):
        return None

    mac = ':'.join(f'{b:02x}' for b in data[3:9])
    gw_major = data[9]
    board_id = data[10]

    # Extended HL2 fields (bytes 0x0B onward)
    config_bits = data[11]
    fixed_ip_bytes = data[13:17]
    fixed_ip = '.'.join(str(b) for b in fixed_ip_bytes)

    num_hw_rx = data[19] if len(data) > 19 else 0
    wb_type = (data[20] >> 6) & 0x03 if len(data) > 20 else 0
    board_id_ext = data[20] & 0x3F if len(data) > 20 else 0
    gw_minor = data[21] if len(data) > 21 else 0

    fw_version = f"{gw_major}.{gw_minor}"

    return DiscoveryReply(
        raw=data,
        status=status,
        mac_address=mac,
        gateware_major=gw_major,
        board_id=board_id,
        config_bits=config_bits,
        fixed_ip=fixed_ip,
        num_hw_receivers=num_hw_rx,
        wideband_type=wb_type,
        board_id_ext=board_id_ext,
        gateware_minor=gw_minor,
        firmware_version=fw_version,
        source_ip=addr[0],
        source_port=addr[1],
    )


# ──────────────────────────────────────────────
# Start / Stop
# ──────────────────────────────────────────────

def build_start_packet(start_iq: bool = True, start_wideband: bool = False,
                       disable_watchdog: bool = False) -> bytes:
    """Build a Metis start command packet (63 bytes).

    Format: <0xEF><0xFE><0x04><Command><60 bytes of 0x00>

    Command bits:
        [0] = Start IQ streaming
        [1] = Start wideband data
        [7] = Disable watchdog timer (useful for RX-only)
    """
    cmd = 0x00
    if start_iq:
        cmd |= 0x01
    if start_wideband:
        cmd |= 0x02
    if disable_watchdog:
        cmd |= 0x80
    return METIS_SIGNATURE + b'\x04' + bytes([cmd]) + (b'\x00' * 59)


def build_stop_packet() -> bytes:
    """Build a Metis stop command packet (63 bytes).

    Format: <0xEF><0xFE><0x04><0x00><60 bytes of 0x00>
    """
    return METIS_SIGNATURE + b'\x04' + (b'\x00' * 60)


# ──────────────────────────────────────────────
# Command & Control (C&C) Encoding
# ──────────────────────────────────────────────

@dataclass
class CCCommand:
    """A single C0..C4 command word to embed in an IQ frame.

    C0 format:
        [7]    = RQST (request ACK response from HL2)
        [6:1]  = ADDR[5:0] (register address)
        [0]    = MOX (1=transmit, 0=receive)

    C1..C4 = DATA[31:0] for the addressed register
    """
    addr: int
    data: int
    mox: bool = False
    rqst: bool = False

    def encode(self) -> bytes:
        """Encode to 5 bytes (C0..C4)."""
        c0 = ((self.addr & 0x3F) << 1) | (int(self.mox) & 0x01)
        if self.rqst:
            c0 |= 0x80
        c1 = (self.data >> 24) & 0xFF
        c2 = (self.data >> 16) & 0xFF
        c3 = (self.data >> 8) & 0xFF
        c4 = self.data & 0xFF
        return bytes([c0, c1, c2, c3, c4])


def cc_set_sample_rate(rate: SampleRate, num_rx: int = 1,
                       duplex: bool = False) -> CCCommand:
    """ADDR 0x00: Set sample rate, number of receivers, duplex.

    DATA layout:
        [25:24] = Speed
        [6:3]   = Number of receivers - 1
        [2]     = Duplex
    """
    data = (rate.value << 24) | (((num_rx - 1) & 0x0F) << 3) | (int(duplex) << 2)
    return CCCommand(addr=0x00, data=data)


def cc_set_frequency(rx_index: int, freq_hz: int) -> CCCommand:
    """Set NCO frequency for TX (index=0) or RX1..RX12.

    ADDR mapping:
        0x01 = TX1 NCO
        0x02 = RX1 NCO
        0x03 = RX2 NCO
        ...
        0x08 = RX7 NCO
        0x12 = RX8 NCO
        ...
    """
    if rx_index == 0:
        addr = 0x01  # TX
    elif 1 <= rx_index <= 7:
        addr = 0x01 + rx_index  # 0x02..0x08
    elif 8 <= rx_index <= 12:
        addr = 0x0A + rx_index  # 0x12..0x16
    else:
        raise ValueError(f"Invalid RX index: {rx_index}")
    return CCCommand(addr=addr, data=freq_hz & 0xFFFFFFFF)


def cc_set_lna_gain(gain_db: int) -> CCCommand:
    """ADDR 0x0A: Set LNA gain in direct mode (-12 to +48 dB).

    DATA layout:
        [6]   = 1 (direct gain mode)
        [5:0] = gain value (0=-12dB, 60=+48dB)
    """
    gain_val = max(0, min(60, gain_db + 12))
    data = (1 << 6) | (gain_val & 0x3F)
    return CCCommand(addr=0x0A, data=data)


def cc_set_tx_drive(level: int, pa_on: bool = False) -> CCCommand:
    """ADDR 0x09: TX drive level and PA control.

    DATA layout:
        [31:28] = TX drive level (0-15)
        [19]    = PA on/off
    """
    data = ((level & 0x0F) << 28) | (int(pa_on) << 19)
    return CCCommand(addr=0x09, data=data)


# ──────────────────────────────────────────────
# IQ Frame Building (PC → Radio)
# ──────────────────────────────────────────────

_tx_seq_counter = 0


def build_iq_packet(cc1: CCCommand, cc2: CCCommand,
                    tx_iq_samples: Optional[bytes] = None) -> bytes:
    """Build a 1032-byte IQ packet to send to the radio.

    Structure:
        [0:1]   = 0xEFFE (signature)
        [2]     = 0x01 (endpoint tag for IQ to radio)
        [3]     = Endpoint EP2 (0x02)
        [4:7]   = Sequence number (big-endian 32-bit)
        [8:519] = USB frame 1 (sync + CC1 + 63 IQ samples)
        [520:1031] = USB frame 2 (sync + CC2 + 63 IQ samples)
    """
    global _tx_seq_counter

    # Metis header
    header = METIS_SIGNATURE + b'\x01' + bytes([EP_IQ_TO_RADIO])
    header += struct.pack('>I', _tx_seq_counter & 0xFFFFFFFF)
    _tx_seq_counter += 1

    # Build USB frames
    frame1 = _build_usb_frame(cc1, tx_iq_samples)
    frame2 = _build_usb_frame(cc2, None)

    return header + frame1 + frame2


def _build_usb_frame(cc: CCCommand, iq_data: Optional[bytes] = None) -> bytes:
    """Build a single 512-byte USB frame.

    Structure:
        [0:2]   = Sync (0x7F7F7F)
        [3:7]   = C0..C4 (command & control)
        [8:511] = 63 × (L1 L0 R1 R0 I2 I1 I0 Q2 Q1 Q0) ... simplified:
                   63 × 8 bytes = 504 bytes of IQ + audio samples

    For TX silence: all zeros.
    """
    frame = bytearray(USB_FRAME_LEN)
    frame[0:3] = SYNC_BYTES
    frame[3:8] = cc.encode()

    if iq_data and len(iq_data) >= 504:
        frame[8:512] = iq_data[:504]
    # else: zeros (silence)

    return bytes(frame)


# ──────────────────────────────────────────────
# IQ Frame Parsing (Radio → PC)
# ──────────────────────────────────────────────

@dataclass
class MetisHeader:
    """Parsed 8-byte Metis header."""
    signature: bytes
    packet_type: int    # 0x01 for IQ, 0x04 for wideband
    endpoint: int
    sequence: int

    @property
    def is_iq(self) -> bool:
        return self.endpoint == EP_IQ_DATA

    @property
    def is_wideband(self) -> bool:
        return self.endpoint == EP_WIDEBAND


@dataclass
class CCStatus:
    """Parsed C&C status from a received USB frame."""
    ack: bool
    addr: int
    data: int
    ptt: bool
    dot: bool
    raw: bytes

    @property
    def firmware_version(self) -> int:
        """RADDR 0x00: firmware version in DATA[7:0]."""
        if not self.ack and self.addr == 0x00:
            return self.data & 0xFF
        return 0

    @property
    def adc_overload(self) -> bool:
        """RADDR 0x00: DATA[24]."""
        if not self.ack and self.addr == 0x00:
            return bool((self.data >> 24) & 0x01)
        return False

    @property
    def temperature_raw(self) -> int:
        """RADDR 0x01: DATA[31:16]."""
        if not self.ack and self.addr == 0x01:
            return (self.data >> 16) & 0xFFFF
        return 0

    @property
    def forward_power_raw(self) -> int:
        """RADDR 0x01: DATA[15:0]."""
        if not self.ack and self.addr == 0x01:
            return self.data & 0xFFFF
        return 0

    @property
    def reverse_power_raw(self) -> int:
        """RADDR 0x02: DATA[31:16]."""
        if not self.ack and self.addr == 0x02:
            return (self.data >> 16) & 0xFFFF
        return 0

    @property
    def current_raw(self) -> int:
        """RADDR 0x02: DATA[15:0]."""
        if not self.ack and self.addr == 0x02:
            return self.data & 0xFFFF
        return 0


@dataclass
class IQFrame:
    """Parsed IQ samples from one USB frame (63 samples)."""
    cc: CCStatus
    i_samples: list = field(default_factory=list)  # 24-bit signed integers
    q_samples: list = field(default_factory=list)   # 24-bit signed integers


def _sign_extend_24(val: int) -> int:
    """Sign-extend a 24-bit integer to Python int."""
    if val & 0x800000:
        return val - 0x1000000
    return val


def parse_metis_header(data: bytes) -> Optional[MetisHeader]:
    """Parse the 8-byte Metis header."""
    if len(data) < METIS_HEADER_LEN:
        return None
    sig = data[0:2]
    if sig != METIS_SIGNATURE:
        return None
    ptype = data[2]
    endpoint = data[3]
    seq = struct.unpack('>I', data[4:8])[0]
    return MetisHeader(signature=sig, packet_type=ptype,
                       endpoint=endpoint, sequence=seq)


def parse_cc_status(frame_data: bytes) -> CCStatus:
    """Parse C0..C4 from a USB frame (bytes 3..7 after sync)."""
    c0 = frame_data[0]
    c1234 = frame_data[1:5]
    ack = bool(c0 & 0x80)

    if ack:
        addr = (c0 >> 1) & 0x3F
    else:
        addr = (c0 >> 3) & 0x0F
    ptt = bool(c0 & 0x01)
    dot = bool(c0 & 0x04) if not ack else False

    data = struct.unpack('>I', c1234)[0]
    return CCStatus(ack=ack, addr=addr, data=data, ptt=ptt, dot=dot,
                    raw=bytes([c0]) + c1234)


def parse_usb_frame(frame_data: bytes) -> Optional[IQFrame]:
    """Parse a 512-byte USB frame into IQ samples and C&C status.

    Structure:
        [0:2]  = Sync 0x7F7F7F
        [3:7]  = C0..C4
        [8:]   = 63 × (I2 I1 I0 Q2 Q1 Q0 M1 M0) = 63 × 8 bytes

    I and Q are 24-bit signed big-endian. M (mic) is 16-bit, ignored.

    Uses numpy vectorized ops to avoid per-sample Python loops —
    critical at 192kHz where this runs ~3000×/sec.
    """
    if len(frame_data) < USB_FRAME_LEN:
        return None
    if frame_data[0:3] != SYNC_BYTES:
        return None

    cc = parse_cc_status(frame_data[3:8])

    # Vectorized extraction: reshape 504 sample bytes into (63, 8)
    raw = np.frombuffer(frame_data, dtype=np.uint8, offset=8,
                        count=SAMPLES_PER_FRAME * SAMPLE_BYTES)
    raw = raw.reshape(SAMPLES_PER_FRAME, SAMPLE_BYTES)

    # 24-bit big-endian → int32: I = bytes 0,1,2; Q = bytes 3,4,5
    i_raw = (raw[:, 0].astype(np.int32) << 16 |
             raw[:, 1].astype(np.int32) << 8 |
             raw[:, 2].astype(np.int32))
    q_raw = (raw[:, 3].astype(np.int32) << 16 |
             raw[:, 4].astype(np.int32) << 8 |
             raw[:, 5].astype(np.int32))

    # Sign-extend 24-bit → 32-bit
    i_samples = np.where(i_raw & 0x800000, i_raw - 0x1000000, i_raw)
    q_samples = np.where(q_raw & 0x800000, q_raw - 0x1000000, q_raw)

    return IQFrame(cc=cc, i_samples=i_samples, q_samples=q_samples)


def parse_iq_packet(data: bytes) -> Optional[tuple]:
    """Parse a full 1032-byte IQ packet from the radio.

    Returns:
        (MetisHeader, IQFrame1, IQFrame2) or None
    """
    if len(data) < IQ_PACKET_LEN:
        return None

    header = parse_metis_header(data)
    if header is None:
        return None

    frame1 = parse_usb_frame(data[METIS_HEADER_LEN:METIS_HEADER_LEN + USB_FRAME_LEN])
    frame2 = parse_usb_frame(data[METIS_HEADER_LEN + USB_FRAME_LEN:])

    if frame1 is None or frame2 is None:
        return None

    return (header, frame1, frame2)


# ──────────────────────────────────────────────
# Wideband Data Parsing
# ──────────────────────────────────────────────

def parse_wideband_packet(data: bytes) -> Optional[tuple]:
    """Parse a wideband ADC data packet.

    The HL2 ADC runs at 76.8 MHz, collecting 2048 samples.
    Each packet has 1024 bytes of payload = 512 16-bit samples.
    A complete wideband block spans 4 packets (tracked by seq % 4 == 0).

    Returns:
        (MetisHeader, list[int]) - header and 512 signed 16-bit samples
    """
    if len(data) < IQ_PACKET_LEN:
        return None

    header = parse_metis_header(data)
    if header is None or not header.is_wideband:
        return None

    samples = []
    payload = data[METIS_HEADER_LEN:]
    for i in range(0, 1024, 2):
        val = struct.unpack('>h', payload[i:i + 2])[0]
        samples.append(val)

    return (header, samples)


# ──────────────────────────────────────────────
# Utility: Hex dump for protocol debugging
# ──────────────────────────────────────────────

def hex_dump(data: bytes, width: int = 16, max_bytes: int = 256) -> str:
    """Format bytes as a hex dump string for debugging."""
    lines = []
    for offset in range(0, min(len(data), max_bytes), width):
        chunk = data[offset:offset + width]
        hex_part = ' '.join(f'{b:02x}' for b in chunk)
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f'{offset:04x}  {hex_part:<{width * 3}}  {ascii_part}')
    if len(data) > max_bytes:
        lines.append(f'... ({len(data) - max_bytes} more bytes)')
    return '\n'.join(lines)
