#!/usr/bin/env python3
"""
hl2_set_ip.py - Program a fixed IP address into a Hermes Lite 2 EEPROM
========================================================================

Uses the HPSDR Protocol 1 I2C2 EEPROM write commands to set:
  - Fixed IP address (EEPROM 0x08..0x0B)
  - Config flags: Valid IP + Favor Fixed IP over DHCP (EEPROM 0x06)

The HL2 must be power-cycled after programming for the new IP to take effect.

Protocol reference:
  https://github.com/softerhardware/Hermes-Lite2/wiki/Protocol#configuration-eeprom

  EEPROM write format (32-bit word to ADDR 0x3d):
    0x06acA0vv  where A = EEPROM address nibble, vv = value byte
    0x06 = I2C write cookie
    0xac = MCP4662 chip address (0x56 << 1)
    A0   = EEPROM address (upper nibble) + 0 (nonvolatile write)
    vv   = data byte

Usage:
    # First add a route so we can reach the HL2:
    ip route add 169.254.0.0/16 dev enp10s0

    # Then program the new IP:
    python3 hl2_set_ip.py 169.254.19.221 192.168.40.20

    # Power cycle the HL2, then verify:
    python3 hl2_set_ip.py --discover
"""

import socket
import struct
import sys
import time
import argparse

METIS_SIG = b'\xef\xfe'
PORT = 1024
SYNC = b'\x7f\x7f\x7f'


def build_discovery():
    return METIS_SIG + b'\x02' + b'\x00' * 60


def build_start(disable_watchdog=True):
    cmd = 0x01  # start IQ
    if disable_watchdog:
        cmd |= 0x80
    return METIS_SIG + b'\x04' + bytes([cmd]) + b'\x00' * 59


def build_stop():
    return METIS_SIG + b'\x04' + b'\x00' * 60


def build_iq_packet(seq, cc1_addr, cc1_data, cc2_addr=0, cc2_data=0):
    """Build a 1032-byte IQ packet with two C&C commands."""
    pkt = bytearray(1032)
    pkt[0:2] = METIS_SIG
    pkt[2] = 0x01
    pkt[3] = 0x02  # EP2
    struct.pack_into('>I', pkt, 4, seq)

    # Frame 1
    pkt[8:11] = SYNC
    c0_1 = (cc1_addr & 0x3F) << 1
    pkt[11] = c0_1
    pkt[12] = (cc1_data >> 24) & 0xFF
    pkt[13] = (cc1_data >> 16) & 0xFF
    pkt[14] = (cc1_data >> 8) & 0xFF
    pkt[15] = cc1_data & 0xFF

    # Frame 2
    pkt[520:523] = SYNC
    c0_2 = (cc2_addr & 0x3F) << 1
    pkt[523] = c0_2
    pkt[524] = (cc2_data >> 24) & 0xFF
    pkt[525] = (cc2_data >> 16) & 0xFF
    pkt[526] = (cc2_data >> 8) & 0xFF
    pkt[527] = cc2_data & 0xFF

    return bytes(pkt)


def eeprom_write_word(addr_nibble, value):
    """Build the 32-bit I2C2 EEPROM write word.

    Format: 0x06ac{A}0{vv}
      0x06 = I2C write cookie
      0xac = MCP4662 I2C address
      {A}0 = EEPROM address (upper nibble) + nonvolatile write
      {vv} = data byte
    """
    return (0x06 << 24) | (0xac << 16) | (addr_nibble << 12) | (value & 0xFF)


