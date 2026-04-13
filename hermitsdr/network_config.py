"""
HermitSDR - HL2 Network Configuration
=======================================

Read and write the Hermes Lite 2 EEPROM network settings
(fixed IP, config flags) over the HPSDR Protocol 1 I2C2 bus.

EEPROM layout (MCP4662 at I2C address 0x56/0xac):
    0x06: Config bits — [7]=valid_ip, [6]=valid_mac, [5]=favor_dhcp
    0x08: IP octet W
    0x09: IP octet X
    0x0A: IP octet Y
    0x0B: IP octet Z

Write format (32-bit word to C&C ADDR 0x3d):
    0x06acA0vv — A=EEPROM addr nibble, vv=value byte

Reference: https://github.com/softerhardware/Hermes-Lite2/wiki/Protocol
"""

import socket
import struct
import time
import logging
from dataclasses import dataclass
from typing import Optional

from .protocol import METIS_PORT, METIS_SIGNATURE, SYNC_BYTES

logger = logging.getLogger(__name__)

I2C2_ADDR = 0x3d
WRITE_DELAY = 0.050  # 50ms per EEPROM write


@dataclass
class HL2NetworkConfig:
    """Current network configuration from an HL2 discovery reply."""
    source_ip: str = ""
    mac_address: str = ""
    fixed_ip: str = "0.0.0.0"
    valid_ip: bool = False
    valid_mac: bool = False
    favor_dhcp: bool = False
    is_apipa: bool = False

    def to_dict(self) -> dict:
        return {
            'source_ip': self.source_ip,
            'mac_address': self.mac_address,
            'fixed_ip': self.fixed_ip,
            'valid_ip': self.valid_ip,
            'favor_dhcp': self.favor_dhcp,
            'is_apipa': self.is_apipa,
            'needs_setup': self.is_apipa or not self.valid_ip,
        }

    @staticmethod
    def from_discovery_reply(data: bytes, addr: tuple) -> 'HL2NetworkConfig':
        """Parse network config from a discovery reply."""
        mac = ':'.join(f'{b:02x}' for b in data[3:9])
        config = data[11] if len(data) > 11 else 0
        fixed_ip = '.'.join(str(b) for b in data[13:17]) if len(data) > 16 else '0.0.0.0'
        source_ip = addr[0]

        return HL2NetworkConfig(
            source_ip=source_ip,
            mac_address=mac,
            fixed_ip=fixed_ip,
            valid_ip=bool(config & 0x80),
            valid_mac=bool(config & 0x40),
            favor_dhcp=bool(config & 0x20),
            is_apipa=source_ip.startswith('169.254.'),
        )


def _eeprom_write_word(eeprom_addr: int, value: int) -> int:
    """Build the 32-bit I2C2 EEPROM write word: 0x06acA0vv."""
    return (0x06 << 24) | (0xac << 16) | (eeprom_addr << 12) | (value & 0xFF)


def _build_start_packet() -> bytes:
    """Start with watchdog disabled."""
    return METIS_SIGNATURE + b'\x04' + bytes([0x81]) + b'\x00' * 59


def _build_stop_packet() -> bytes:
    return METIS_SIGNATURE + b'\x04' + b'\x00' * 60


def _build_write_packet(seq: int, eeprom_addr: int, value: int) -> bytes:
    """Build a 1032-byte IQ packet with one EEPROM write in frame 1."""
    pkt = bytearray(1032)
    pkt[0:2] = METIS_SIGNATURE
    pkt[2] = 0x01
    pkt[3] = 0x02
    struct.pack_into('>I', pkt, 4, seq)

    # Frame 1: I2C2 write
    pkt[8:11] = SYNC_BYTES
    c0 = (I2C2_ADDR & 0x3F) << 1
    word = _eeprom_write_word(eeprom_addr, value)
    pkt[11] = c0
    pkt[12] = (word >> 24) & 0xFF
    pkt[13] = (word >> 16) & 0xFF
    pkt[14] = (word >> 8) & 0xFF
    pkt[15] = word & 0xFF

    # Frame 2: idle
    pkt[520:523] = SYNC_BYTES

    return bytes(pkt)


