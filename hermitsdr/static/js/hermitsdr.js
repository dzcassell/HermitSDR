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
    if (!devices || !devices.length) {
        dl.innerHTML = '<tr class="placeholder-row"><td colspan="7">No devices discovered. Click <strong>Scan LAN</strong> to search.</td></tr>';
        return;
    }
    dl.innerHTML = devices.map(d => `<tr>
        <td>${d.board_name}</td><td>${d.mac_address}</td><td>${d.source_ip||'\u2014'}</td>
        <td>${d.gateware_version}</td><td>${d.num_receivers}</td>
        <td>${d.is_streaming?'<span style="color:var(--blue)">STREAMING</span>':'<span style="color:var(--green)">IDLE</span>'}</td>
        <td><button class="btn btn-primary btn-tiny" onclick="connectRadio('${d.mac_address}')">Connect</button></td>
    </tr>`).join('');
}

// Connect / Disconnect
const panelRadio = document.getElementById('panel-radio');
const panelTelem = document.getElementById('panel-telemetry');
const panelIQ = document.getElementById('panel-iq');

async function connectRadio(mac) {
    logMsg(`Connecting to ${mac}...`);
    try {
        const res = await fetch('/api/connect', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({mac})});
        const data = await res.json();
        if (data.status === 'connected') {
            logMsg(`Connected to ${data.device.board_name} at ${data.device.source_ip}`, 'success');
            panelRadio.classList.remove('hidden');
        } else logMsg(`Connect failed: ${data.error}`, 'error');
    } catch(e) { logMsg(`Connect error: ${e.message}`, 'error'); }
}
window.connectRadio = connectRadio;

document.getElementById('btn-disconnect').addEventListener('click', async () => {
    await fetch('/api/disconnect', {method:'POST'});
    panelRadio.classList.add('hidden'); panelTelem.classList.add('hidden'); panelIQ.classList.add('hidden');
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
        panelTelem.classList.remove('hidden'); panelIQ.classList.remove('hidden');
    } else logMsg(`Start failed: ${data.error}`, 'error');
});

document.getElementById('btn-stop').addEventListener('click', async () => {
    await fetch('/api/stop', {method:'POST'});
    logMsg('IQ stream stopped', 'warn');
    document.getElementById('btn-stop').classList.add('hidden');
    document.getElementById('btn-start').classList.remove('hidden');
});

// Frequency
function formatFreq(hz) {
    const mhz = Math.floor(hz / 1000000);
    const khz = Math.floor((hz % 1000000) / 1000);
    const r = hz % 1000;
    return `${mhz}.${String(khz).padStart(3,'0')}.${String(r).padStart(3,'0')}`;
}

document.getElementById('btn-set-freq').addEventListener('click', async () => {
    const freq = parseInt(document.getElementById('freq-input').value);
    if (isNaN(freq) || freq < 100000 || freq > 54000000) return;
    const res = await fetch('/api/frequency', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({frequency:freq})});
    const data = await res.json();
    if (data.frequency) {
        document.getElementById('freq-display').textContent = formatFreq(data.frequency);
        logMsg(`Frequency set to ${formatFreq(data.frequency)} MHz`);
    }
});

document.querySelectorAll('.freq-presets .btn-tiny').forEach(btn => {
    btn.addEventListener('click', () => {
        document.getElementById('freq-input').value = btn.dataset.freq;
        document.getElementById('btn-set-freq').click();
    });
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
    document.getElementById('telem-temp').textContent = t.temperature_raw || '--';
    document.getElementById('telem-fwd').textContent = t.forward_power_raw || '--';
    document.getElementById('telem-rev').textContent = t.reverse_power_raw || '--';
    document.getElementById('telem-current').textContent = t.current_raw || '--';
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

logMsg('HermitSDR client loaded');
logMsg('Ready for Hermes Lite 2 discovery');
