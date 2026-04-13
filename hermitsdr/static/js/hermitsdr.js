/* HermitSDR Client */
'use strict';
const socket = io();
const log = document.getElementById('event-log');

function logMsg(msg, level='info') {
    const time = new Date().toLocaleTimeString('en-US',{hour12:false});
    const line = document.createElement('div');
    line.className = 'log-line';
    line.innerHTML = `<span class="log-time">${time}</span> <span class="log-${level}">${msg}</span>`;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
    if (log.children.length > 500) log.removeChild(log.firstChild);
}

// WebSocket status
const wsStatus = document.getElementById('ws-status');
socket.on('connect', () => { wsStatus.textContent='WS: LIVE'; wsStatus.className='badge badge-connected'; logMsg('WebSocket connected','success'); });
socket.on('disconnect', () => { wsStatus.textContent='WS: LOST'; wsStatus.className='badge badge-disconnected'; logMsg('WebSocket disconnected','error'); });

// Discovery
document.getElementById('btn-discover').addEventListener('click', async function() {
    this.disabled = true; this.textContent = 'Scanning...';
    logMsg('Broadcasting discovery on UDP 1024...');
    try {
        const res = await fetch('/api/discover', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
        const data = await res.json();
        logMsg(`Discovery complete: ${data.count} device(s) found`, data.count > 0 ? 'success' : 'warn');
    } catch(e) { logMsg(`Discovery error: ${e.message}`, 'error'); }
    this.disabled = false; this.textContent = 'Scan LAN';
});

document.getElementById('btn-discover-directed').addEventListener('click', () => {
    document.getElementById('directed-input').classList.toggle('hidden');
});

document.getElementById('btn-directed-go').addEventListener('click', async () => {
    const ip = document.getElementById('directed-ip').value.trim();
    if (!ip) return;
    logMsg(`Directed discovery to ${ip}:1024`);
    try {
        const res = await fetch('/api/discover/directed', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ip})});
        const data = await res.json();
        if (data.device) logMsg(`Found ${data.device.board_name} at ${ip}`, 'success');
        else logMsg(`No reply from ${ip}`, 'warn');
    } catch(e) { logMsg(`Error: ${e.message}`, 'error'); }
});

socket.on('devices_updated', (data) => renderDevices(data.devices));

function renderDevices(devices) {
    const dl = document.getElementById('device-list');
    const alert = document.getElementById('network-alert');
    if (!devices || !devices.length) {
        dl.innerHTML = '<tr class="placeholder-row"><td colspan="7">No devices discovered. Click <strong>Scan LAN</strong> to search.</td></tr>';
        alert.classList.add('hidden');
        return;
    }
    // Check for APIPA devices
    const apipa = devices.find(d => d.is_apipa || d.needs_setup);
    if (apipa) {
        alert.classList.remove('hidden');
        document.getElementById('network-alert-msg').textContent = apipa.is_apipa
            ? `HL2 at ${apipa.source_ip} is using a link-local address — no DHCP lease and no fixed IP. Use Setup to assign a fixed IP on your LAN subnet.`
            : `HL2 at ${apipa.source_ip} may need network configuration.`;
    } else {
        alert.classList.add('hidden');
    }
    dl.innerHTML = devices.map(d => {
        const ipClass = d.is_apipa ? 'style="color:var(--orange)"' : '';
        const setupBtn = d.needs_setup
            ? `<button class="btn btn-tiny" style="color:var(--orange);border-color:var(--orange)" onclick="showNetConfig('${d.source_ip}')">Setup</button> `
            : '';
        return `<tr>
        <td>${d.board_name}</td><td>${d.mac_address}</td><td ${ipClass}>${d.source_ip||'\u2014'}</td>
        <td>${d.gateware_version}</td><td>${d.num_receivers}</td>
        <td>${d.is_streaming?'<span style="color:var(--blue)">STREAMING</span>':'<span style="color:var(--green)">IDLE</span>'}</td>
        <td>${setupBtn}<button class="btn btn-primary btn-tiny" onclick="connectRadio('${d.mac_address}')">Connect</button></td>
    </tr>`;
    }).join('');
}

// Connect / Disconnect
const panelRadio = document.getElementById('panel-radio');
const panelTelem = document.getElementById('panel-telemetry');
const panelIQ = document.getElementById('panel-iq');
const panelWf = document.getElementById('panel-waterfall');
let waterfall = null;
let audioPlayer = null;
let vfo = null;

