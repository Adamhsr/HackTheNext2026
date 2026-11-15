import time
import asyncio
import threading
from collections import deque
import numpy as np
from scipy.signal import butter, lfilter
from bleak import BleakScanner, BleakClient
import dash
from dash import dcc, html
from dash.dependencies import Input, Output
import plotly.graph_objs as go

# ==========================================
# 1. HACKATHON DEVICE FILTER & BLE CONSTANTS
# ==========================================
# Enter your board's unique name/suffix (e.g., "A4F2") to lock onto your board
TARGET_OCTOPUS = ""

SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
DATA_CHAR_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"

NUM_CHANNELS = 16
SAMPLE_RATE = 250
WINDOW_SEC = 0.25           # 250ms window = snappy blink/jaw response (was 1.0s)
BUFFER_SIZE = int(SAMPLE_RATE * WINDOW_SEC)

# TI ADS131M08 conversion: Vref = 1.2V, Gain = 4, 24-bit signed -> microvolts (µV)
SCALE_TO_UV = (1.2 / 4.0 / 8388607.0) * 1e6

# Single 2D ring buffer (16 x BUFFER_SIZE) for fast vectorized NumPy math
eeg_buffer = deque(maxlen=BUFFER_SIZE)
start_time = time.time()

connection_status = "Scanning for Octopus-16 devices..."
connected_device_label = "None"
nearby_devices_str = "None yet"
lock = threading.Lock()

# ==========================================
# 2. VECTORIZED SIGNAL PROCESSING (1-50 Hz + Mean RMS)
# ==========================================
def butter_bandpass(lowcut, highcut, fs, order=4):
    nyq = 0.5 * fs
    return butter(order, [lowcut / nyq, highcut / nyq], btype='band')

b_filt, a_filt = butter_bandpass(1.0, 50.0, SAMPLE_RATE, order=4)

def compute_mean_rms_uv():
    with lock:
        if len(eeg_buffer) < 10:
            return 0.0
        # Shape: (samples, 16) -> transpose to (16, samples)
        arr = np.array(eeg_buffer, dtype=np.float64).T

    # Vectorized DC removal + bandpass filter across all 16 channels at once
    arr_detrended = arr - np.mean(arr, axis=1, keepdims=True)
    filtered = lfilter(b_filt, a_filt, arr_detrended, axis=1)

    # RMS per channel, then mean across all 16 channels
    ch_rms = np.sqrt(np.mean(np.square(filtered), axis=1))
    return round(float(np.mean(ch_rms)), 2)

# ==========================================
# 3. BLE PACKET PARSER & MULTI-DEVICE SCANNER
# ==========================================
def parse_24bit_signed(b0, b1, b2):
    val = (b0 << 16) | (b1 << 8) | b2
    if val & 0x800000:
        val -= 0x1000000
    return val

def notification_handler(sender, data: bytearray):
    if len(data) == 51 and data[0] == 0xA0 and data[50] == 0xC0:
        sample_row = [
            parse_24bit_signed(data[2 + ch * 3], data[3 + ch * 3], data[4 + ch * 3]) * SCALE_TO_UV
            for ch in range(NUM_CHANNELS)
        ]
        with lock:
            eeg_buffer.append(sample_row)

async def ble_worker():
    global connection_status, connected_device_label, nearby_devices_str
    while True:
        try:
            connection_status = "Scanning room for Octopus-16 devices..."
            discovered = await BleakScanner.discover(timeout=4.0, return_adv=True)
            octopus_matches = []

            for addr, (dev, adv) in discovered.items():
                uuids = [u.lower() for u in (adv.service_uuids or [])]
                name = dev.name or adv.local_name or "Unknown"
                if SERVICE_UUID.lower() in uuids or "octopus" in name.lower():
                    octopus_matches.append((dev, name))

            if octopus_matches:
                nearby_devices_str = " | ".join([f"{name} [{dev.address}]" for dev, name in octopus_matches])
            else:
                nearby_devices_str = "None detected in last scan"
                connection_status = "No Octopus-16 found. Retrying..."
                await asyncio.sleep(1.0)
                continue

            selected_dev = None
            selected_name = None
            for dev, name in octopus_matches:
                if not TARGET_OCTOPUS or TARGET_OCTOPUS.lower() in name.lower() or TARGET_OCTOPUS.lower() in dev.address.lower():
                    selected_dev = dev
                    selected_name = name
                    break

            if not selected_dev:
                connection_status = f"Waiting for TARGET_OCTOPUS='{TARGET_OCTOPUS}' (Found: {nearby_devices_str})"
                await asyncio.sleep(1.5)
                continue

            connection_status = f"Connecting to {selected_name}..."
            async with BleakClient(selected_dev) as client:
                connected_device_label = f"{selected_name} ({selected_dev.address})"
                connection_status = "CONNECTED & STREAMING"
                print(f"Connected to {connected_device_label}")
                await client.start_notify(DATA_CHAR_UUID, notification_handler)

                while client.is_connected:
                    await asyncio.sleep(0.5)

            connected_device_label = "None (Disconnected)"

        except Exception as e:
            connected_device_label = "None"
            connection_status = f"BLE Error ({e}). Reconnecting..."
            await asyncio.sleep(1.5)

