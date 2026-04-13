"""
HermitSDR - Flask Application
==============================

Web UI for Hermes Lite 2 discovery, connection, and instrumentation.
Exposes REST API + WebSocket (SocketIO) for real-time telemetry.
"""

import os
import json
import time
import logging
import threading
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit

from .discovery import HL2Discovery
from .radio import RadioConnection, RadioState
from .protocol import SampleRate, hex_dump, DiscoveryReply
from .dsp import DSPPipeline, DSPConfig, ColorPalette, generate_color_palette
from .demod import Demodulator, DemodConfig, DemodMode, AUDIO_RATE
from .network_config import set_hl2_ip, HL2NetworkConfig, check_needs_setup

__version__ = '0.4.1'

# ──────────────────────────────────────────────
# App setup
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger('hermitsdr')

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'hermitsdr-dev-key')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Global state
discovery = HL2Discovery(timeout=2.0)
active_radio: RadioConnection = None
dsp_pipeline: DSPPipeline = None
demodulator: Demodulator = None
packet_log: list = []  # Last N raw packets for protocol inspector
MAX_PACKET_LOG = 100


# ──────────────────────────────────────────────
# Discovery callbacks
# ──────────────────────────────────────────────

def on_device_change(devices):
    """Push device list updates to all connected WebSocket clients."""
    device_list = [d.to_dict() for d in devices.values()]
    socketio.emit('devices_updated', {'devices': device_list})

discovery.on_device_change(on_device_change)


# ──────────────────────────────────────────────
# Routes - Pages
# ──────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', version=__version__)


# ──────────────────────────────────────────────
# Routes - REST API
# ──────────────────────────────────────────────

@app.route('/api/version')
def api_version():
    return jsonify({'version': __version__})


@app.route('/api/discover', methods=['POST'])
def api_discover():
    """Trigger a discovery sweep."""
    broadcast = request.json.get('broadcast', '255.255.255.255') if request.is_json else '255.255.255.255'
    results = discovery.discover_once(broadcast)
    return jsonify({
        'count': len(results),
        'devices': [r.to_dict() for r in results],
    })


@app.route('/api/discover/directed', methods=['POST'])
def api_discover_directed():
    """Send directed discovery to a specific IP."""
    ip = request.json.get('ip', '') if request.is_json else ''
    if not ip:
        return jsonify({'error': 'ip required'}), 400
    result = discovery.discover_directed(ip)
    if result:
        return jsonify({'device': result.to_dict()})
    return jsonify({'error': 'No reply', 'ip': ip}), 404


@app.route('/api/devices')
def api_devices():
    """List all discovered devices."""
    devices = discovery.devices
    return jsonify({
        'devices': [d.to_dict() for d in devices.values()],
    })