async function connectRadio(mac) {
    logMsg(`Connecting to ${mac}...`);
    try {
        const res = await fetch('/api/connect', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({mac})});
        const data = await res.json();
        if (data.status === 'connected') {
            logMsg(`Connected to ${data.device.board_name} at ${data.device.source_ip}`, 'success');
            if (data.dsp) logMsg(`DSP: GPU=${data.dsp.gpu_available ? 'YES' : 'NO'} FFT=${data.dsp.config.fft_size}`, 'info');
            panelRadio.classList.remove('hidden');
            // Initialize VFO
            if (!vfo && window.VFO) {
                vfo = new VFO('vfo-container', socket);
                vfo.onFrequencyChange((hz) => {
                    if (waterfall) waterfall.updateCenterFreq(hz);
                    const mhz = (hz / 1e6).toFixed(6);
                    logMsg(`Tuned to ${mhz} MHz (${hz} Hz)`);
                });
                vfo.onModeChange((mode) => {
                    logMsg(`Mode → ${mode.toUpperCase()}`);
                });
            }
            // Initialize waterfall
            if (!waterfall && window.WaterfallDisplay) {
                waterfall = new WaterfallDisplay('waterfall-container', socket);
                // Route click-to-tune through VFO for step-snapping
                if (vfo) {
                    waterfall.onClickTune = (hz) => vfo.setFrequency(hz);
                    // Bind scroll-wheel tuning on waterfall canvases
                    // (slight delay to ensure canvases exist after WaterfallDisplay._build)
                    setTimeout(() => {
                        const specEl = document.getElementById('wf-spectrum');
                        const wfEl = document.getElementById('wf-waterfall');
                        if (specEl) vfo.bindScrollTarget(specEl);
                        if (wfEl) vfo.bindScrollTarget(wfEl);
                    }, 100);
                }
            }
        } else logMsg(`Connect failed: ${data.error}`, 'error');
    } catch(e) { logMsg(`Connect error: ${e.message}`, 'error'); }
}
window.connectRadio = connectRadio;

document.getElementById('btn-disconnect').addEventListener('click', async () => {
    if (audioPlayer) { audioPlayer.destroy(); audioPlayer = null; }
    await fetch('/api/disconnect', {method:'POST'});
    panelRadio.classList.add('hidden'); panelTelem.classList.add('hidden'); panelIQ.classList.add('hidden'); panelWf.classList.add('hidden');
    document.getElementById('panel-audio').classList.add('hidden');
    document.getElementById('btn-stop').classList.add('hidden');
    document.getElementById('btn-start').classList.remove('hidden');
    logMsg('Disconnected', 'warn');
});

// Start / Stop streaming
document.getElementById('btn-start').addEventListener('click', async () => {
    logMsg('Starting IQ stream...');
    const res = await fetch('/api/start', {method:'POST'});
    const data = await res.json();
    if (data.status === 'streaming') {
        logMsg('IQ stream active', 'success');
        document.getElementById('btn-start').classList.add('hidden');
        document.getElementById('btn-stop').classList.remove('hidden');
        panelTelem.classList.remove('hidden'); panelWf.classList.remove('hidden'); panelIQ.classList.remove('hidden');
        document.getElementById('panel-audio').classList.remove('hidden');
    } else logMsg(`Start failed: ${data.error}`, 'error');
});

document.getElementById('btn-stop').addEventListener('click', async () => {
    if (audioPlayer) { audioPlayer.destroy(); audioPlayer = null; }
    await fetch('/api/stop', {method:'POST'});
    logMsg('IQ stream stopped', 'warn');
    document.getElementById('btn-stop').classList.add('hidden');
    document.getElementById('btn-start').classList.remove('hidden');
    document.getElementById('panel-audio').classList.add('hidden');
});

// LNA Gain
const gainSlider = document.getElementById('gain-slider');
let gainTimeout;
gainSlider.addEventListener('input', () => {
    document.getElementById('gain-value').textContent = gainSlider.value;
    clearTimeout(gainTimeout);
    gainTimeout = setTimeout(async () => {
        await fetch('/api/gain', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({gain:parseInt(gainSlider.value)})});
        logMsg(`LNA gain set to ${gainSlider.value} dB`);
    }, 150);
});