def start_ble_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(ble_worker())

threading.Thread(target=start_ble_thread, daemon=True).start()

# ==========================================
# 4. ULTRA-SMOOTH DASHBOARD (extendData Streaming)
# ==========================================
app = dash.Dash(__name__)

# Pre-build the figure once so Dash only streams new points (no full redraw lag)
initial_fig = go.Figure(data=[
    go.Scatter(
        x=[],
        y=[],
        mode='lines',
        line=dict(color='#00FFCC', width=3),
        fill='tozeroy',
        fillcolor='rgba(0, 255, 204, 0.12)',
        name='Mean EEG RMS (µV)'
    )
])
initial_fig.update_layout(
    template='plotly_dark',
    xaxis_title='Elapsed Time (s)',
    yaxis_title='Mean EEG RMS (µV)',
    yaxis=dict(autorange=True, rangemode='tozero'),
    margin=dict(l=50, r=20, t=20, b=40),
    height=420,
    uirevision='constant'  # Prevents zoom/axis reset jitter on updates
)

app.layout = html.Div(style={'backgroundColor': '#111111', 'color': '#FFFFFF', 'padding': '20px', 'fontFamily': 'sans-serif'}, children=[
    html.H2("Octopus 16 — Live Mean EEG RMS (µV)"),

    html.Div(style={'backgroundColor': '#1E1E1E', 'padding': '12px 16px', 'borderRadius': '8px', 'marginBottom': '16px', 'borderLeft': '4px solid #00FFCC'}, children=[
        html.Div(id='connected-device-banner', style={'fontSize': '18px', 'fontWeight': 'bold', 'color': '#00FFCC'}),
        html.Div(id='ble-status', style={'fontSize': '13px', 'color': '#CCCCCC', 'marginTop': '4px'}),
        html.Div(id='nearby-devices', style={'fontSize': '12px', 'color': '#888888', 'marginTop': '4px'}),
    ]),

    html.Div(id='mean-rms-banner', style={'fontSize': '32px', 'fontWeight': 'bold', 'marginBottom': '12px', 'color': '#00FFCC'}),

    # extendData streams single points smoothly at 10 FPS without redrawing the chart
    dcc.Graph(id='rms-timeline-chart', figure=initial_fig),
    dcc.Interval(id='interval-component', interval=100, n_intervals=0)  # 10 FPS smooth updates
])

@app.callback(
    [Output('connected-device-banner', 'children'),
     Output('ble-status', 'children'),
     Output('nearby-devices', 'children'),
     Output('mean-rms-banner', 'children'),
     Output('rms-timeline-chart', 'extendData')],
    [Input('interval-component', 'n_intervals')]
)
def update_dashboard(n):
    mean_uv = compute_mean_rms_uv()
    elapsed_sec = round(time.time() - start_time, 2)

    # Stream just the new (x, y) point and keep the last 300 points (30 seconds @ 10 FPS)
    extend_payload = ({'x': [[elapsed_sec]], 'y': [[mean_uv]]}, [0], 300)

    return (
        f"Active Board: {connected_device_label}",
        f"Status: {connection_status}",
        f"Octopus Boards Detected in Room: {nearby_devices_str}",
        f"Mean EEG RMS: {mean_uv:.2f} µV",
        extend_payload
    )

if __name__ == '__main__':
    app.run(debug=False, port=8050)