def _build_idle_packet(seq: int) -> bytes:
    """Build a 1032-byte idle IQ packet."""
    pkt = bytearray(1032)
    pkt[0:2] = METIS_SIGNATURE
    pkt[2] = 0x01
    pkt[3] = 0x02
    struct.pack_into('>I', pkt, 4, seq)
    pkt[8:11] = SYNC_BYTES
    pkt[520:523] = SYNC_BYTES
    return bytes(pkt)


def _drain_socket(sock, count=20):
    """Drain up to `count` packets from socket."""
    for _ in range(count):
        try:
            sock.recvfrom(2048)
        except socket.timeout:
            break


def set_hl2_ip(hl2_current_ip: str, new_ip: str, favor_dhcp: bool = False) -> dict:
    """Program a fixed IP address into an HL2's EEPROM.

    Args:
        hl2_current_ip: Current reachable IP of the HL2
        new_ip: New IP address to program (e.g., "192.168.40.20")
        favor_dhcp: If True, HL2 will prefer DHCP over fixed IP

    Returns:
        dict with status and details

    Note: The HL2 must be power-cycled after programming.
    """
    try:
        octets = [int(x) for x in new_ip.split('.')]
        if len(octets) != 4 or not all(0 <= o <= 255 for o in octets):
            return {'success': False, 'error': f'Invalid IP: {new_ip}'}
    except ValueError:
        return {'success': False, 'error': f'Invalid IP format: {new_ip}'}

    config_byte = 0x80  # valid_ip=1
    if favor_dhcp:
        config_byte |= 0x20

    writes = [
        (0x08, octets[0], 'IP.W'),
        (0x09, octets[1], 'IP.X'),
        (0x0A, octets[2], 'IP.Y'),
        (0x0B, octets[3], 'IP.Z'),
        (0x06, config_byte, 'Config'),
    ]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', 0))
    sock.settimeout(0.005)

    try:
        # Start protocol
        sock.sendto(_build_start_packet(), (hl2_current_ip, METIS_PORT))
        time.sleep(0.1)
        _drain_socket(sock, 50)

        # Write each EEPROM value
        seq = 0
        written = []
        for eeprom_addr, value, name in writes:
            pkt = _build_write_packet(seq, eeprom_addr, value)
            sock.sendto(pkt, (hl2_current_ip, METIS_PORT))
            seq += 1
            time.sleep(WRITE_DELAY)
            _drain_socket(sock)
            written.append(f'{name}=0x{value:02x}')
            logger.info(f"EEPROM write: {name} [0x{eeprom_addr:02x}] = 0x{value:02x}")

        # Keepalive while writes settle
        for _ in range(20):
            sock.sendto(_build_idle_packet(seq), (hl2_current_ip, METIS_PORT))
            seq += 1
            time.sleep(0.010)

        # Stop
        sock.sendto(_build_stop_packet(), (hl2_current_ip, METIS_PORT))

        logger.info(f"HL2 EEPROM programmed: {new_ip} (power cycle required)")
        return {
            'success': True,
            'new_ip': new_ip,
            'favor_dhcp': favor_dhcp,
            'writes': written,
            'message': f'IP {new_ip} programmed. Power cycle the HL2 to apply.',
        }

    except Exception as e:
        logger.error(f"EEPROM programming failed: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        sock.close()


def check_needs_setup(source_ip: str, config_byte: int) -> dict:
    """Check if an HL2 needs network setup and return guidance."""
    is_apipa = source_ip.startswith('169.254.')
    valid_ip = bool(config_byte & 0x80)

    if is_apipa and not valid_ip:
        return {
            'needs_setup': True,
            'reason': 'APIPA address (no DHCP lease, no fixed IP configured)',
            'hint': 'Configure a fixed IP on your LAN subnet, then power cycle.',
            'route_needed': f'ip route add 169.254.0.0/16 dev <interface>',
        }
    elif is_apipa and valid_ip:
        return {
            'needs_setup': True,
            'reason': 'APIPA address despite having a fixed IP — EEPROM may need reprogramming',
            'hint': 'The fixed IP in EEPROM may be invalid. Reprogram and power cycle.',
        }
    else:
        return {'needs_setup': False}