// Telemetry
socket.on('telemetry', (t) => {
    document.getElementById('telem-fw').textContent = t.firmware_version || '--';
    document.getElementById('telem-temp').textContent = t.temperature_c ? t.temperature_c + ' °C' : '--';
    document.getElementById('telem-fwd').textContent = t.forward_power_raw || '--';
    document.getElementById('telem-rev').textContent = t.reverse_power_raw || '--';
    document.getElementById('telem-current').textContent = t.current_ma ? t.current_ma + ' mA' : '--';
    const adcEl = document.getElementById('telem-adc');
    adcEl.textContent = t.adc_overload ? 'OVL' : 'OK';
    adcEl.style.color = t.adc_overload ? 'var(--red)' : 'var(--green)';
    document.getElementById('telem-rxpkt').textContent = (t.rx_packets||0).toLocaleString();
    const seqEl = document.getElementById('telem-seqerr');
    seqEl.textContent = t.rx_sequence_errors || '0';
    seqEl.style.color = t.rx_sequence_errors > 0 ? 'var(--orange)' : 'var(--text-bright)';
});

// IQ Visualizer
const iqCanvas = document.getElementById('iq-canvas');
const iqCtx = iqCanvas.getContext('2d');
function resizeCanvas() {
    iqCanvas.width = iqCanvas.clientWidth * (window.devicePixelRatio||1);
    iqCanvas.height = iqCanvas.clientHeight * (window.devicePixelRatio||1);
    iqCtx.setTransform(window.devicePixelRatio||1, 0, 0, window.devicePixelRatio||1, 0, 0);
}
resizeCanvas(); window.addEventListener('resize', resizeCanvas);

let iqI = [], iqQ = [];
const IQ_MAX = 256;

socket.on('iq_sample', (data) => {
    iqI.push(...data.i); iqQ.push(...data.q);
    if (iqI.length > IQ_MAX) { iqI = iqI.slice(-IQ_MAX); iqQ = iqQ.slice(-IQ_MAX); }
    document.getElementById('iq-count').textContent = data.count;
    drawIQ();
});

function drawIQ() {
    const w = iqCanvas.clientWidth, h = iqCanvas.clientHeight;
    iqCtx.clearRect(0, 0, w, h);
    let mx = 1;
    for (const v of iqI) mx = Math.max(mx, Math.abs(v));
    for (const v of iqQ) mx = Math.max(mx, Math.abs(v));
    iqCtx.strokeStyle = '#1e2a38'; iqCtx.lineWidth = 0.5;
    iqCtx.beginPath(); iqCtx.moveTo(0, h/2); iqCtx.lineTo(w, h/2); iqCtx.stroke();
    drawTrace(iqI, '#00cc88', mx, w, h);
    drawTrace(iqQ, '#4488ff', mx, w, h);
}

function drawTrace(samples, color, maxVal, w, h) {
    if (samples.length < 2) return;
    iqCtx.strokeStyle = color; iqCtx.lineWidth = 1.5;
    iqCtx.beginPath();
    const step = w / (samples.length - 1);
    for (let i = 0; i < samples.length; i++) {
        const x = i * step, y = (h/2) - (samples[i]/maxVal) * (h/2 - 10);
        i === 0 ? iqCtx.moveTo(x, y) : iqCtx.lineTo(x, y);
    }
    iqCtx.stroke();
}

// Log clear
document.getElementById('btn-clear-log').addEventListener('click', () => { log.innerHTML = ''; });

// Network Config
function showNetConfig(currentIp) {
    document.getElementById('net-current-ip').value = currentIp;
    document.getElementById('net-new-ip').value = '';
    document.getElementById('net-status').classList.add('hidden');
    document.getElementById('network-config').classList.remove('hidden');
    logMsg(`Opening network config for ${currentIp}`);
}
window.showNetConfig = showNetConfig;

document.getElementById('btn-cancel-config').addEventListener('click', () => {
    document.getElementById('network-config').classList.add('hidden');
});