def discover(iface_addr=''):
    """Broadcast discovery and list all responding devices."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((iface_addr, 0))
    sock.settimeout(2.0)

    sock.sendto(build_discovery(), ('255.255.255.255', PORT))
    print("Discovery sent...")

    found = []
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(1500)
            if len(data) >= 60 and data[0:2] == METIS_SIG and data[2] in (0x02, 0x03):
                mac = ':'.join(f'{b:02x}' for b in data[3:9])
                gw_ver = f"{data[9]}.{data[21] if len(data) > 21 else 0}"
                board_id = data[10]
                status = 'STREAMING' if data[2] == 0x03 else 'IDLE'
                fixed_ip = '.'.join(str(b) for b in data[13:17])
                config = data[11]
                favor_dhcp = bool(config & 0x20)
                valid_ip = bool(config & 0x80)

                print(f"\n  Found: {'HL2' if board_id == 0x06 else f'Board 0x{board_id:02x}'}")
                print(f"  Source IP:  {addr[0]}:{addr[1]}")
                print(f"  MAC:        {mac}")
                print(f"  Gateware:   {gw_ver}")
                print(f"  Status:     {status}")
                print(f"  Config:     0x{config:02x} (valid_ip={valid_ip}, favor_dhcp={favor_dhcp})")
                print(f"  Fixed IP:   {fixed_ip}")
                found.append((addr[0], mac, board_id))
        except socket.timeout:
            break

    sock.close()
    if not found:
        print("  No devices found.")
    return found


def program_ip(hl2_ip, new_ip_str):
    """Program a fixed IP into the HL2 EEPROM via I2C2 commands."""
    octets = [int(x) for x in new_ip_str.split('.')]
    if len(octets) != 4 or not all(0 <= o <= 255 for o in octets):
        print(f"ERROR: Invalid IP address: {new_ip_str}")
        return False

    print(f"\n{'='*60}")
    print(f"  Programming HL2 at {hl2_ip}")
    print(f"  New fixed IP: {new_ip_str}")
    print(f"  Config: Valid IP=YES, Favor DHCP=NO (fixed IP preferred)")
    print(f"{'='*60}\n")

    # Build the EEPROM write commands
    # ADDR 0x3d = I2C2 bus
    I2C2_ADDR = 0x3d

    writes = [
        # EEPROM 0x08: IP octet W (192)
        ('IP.W', 0x08, octets[0]),
        # EEPROM 0x09: IP octet X (168)
        ('IP.X', 0x09, octets[1]),
        # EEPROM 0x0A: IP octet Y (40)
        ('IP.Y', 0x0A, octets[2]),
        # EEPROM 0x0B: IP octet Z (20)
        ('IP.Z', 0x0B, octets[3]),
        # EEPROM 0x06: Config = 0x80 (valid_ip=1, favor_dhcp=0)
        ('Config', 0x06, 0x80),
    ]

    # Open socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', 0))
    sock.settimeout(2.0)

    # Send start command (needed to begin protocol exchange)
    print("[1/4] Sending start command...")
    sock.sendto(build_start(disable_watchdog=True), (hl2_ip, PORT))
    time.sleep(0.1)

    # Drain any incoming packets briefly
    sock.settimeout(0.1)
    for _ in range(50):
        try:
            sock.recvfrom(2048)
        except socket.timeout:
            break

    # Send EEPROM write commands — ONE write per packet
    # The I2C bus needs ~5ms per write; sending two in one packet
    # causes the second to be dropped while the bus is busy.
    print("[2/4] Writing EEPROM values...")
    seq = 0
    for name, addr, val in writes:
        word = eeprom_write_word(addr, val)
        print(f"       {name}: EEPROM[0x{addr:02x}] = 0x{val:02x} ({val}) "
              f"→ I2C2 word: 0x{word:08x}")

        # Send write in frame 1, idle in frame 2
        pkt = build_iq_packet(seq, I2C2_ADDR, word, 0x00, 0)
        sock.sendto(pkt, (hl2_ip, PORT))
        seq += 1

        # Wait for EEPROM write to complete before next command
        time.sleep(0.050)

        # Drain incoming IQ packets to prevent buffer buildup
        try:
            while True:
                sock.recvfrom(2048)
        except socket.timeout:
            pass

    # Send a few more keepalive packets to let writes complete
    print("[3/4] Waiting for EEPROM writes to complete...")
    for _ in range(20):
        pkt = build_iq_packet(seq, 0x00, 0, 0x00, 0)  # idle C&C
        sock.sendto(pkt, (hl2_ip, PORT))
        seq += 1
        time.sleep(0.010)

    # Send stop
    print("[4/4] Sending stop command...")
    sock.sendto(build_stop(), (hl2_ip, PORT))
    time.sleep(0.1)
    sock.close()

    print(f"\n{'='*60}")
    print(f"  EEPROM programmed successfully!")
    print(f"")
    print(f"  >>> POWER CYCLE the HL2 now <<<")
    print(f"")
    print(f"  After power cycle, the HL2 should come up at {new_ip_str}")
    print(f"  Verify with: python3 {sys.argv[0]} --discover")
    print(f"{'='*60}\n")
    return True


def main():
    parser = argparse.ArgumentParser(
        description='Program a fixed IP address into a Hermes Lite 2 EEPROM',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --discover                          # Find HL2 devices on the LAN
  %(prog)s 169.254.19.221 192.168.40.20        # Set fixed IP
  %(prog)s 192.168.40.20 192.168.40.20 --verify  # Verify after power cycle
        """
    )
    parser.add_argument('hl2_ip', nargs='?', help='Current IP of the HL2')
    parser.add_argument('new_ip', nargs='?', help='New fixed IP to program')
    parser.add_argument('--discover', action='store_true', help='Discover HL2 devices')
    parser.add_argument('--verify', action='store_true', help='Discover and verify IP')

    args = parser.parse_args()

    if args.discover or args.verify:
        discover()
        return

    if not args.hl2_ip or not args.new_ip:
        parser.print_help()
        return

    program_ip(args.hl2_ip, args.new_ip)


if __name__ == '__main__':
    main()
