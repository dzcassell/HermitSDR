"""Mock Hermes Lite 2 for testing without hardware.

Responds to discovery packets and can simulate IQ streaming.
Run standalone: python mock_hl2.py [--stream]
"""
import socket, struct, time, threading, argparse, random

METIS_SIG = b'\xef\xfe'
PORT = 1024
MAC = b'\x00\x1c\xc0\xde\xad\x42'
BOARD_ID = 0x06
GW_MAJOR = 73
GW_MINOR = 5
NUM_RX = 4

def build_discovery_reply(streaming=False):
    reply = bytearray(60)
    reply[0:2] = METIS_SIG
    reply[2] = 0x03 if streaming else 0x02
    reply[3:9] = MAC
    reply[9] = GW_MAJOR
    reply[10] = BOARD_ID
    reply[19] = NUM_RX
    reply[20] = 0x45
    reply[21] = GW_MINOR
    return bytes(reply)

def build_iq_packet(seq, freq=7074000):
    pkt = bytearray(1032)
    pkt[0:2] = METIS_SIG; pkt[2] = 0x01; pkt[3] = 0x06
    struct.pack_into('>I', pkt, 4, seq)
    for frame_start in (8, 520):
        pkt[frame_start:frame_start+3] = b'\x7f\x7f\x7f'
        # CC: cycle through addr 0,1,2
        addr = (seq * 2 + (0 if frame_start == 8 else 1)) % 3
        c0 = (addr & 0x0F) << 3
        pkt[frame_start+3] = c0
        if addr == 0:
            pkt[frame_start+7] = GW_MAJOR  # firmware version
        # Generate fake IQ: sine-ish pattern with noise
        for i in range(63):
            off = frame_start + 8 + i * 8
            phase = (seq * 63 + i) * 0.05
            iv = int(32000 * __import__('math').sin(phase) + random.randint(-500, 500))
            qv = int(32000 * __import__('math').cos(phase) + random.randint(-500, 500))
            iv = max(-8388608, min(8388607, iv)); qv = max(-8388608, min(8388607, qv))
            pkt[off] = (iv >> 16) & 0xFF; pkt[off+1] = (iv >> 8) & 0xFF; pkt[off+2] = iv & 0xFF
            pkt[off+3] = (qv >> 16) & 0xFF; pkt[off+4] = (qv >> 8) & 0xFF; pkt[off+5] = qv & 0xFF
    return bytes(pkt)

def run_mock(stream=False):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(('', PORT))
    print(f"Mock HL2 listening on UDP :{PORT} (MAC={MAC.hex(':')}, stream={stream})")

    client_addr = None
    streaming = False
    seq = 0

    def stream_loop():
        nonlocal seq, streaming
        while streaming and client_addr:
            pkt = build_iq_packet(seq); seq = (seq + 1) & 0xFFFFFFFF
            try: sock.sendto(pkt, client_addr)
            except: pass
            time.sleep(0.002625)

    while True:
        try:
            data, addr = sock.recvfrom(1500)
            if len(data) < 3 or data[0:2] != METIS_SIG: continue
            cmd = data[2]
            if cmd == 0x02:  # Discovery
                print(f"Discovery from {addr}")
                reply = build_discovery_reply(streaming)
                sock.sendto(reply, addr)
            elif cmd == 0x04:  # Start/Stop
                if data[3] & 0x01:
                    print(f"Start from {addr}")
                    client_addr = addr; streaming = True
                    threading.Thread(target=stream_loop, daemon=True).start()
                else:
                    print(f"Stop from {addr}")
                    streaming = False
            elif cmd == 0x01:  # IQ data from host (keepalive/commands)
                client_addr = addr  # update client address
        except KeyboardInterrupt:
            break
    sock.close()

if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Mock Hermes Lite 2')
    p.add_argument('--stream', action='store_true', help='Auto-stream on start')
    run_mock(**vars(p.parse_args()))