@app.route('/api/connect', methods=['POST'])
def api_connect():
    """Connect to a discovered radio by MAC address."""
    global active_radio, dsp_pipeline, demodulator
    mac = request.json.get('mac', '') if request.is_json else ''
    devices = discovery.devices
    if mac not in devices:
        return jsonify({'error': f'Device {mac} not found'}), 404

    if active_radio:
        if dsp_pipeline:
            dsp_pipeline.stop()
            dsp_pipeline = None
        if demodulator:
            demodulator.stop()
            demodulator = None
        active_radio.disconnect()

    device = devices[mac]
    active_radio = RadioConnection(device)

    # Create DSP pipeline
    dsp_pipeline = DSPPipeline(DSPConfig(
        sample_rate=active_radio.state.sample_rate.hz,
        center_freq=active_radio.state.frequency_hz,
    ))

    # Create demodulator
    demodulator = Demodulator(DemodConfig())

    # ── Throttled WebSocket emitters ──
    # At 192kHz, callbacks fire ~3000/sec. SocketIO can't keep up.
    # Throttle: telemetry 2fps, IQ inspector 10fps, DSP frames 30fps.

    _last_telem_emit = [0.0]   # mutable ref for closure
    _last_iq_emit = [0.0]

    def on_telemetry(t):
        now = time.monotonic()
        if now - _last_telem_emit[0] >= 0.5:  # 2 fps
            _last_telem_emit[0] = now
            socketio.emit('telemetry', t.to_dict())

    active_radio.on_telemetry(on_telemetry)

    def on_iq(i_samples, q_samples):
        # Always feed DSP pipeline (fast deque append)
        if dsp_pipeline:
            dsp_pipeline.push_iq(i_samples, q_samples)
        # Always feed demodulator
        if demodulator:
            demodulator.push_iq(i_samples, q_samples)
        # Throttle WebSocket IQ inspector to 10 fps
        now = time.monotonic()
        if now - _last_iq_emit[0] >= 0.1:
            _last_iq_emit[0] = now
            # .tolist() handles both numpy arrays and plain lists
            i_preview = list(i_samples[:8]) if hasattr(i_samples, 'tolist') else i_samples[:8]
            q_preview = list(q_samples[:8]) if hasattr(q_samples, 'tolist') else q_samples[:8]
            socketio.emit('iq_sample', {
                'i': i_preview,
                'q': q_preview,
                'count': len(i_samples),
            })

    active_radio.on_iq_data(on_iq)

    # DSP spectral frames are already throttled by the pipeline's FPS target
    def on_spectral_frame(frame):
        socketio.emit('spectral_frame', frame.to_binary())
    dsp_pipeline.on_frame(on_spectral_frame)

    # Audio PCM output — stream binary float32 chunks to browser
    _last_level_emit = [0.0]

    def on_audio(audio_bytes, level_db):
        socketio.emit('audio_pcm', audio_bytes)
        # Throttle level updates to 10fps
        now = time.monotonic()
        if now - _last_level_emit[0] >= 0.1:
            _last_level_emit[0] = now
            socketio.emit('audio_level', {
                'level_db': round(level_db, 1),
                'squelched': level_db < demodulator.config.squelch_db,
            })

    demodulator.on_audio(on_audio)

    if not active_radio.connect():
        return jsonify({'error': 'Connection failed'}), 500

    return jsonify({
        'status': 'connected',
        'device': device.to_dict(),
        'dsp': dsp_pipeline.get_stats(),
        'demod': demodulator.get_stats(),
    })


@app.route('/api/start', methods=['POST'])
def api_start():
    """Start IQ streaming, DSP pipeline, and demodulator."""
    global active_radio, dsp_pipeline, demodulator
    if not active_radio or not active_radio.state.connected:
        return jsonify({'error': 'Not connected'}), 400
    if active_radio.start_streaming():
        if dsp_pipeline:
            dsp_pipeline.start()
        if demodulator:
            demodulator.start()
        return jsonify({'status': 'streaming'})
    return jsonify({'error': 'Failed to start streaming'}), 500


@app.route('/api/stop', methods=['POST'])
def api_stop():
    """Stop IQ streaming, DSP pipeline, and demodulator."""
    global active_radio, dsp_pipeline, demodulator
    if not active_radio:
        return jsonify({'error': 'Not connected'}), 400
    if demodulator:
        demodulator.stop()
    if dsp_pipeline:
        dsp_pipeline.stop()
    active_radio.stop_streaming()
    return jsonify({'status': 'stopped'})


@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    """Disconnect from radio."""
    global active_radio, dsp_pipeline, demodulator
    if demodulator:
        demodulator.stop()
        demodulator = None
    if dsp_pipeline:
        dsp_pipeline.stop()
        dsp_pipeline = None
    if active_radio:
        active_radio.disconnect()
        active_radio = None
    return jsonify({'status': 'disconnected'})


@app.route('/api/frequency', methods=['POST'])
def api_frequency():
    """Set RX frequency in Hz."""
    global active_radio
    if not active_radio or not active_radio.state.connected:
        return jsonify({'error': 'Not connected'}), 400
    freq = request.json.get('frequency', 0) if request.is_json else 0
    if not (100000 <= freq <= 54000000):
        return jsonify({'error': 'Frequency out of range (100kHz - 54MHz)'}), 400
    active_radio.set_frequency(freq)
    # Sync DSP center frequency
    if dsp_pipeline:
        dsp_pipeline.reconfigure(center_freq=freq)
    return jsonify({'frequency': freq})