document.getElementById('btn-set-ip').addEventListener('click', async () => {
    const currentIp = document.getElementById('net-current-ip').value;
    const newIp = document.getElementById('net-new-ip').value.trim();
    const favorDhcp = document.getElementById('net-favor-dhcp').checked;
    const statusEl = document.getElementById('net-status');

    if (!newIp || !/^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(newIp)) {
        statusEl.textContent = 'Enter a valid IP address.';
        statusEl.className = 'net-status net-error';
        statusEl.classList.remove('hidden');
        return;
    }

    statusEl.textContent = 'Programming EEPROM...';
    statusEl.className = 'net-status net-info';
    statusEl.classList.remove('hidden');
    document.getElementById('btn-set-ip').disabled = true;
    logMsg(`Programming HL2 EEPROM: ${currentIp} → ${newIp}`);

    try {
        const res = await fetch('/api/network/set_ip', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ current_ip: currentIp, new_ip: newIp, favor_dhcp: favorDhcp })
        });
        const data = await res.json();
        if (data.success) {
            statusEl.innerHTML = `<strong>Success!</strong> IP ${newIp} written to EEPROM.<br>` +
                `<strong>Power cycle the HL2 now</strong> to apply the new address.<br>` +
                `After reboot, click Scan LAN to find it at ${newIp}.`;
            statusEl.className = 'net-status net-success';
            logMsg(`EEPROM programmed: ${newIp} — power cycle required`, 'success');
        } else {
            statusEl.textContent = `Error: ${data.error}`;
            statusEl.className = 'net-status net-error';
            logMsg(`EEPROM programming failed: ${data.error}`, 'error');
        }
    } catch (e) {
        statusEl.textContent = `Request failed: ${e.message}`;
        statusEl.className = 'net-status net-error';
        logMsg(`Network config error: ${e.message}`, 'error');
    }
    document.getElementById('btn-set-ip').disabled = false;
});

logMsg('HermitSDR client loaded');
logMsg('Ready for Hermes Lite 2 discovery');

// ── Audio Controls ──

// Enable Audio (must be triggered by user gesture for Web Audio API)
document.getElementById('btn-audio-start').addEventListener('click', () => {
    if (!audioPlayer) {
        audioPlayer = new AudioPlayer(socket);
    }
    audioPlayer.init();
    document.getElementById('btn-audio-start').classList.add('hidden');
    document.getElementById('btn-audio-mute').classList.remove('hidden');
    logMsg('Audio enabled (48kHz PCM)', 'success');
});

// Mute toggle
let audioMuted = false;
document.getElementById('btn-audio-mute').addEventListener('click', () => {
    audioMuted = !audioMuted;
    if (audioPlayer) audioPlayer.setMute(audioMuted);
    const btn = document.getElementById('btn-audio-mute');
    btn.textContent = audioMuted ? 'Unmute' : 'Mute';
    btn.classList.toggle('btn-red', audioMuted);
});

// Mode buttons (USB / LSB / CW / AM)
document.querySelectorAll('#mode-buttons .btn-mode').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('#mode-buttons .btn-mode').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const mode = btn.dataset.mode;
        socket.emit('set_demod_mode', { mode });
        logMsg(`Mode → ${mode.toUpperCase()}`);
        // Sync to VFO band memory
        if (vfo) vfo.syncMode(mode);
    });
});

// Volume slider
const volSlider = document.getElementById('vol-slider');
let volTimeout;
volSlider.addEventListener('input', () => {
    document.getElementById('vol-value').textContent = volSlider.value;
    if (audioPlayer) audioPlayer.setVolume(parseInt(volSlider.value) / 100);
    clearTimeout(volTimeout);
    volTimeout = setTimeout(() => {
        socket.emit('set_volume', { volume: parseInt(volSlider.value) / 100 });
    }, 100);
});

// Squelch slider
const squelchSlider = document.getElementById('squelch-slider');
squelchSlider.addEventListener('input', () => {
    const val = parseInt(squelchSlider.value);
    document.getElementById('squelch-value').textContent = val <= -140 ? 'OFF' : val + ' dB';
    socket.emit('set_squelch', { squelch_db: val });
});

// AGC speed slider
const agcSlider = document.getElementById('agc-slider');
agcSlider.addEventListener('input', () => {
    const val = parseInt(agcSlider.value) / 100;
    const label = val < 0.03 ? 'Slow' : val < 0.15 ? 'Medium' : 'Fast';
    document.getElementById('agc-value').textContent = label;
    fetch('/api/demod', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ agc_speed: val })
    });
});

// Audio level meter + buffer depth updates
socket.on('audio_level', (data) => {
    const db = data.level_db;
    const levelPct = Math.max(0, Math.min(100, ((db + 100) / 80) * 100));
    document.getElementById('audio-meter-bar').style.setProperty('--level', levelPct + '%');
    document.getElementById('audio-level').textContent =
        db < -150 ? '-∞ dB' : db.toFixed(1) + ' dB';
    if (data.squelched) {
        document.getElementById('audio-level').style.color = 'var(--orange)';
    } else {
        document.getElementById('audio-level').style.color = 'var(--text-dim)';
    }
});

// Buffer depth display (updated from AudioPlayer state)
setInterval(() => {
    if (audioPlayer && audioPlayer.isPlaying) {
        document.getElementById('audio-buffer').textContent =
            'buf: ' + (audioPlayer.bufferDepth || 0) + 'ms';
    }
}, 500);
