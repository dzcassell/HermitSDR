<p align="center">
  <img src="hermitsdr_logo.png" alt="HermitSDR Logo" width="300">
</p>
# HermitSDR

A GPU-accelerated web client for Hermes SDR compatible radios.

**Phase 1**: Device discovery, connection management, live telemetry, IQ inspection, and protocol test harnesses for Hermes Lite 2 development.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  Browser (any device on LAN)                         │
│  ┌────────────────────────────────────────────────┐  │
│  │  Discovery UI │ Radio Control │ IQ Inspector   │  │
│  │  Telemetry    │ Waterfall (Phase 2)            │  │
│  └──────────────────┬─────────────────────────────┘  │
│                     │ WebSocket + REST                │
└─────────────────────┼────────────────────────────────┘
                      │
┌─────────────────────┼────────────────────────────────┐
│  Docker Container   │  (NVIDIA GPU + host network)   │
│  ┌──────────────────┴─────────────────────────────┐  │
│  │  Flask + SocketIO (port 5000)                  │  │
│  │  ┌─────────────┐  ┌────────────────────────┐   │  │
│  │  │ Discovery   │  │ Radio Connection Mgr   │   │  │
│  │  │ UDP bcast   │  │ IQ stream / C&C / telem│   │  │
│  │  └─────────────┘  └──────────┬─────────────┘   │  │
│  │                              │                  │  │
│  │  ┌───────────────────────────┴──────────────┐   │  │
│  │  │  CuPy FFT Pipeline (RTX 5070 Ti)        │   │  │
│  │  │  GPU-accelerated spectral processing     │   │  │
│  │  └──────────────────────────────────────────┘   │  │
│  └─────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
                      │ UDP :1024
┌─────────────────────┼────────────────────────────────┐
│  Hermes Lite 2      │  HPSDR Protocol 1              │
│  76.8 MHz ADC → IQ samples @ 192 kHz                │
│  HF coverage: 0 - 38.4 MHz                          │
└──────────────────────────────────────────────────────┘
```

## Quick Start

```bash
# Clone
git clone https://github.com/dzcassell/HermitSDR.git
cd HermitSDR

# Build and run (requires nvidia-container-toolkit)
docker compose up -d

# Open UI
# http://<your-host-ip>:5000
```

### Prerequisites

- Docker with `nvidia-container-toolkit` installed
- NVIDIA GPU (tested: RTX 5070 Ti 16GB)
- Hermes Lite 2 on the same LAN segment

### Network Bootstrapping

The HL2 uses DHCP by default. If no DHCP server is present (or the lease fails), it falls back to a link-local address (`169.254.19.221`). This causes routing issues since your host is likely on a different subnet (e.g., `192.168.x.x`).

**Option 1: Web UI (recommended)**

1. Start HermitSDR — discovery uses UDP broadcast and will find the HL2 regardless of its IP
2. If the HL2 shows an orange IP (169.254.x.x), click the **Setup** button next to it
3. Enter your desired fixed IP (e.g., `192.168.40.20`) and click **Program EEPROM**
4. Power cycle the HL2 — it will come up at the new address

> **Note:** If the HL2 is on a link-local address, you may need to add a route on the Docker host first:
> ```bash
> ip route add 169.254.0.0/16 dev <your-interface>
> ```

**Option 2: CLI tool**

```bash
# Add route to reach the HL2
ip route add 169.254.0.0/16 dev enp10s0

# Discover devices
python3 tools/hl2_set_ip.py --discover

# Program fixed IP
python3 tools/hl2_set_ip.py 169.254.19.221 192.168.40.20

# Power cycle the HL2, then verify
python3 tools/hl2_set_ip.py --discover
```

**Option 3: DHCP reservation**

Assign a static lease in your router/DHCP server for the HL2's MAC address (starts with `00:1c:c0`).

## HPSDR Protocol 1 Reference

This implementation targets the [Hermes Lite 2 Protocol](https://github.com/softerhardware/Hermes-Lite2/wiki/Protocol), which is based on openHPSDR Protocol 1.

### Discovery

| Packet | Size | Format |
|--------|------|--------|
| Discovery request | 63 bytes | `0xEFFE 0x02` + 60×`0x00` |
| Discovery reply | 60 bytes | `0xEFFE` + status + MAC + gateware + board_id + extensions |
| Start command | 63 bytes | `0xEFFE 0x04` + command byte + 60×`0x00` |
| Stop command | 63 bytes | `0xEFFE 0x04 0x00` + 60×`0x00` |

- **Board ID**: `0x06` = Hermes Lite 2
- **Status**: `0x02` = idle, `0x03` = streaming
- **Start command bits**: `[0]` = IQ, `[1]` = wideband, `[7]` = disable watchdog

### IQ Data Framing

Each UDP packet is **1032 bytes**:

```
[0:1]    0xEFFE signature
[2]      0x01 packet type
[3]      0x06 endpoint (EP6 = IQ from radio)
[4:7]    Sequence number (big-endian u32)
[8:519]  USB Frame 1 (512 bytes)
[520:1031] USB Frame 2 (512 bytes)
```

Each 512-byte USB frame:

```
[0:2]    Sync: 0x7F7F7F
[3:7]    C0..C4 (command & control)
[8:511]  63 × (I₂I₁I₀ Q₂Q₁Q₀ M₁M₀) = 504 bytes
         I,Q = 24-bit signed big-endian
         M = 16-bit mic (unused on HL2)
```

### Sample Rates

| Encoding | Rate |
|----------|------|
| `0b00` | 48 kHz |
| `0b01` | 96 kHz |
| `0b10` | 192 kHz |
| `0b11` | 384 kHz |

### Key Registers (C&C ADDR)

| ADDR | Function |
|------|----------|
| `0x00` | Sample rate, num receivers, duplex |
| `0x01` | TX NCO frequency (Hz) |
| `0x02` | RX1 NCO frequency (Hz) |
| `0x09` | TX drive, PA control, VNA mode |
| `0x0A` | LNA gain (-12 to +48 dB) |

## Development

### Run tests

```bash
cd HermitSDR
pip install pytest
pytest tests/ -v
```

### Mock HL2 (no hardware needed)

```bash
# Terminal 1: Start mock radio
python tests/mock_hl2.py

# Terminal 2: Start HermitSDR
python -m hermitsdr --debug
```

### Project Structure

```
HermitSDR/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── INSTALL.sh
├── hermitsdr/
│   ├── __init__.py
│   ├── __main__.py        # CLI entry point
│   ├── app.py             # Flask + SocketIO web app
│   ├── discovery.py       # UDP broadcast discovery
│   ├── protocol.py        # HPSDR Protocol 1 encoding/decoding
│   ├── radio.py           # Radio connection + IQ stream manager
│   ├── static/
│   │   ├── css/hermitsdr.css
│   │   └── js/hermitsdr.js
│   └── templates/
│       └── index.html
└── tests/
    ├── test_protocol.py   # Protocol unit tests
    └── mock_hl2.py        # Mock HL2 for testing
```

## Roadmap

- [x] **Phase 1**: Discovery, connection, telemetry, IQ inspector, protocol harness
- [ ] **Phase 2**: GPU-accelerated waterfall (CuPy FFT → WebGL/Canvas rendering)
- [ ] **Phase 3**: Multi-receiver support, wideband bandscope
- [ ] **Phase 4**: Demodulation, audio output, filter controls

## License

Apache License 2.0
