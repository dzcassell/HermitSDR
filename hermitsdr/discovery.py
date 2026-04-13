"""
HermitSDR - Hermes Lite 2 Discovery
====================================

UDP broadcast discovery on port 1024. Supports both broadcast and
directed (unicast) discovery.

The HL2 uses DHCP by default. If no DHCP server is present, it
self-assigns 169.254.19.221. Discovery finds it regardless.
"""

import socket
import select
import logging
import time
import threading
from typing import List, Optional, Callable

from .protocol import (
    METIS_PORT,
    build_discovery_packet,
    parse_discovery_reply,
    DiscoveryReply,
)

logger = logging.getLogger(__name__)


class HL2Discovery:
    """Discover Hermes Lite 2 devices on the LAN via UDP broadcast."""

    def __init__(self, timeout: float = 2.0, bind_addr: str = ''):
        """
        Args:
            timeout: Seconds to wait for discovery replies
            bind_addr: Interface IP to bind to ('' = all interfaces)
        """
        self.timeout = timeout
        self.bind_addr = bind_addr
        self._devices: dict[str, DiscoveryReply] = {}  # keyed by MAC
        self._lock = threading.Lock()
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._callbacks: list[Callable] = []

    @property
    def devices(self) -> dict[str, DiscoveryReply]:
        with self._lock:
            return dict(self._devices)

    def discover_once(self, broadcast_addr: str = '255.255.255.255') -> List[DiscoveryReply]:
        """Run a single discovery sweep.

        Sends a broadcast discovery packet and collects all replies
        within the timeout window.

        Returns:
            List of DiscoveryReply objects found
        """
        found = []
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.bind_addr, 0))
            sock.settimeout(self.timeout)

            # Send discovery
            pkt = build_discovery_packet()
            sock.sendto(pkt, (broadcast_addr, METIS_PORT))
            logger.info(f"Discovery sent to {broadcast_addr}:{METIS_PORT} "
                        f"({len(pkt)} bytes)")

            # Collect replies
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                ready, _, _ = select.select([sock], [], [], remaining)
                if not ready:
                    break
                try:
                    data, addr = sock.recvfrom(1500)
                    reply = parse_discovery_reply(data, addr)
                    if reply is not None:
                        logger.info(
                            f"Discovered {reply.board_name} at {addr[0]} "
                            f"MAC={reply.mac_address} FW={reply.firmware_version} "
                            f"RX={reply.num_hw_receivers} "
                            f"{'STREAMING' if reply.is_streaming else 'IDLE'}"
                        )
                        found.append(reply)
                        with self._lock:
                            self._devices[reply.mac_address] = reply
                except socket.timeout:
                    break
                except Exception as e:
                    logger.warning(f"Error receiving discovery reply: {e}")
        except Exception as e:
            logger.error(f"Discovery error: {e}")
        finally:
            sock.close()

        return found

    def discover_directed(self, ip: str) -> Optional[DiscoveryReply]:
        """Send a directed (unicast) discovery to a specific IP.

        Useful when the HL2 is on a different subnet or when broadcast
        is unavailable (e.g., across VLANs).
        """
        results = []
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.bind_addr, 0))
            sock.settimeout(self.timeout)

            pkt = build_discovery_packet()
            sock.sendto(pkt, (ip, METIS_PORT))
            logger.info(f"Directed discovery sent to {ip}:{METIS_PORT}")

            try:
                data, addr = sock.recvfrom(1500)
                reply = parse_discovery_reply(data, addr)
                if reply is not None:
                    with self._lock:
                        self._devices[reply.mac_address] = reply
                    return reply
            except socket.timeout:
                logger.info(f"No reply from {ip}")
        except Exception as e:
            logger.error(f"Directed discovery error: {e}")
        finally:
            sock.close()

        return None

    def on_device_change(self, callback: Callable):
        """Register a callback for device list changes.

        Callback signature: callback(devices: dict[str, DiscoveryReply])
        """
        self._callbacks.append(callback)

    def _notify_callbacks(self):
        devices = self.devices
        for cb in self._callbacks:
            try:
                cb(devices)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def start_monitor(self, interval: float = 5.0,
                      broadcast_addr: str = '255.255.255.255'):
        """Start periodic discovery in a background thread.

        Args:
            interval: Seconds between discovery sweeps
            broadcast_addr: Broadcast address to use
        """
        if self._running:
            return
        self._running = True

        def _monitor_loop():
            while self._running:
                old_macs = set(self._devices.keys())
                self.discover_once(broadcast_addr)
                new_macs = set(self._devices.keys())
                if old_macs != new_macs:
                    self._notify_callbacks()
                time.sleep(interval)

        self._monitor_thread = threading.Thread(
            target=_monitor_loop, daemon=True, name="hl2-discovery"
        )
        self._monitor_thread.start()
        logger.info(f"Discovery monitor started (interval={interval}s)")

    def stop_monitor(self):
        """Stop the background discovery monitor."""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5.0)
            self._monitor_thread = None
            logger.info("Discovery monitor stopped")

    def clear(self):
        """Clear all discovered devices."""
        with self._lock:
            self._devices.clear()