@app.route('/api/gain', methods=['POST'])
def api_gain():
    """Set LNA gain in dB (-12 to +48)."""
    global active_radio
    if not active_radio or not active_radio.state.connected:
        return jsonify({'error': 'Not connected'}), 400
    gain = request.json.get('gain', 20) if request.is_json else 20
    if not (-12 <= gain <= 48):
        return jsonify({'error': 'Gain out of range (-12 to +48 dB)'}), 400
    active_radio.set_lna_gain(gain)
    return jsonify({'gain': gain})


@app.route('/api/state')
def api_state():
    """Get current radio state, telemetry, DSP, and demod stats."""
    global active_radio, dsp_pipeline, demodulator
    if not active_radio:
        return jsonify({'connected': False})
    result = {
        'connected': active_radio.state.connected,
        'streaming': active_radio.state.streaming,
        'frequency': active_radio.state.frequency_hz,
        'sample_rate': active_radio.state.sample_rate.hz,
        'lna_gain': active_radio.state.lna_gain_db,
        'telemetry': active_radio.telemetry.to_dict(),
    }
    if dsp_pipeline:
        result['dsp'] = dsp_pipeline.get_stats()
    if demodulator:
        result['demod'] = demodulator.get_stats()
    return jsonify(result)


@app.route('/api/dsp', methods=['GET', 'POST'])
def api_dsp():
    """Get or update DSP pipeline configuration."""
    global dsp_pipeline
    if not dsp_pipeline:
        return jsonify({'error': 'DSP not initialized'}), 400
    if request.method == 'POST' and request.is_json:
        allowed = {'fft_size', 'averaging', 'peak_hold', 'peak_decay',
                    'db_min', 'db_max', 'fps_target', 'overlap', 'window'}
        updates = {k: v for k, v in request.json.items() if k in allowed}
        if 'fft_size' in updates:
            updates['fft_size'] = max(256, min(16384, int(updates['fft_size'])))
        dsp_pipeline.reconfigure(**updates)
    return jsonify(dsp_pipeline.get_stats())


@app.route('/api/palette')
def api_palette():
    """Get color palette RGB data for waterfall rendering."""
    name = request.args.get('name', 'classic')
    try:
        palette = ColorPalette(name)
    except ValueError:
        palette = ColorPalette.CLASSIC
    colors = generate_color_palette(palette)
    return jsonify({'palette': name, 'colors': colors})


@app.route('/api/demod', methods=['GET', 'POST'])
def api_demod():
    """Get or update demodulator configuration."""
    global demodulator
    if not demodulator:
        return jsonify({'error': 'Demodulator not initialized'}), 400
    if request.method == 'POST' and request.is_json:
        allowed = {'mode', 'volume', 'squelch_db', 'agc_speed', 'agc_target'}
        updates = {k: v for k, v in request.json.items() if k in allowed}
        demodulator.reconfigure(**updates)
    return jsonify(demodulator.get_stats())


@app.route('/api/demod/mode', methods=['POST'])
def api_demod_mode():
    """Set demodulation mode (usb, lsb, cw, am)."""
    global demodulator
    if not demodulator:
        return jsonify({'error': 'Demodulator not initialized'}), 400
    mode = request.json.get('mode', 'usb') if request.is_json else 'usb'
    demodulator.set_mode(mode)
    return jsonify(demodulator.get_stats())


@app.route('/api/network/config', methods=['GET'])
def api_network_config():
    """Get network config for all discovered devices."""
    devices = discovery.devices
    configs = []
    for mac, dev in devices.items():
        config = {
            'mac': mac,
            'source_ip': dev.source_ip,
            'board_name': dev.board_name,
            'is_apipa': dev.source_ip.startswith('169.254.'),
            'fixed_ip': dev.fixed_ip,
            'config_bits': dev.config_bits,
            'valid_ip': bool(dev.config_bits & 0x80),
            'favor_dhcp': bool(dev.config_bits & 0x20),
            'needs_setup': dev.source_ip.startswith('169.254.') or not (dev.config_bits & 0x80),
        }
        configs.append(config)
    return jsonify({'devices': configs})


@app.route('/api/network/set_ip', methods=['POST'])
def api_network_set_ip():
    """Program a fixed IP into an HL2's EEPROM.

    Expects JSON: {
        "current_ip": "169.254.19.221",  (or whatever the HL2's current IP is)
        "new_ip": "192.168.40.20",
        "favor_dhcp": false              (optional, default false)
    }

    Note: HL2 must be power-cycled after programming.
    """
    if not request.is_json:
        return jsonify({'error': 'JSON body required'}), 400
    current_ip = request.json.get('current_ip', '')
    new_ip = request.json.get('new_ip', '')
    favor_dhcp = request.json.get('favor_dhcp', False)
    if not current_ip or not new_ip:
        return jsonify({'error': 'current_ip and new_ip required'}), 400
    result = set_hl2_ip(current_ip, new_ip, favor_dhcp)
    status = 200 if result.get('success') else 500
    return jsonify(result), status


@app.route('/api/packet_log')
def api_packet_log():
    """Return recent raw packets for protocol debugging."""
    return jsonify({'packets': packet_log[-50:]})


# ──────────────────────────────────────────────
# WebSocket events
# ──────────────────────────────────────────────

@socketio.on('connect')
def ws_connect():
    logger.info("WebSocket client connected")
    devices = discovery.devices
    emit('devices_updated', {
        'devices': [d.to_dict() for d in devices.values()]
    })
    if active_radio and active_radio.state.connected:
        emit('radio_state', {
            'connected': True,
            'streaming': active_radio.state.streaming,
            'frequency': active_radio.state.frequency_hz,
            'sample_rate': active_radio.state.sample_rate.hz,
        })


@socketio.on('discover')
def ws_discover():
    results = discovery.discover_once()
    emit('devices_updated', {
        'devices': [r.to_dict() for r in results]
    })


@socketio.on('set_frequency')
def ws_set_frequency(data):
    freq = data.get('frequency', 0)
    if active_radio and active_radio.state.connected:
        active_radio.set_frequency(freq)
        if dsp_pipeline:
            dsp_pipeline.reconfigure(center_freq=freq)
        emit('radio_state', {'frequency': freq})


@socketio.on('set_gain')
def ws_set_gain(data):
    gain = data.get('gain', 20)
    if active_radio and active_radio.state.connected:
        active_radio.set_lna_gain(gain)
        emit('radio_state', {'lna_gain': gain})


@socketio.on('set_demod_mode')
def ws_set_demod_mode(data):
    mode = data.get('mode', 'usb')
    if demodulator:
        demodulator.set_mode(mode)
        emit('demod_state', demodulator.get_stats())


@socketio.on('set_volume')
def ws_set_volume(data):
    vol = data.get('volume', 0.7)
    if demodulator:
        demodulator.set_volume(vol)


@socketio.on('set_squelch')
def ws_set_squelch(data):
    db = data.get('squelch_db', -140)
    if demodulator:
        demodulator.set_squelch(db)


# ──────────────────────────────────────────────
# App lifecycle
# ──────────────────────────────────────────────

def start_app(host='0.0.0.0', port=5000, debug=False):
    """Start the HermitSDR application."""
    logger.info(f"HermitSDR v{__version__} starting on {host}:{port}")
    discovery.start_monitor(interval=10.0)
    try:
        socketio.run(app, host=host, port=port, debug=debug,
                     allow_unsafe_werkzeug=True)
    finally:
        discovery.stop_monitor()
        if demodulator:
            demodulator.stop()
        if dsp_pipeline:
            dsp_pipeline.stop()
        if active_radio:
            active_radio.disconnect()
