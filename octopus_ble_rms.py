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
# 1. GESTURE THRESHOLDS & DISPLAY CONSTANTS (µV)
# ==========================================
BASELINE_UV = 30.0          # Resting baseline reference (< 30 µV)
FIST_REF_UV = 300.0        # Wrist tense / fist reference (~50 µV)
ARM_REF_UV = 500.0          # Full arm flex reference (~100 µV)

FIST_MIN_UV = 150.0       # Fist LED turns ON when: FIST_MIN_UV <= RMS < FIST_MAX_UV
FIST_MAX_UV = 400.0
ARM_MIN_UV = 400.0          # Full Arm LED turns ON when: RMS >= ARM_MIN_UV

Y_AXIS_MAX_UV = 50000.0     # Hard cap for the graph's Y-axis (prevents 15k+ spikes from squashing history)
CHANNEL_ARTIFACT_UV = 2000.0 # Ignore individual floating/loose pins above this value when averaging

# ==========================================
# 2. HACKATHON DEVICE FILTER & BLE CONSTANTS
# ==========================================
# Enter your board's unique name/suffix (e.g., "A4F2") to lock onto your board
TARGET_OCTOPUS = ""

SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
DATA_CHAR_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"

NUM_CHANNELS = 16
SAMPLE_RATE = 250
WINDOW_SEC = 0.25           # 250ms window for snappy muscle/gesture response
BUFFER_SIZE = int(SAMPLE_RATE * WINDOW_SEC)

# TI ADS131M08 conversion: Vref = 1.2V, Gain = 4, 24-bit signed -> microvolts (µV)
SCALE_TO_UV = (1.2 / 4.0 / 8388607.0) * 1e6

eeg_buffer = deque(maxlen=BUFFER_SIZE)
start_time = time.time()

connection_status = "Scanning for Octopus-16 devices..."
connected_device_label = "None"
nearby_devices_str = "None yet"
lock = threading.Lock()

# ==========================================
# 3. VECTORIZED SIGNAL PROCESSING (1-50 Hz + Artifact-Rejected Mean RMS)
# ==========================================
def butter_bandpass(lowcut, highcut, fs, order=4):
    nyq = 0.5 * fs
    return butter(order, [lowcut / nyq, highcut / nyq], btype='band')

b_filt, a_filt = butter_bandpass(1.0, 50.0, SAMPLE_RATE, order=4)

def compute_mean_rms_uv():
    with lock:
        if len(eeg_buffer) < 10:
            return 0.0
        arr = np.array(eeg_buffer, dtype=np.float64).T

    arr_detrended = arr - np.mean(arr, axis=1, keepdims=True)
    filtered = lfilter(b_filt, a_filt, arr_detrended, axis=1)
    ch_rms = np.sqrt(np.mean(np.square(filtered), axis=1))

    # Filter out individual floating/disconnected pins (> CHANNEL_ARTIFACT_UV)
    valid_channels = ch_rms[ch_rms < CHANNEL_ARTIFACT_UV]
    if len(valid_channels) > 0:
        mean_val = float(np.mean(valid_channels))
    else:
        # If all pins lose contact at once, clamp at Y_AXIS_MAX_UV
        mean_val = min(float(np.mean(ch_rms)), Y_AXIS_MAX_UV)

    return round(mean_val, 2)

# ==========================================
# 4. BLE PACKET PARSER & MULTI-DEVICE SCANNER
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
# 5. DASHBOARD WITH DIGITAL GESTURE LEDs
# ==========================================
app = dash.Dash(__name__)

def make_led_style(is_on, on_color):
    return {
        'width': '28px',
        'height': '28px',
        'borderRadius': '50%',
        'backgroundColor': on_color if is_on else '#2A2A2A',
        'boxShadow': f'0 0 18px {on_color}, inset 0 0 6px #FFFFFF' if is_on else 'inset 0 2px 5px #000000',
        'border': f'2px solid {on_color if is_on else "#444444"}',
        'transition': 'all 0.08s ease-out',
        'marginRight': '14px',
        'flexShrink': '0'
    }

initial_fig = go.Figure(data=[
    go.Scatter(
        x=[],
        y=[],
        mode='lines',
        line=dict(color='#00FFCC', width=3),
        fill='tozeroy',
        fillcolor='rgba(0, 255, 204, 0.12)',
        name='Mean RMS (µV)'
    )
])

initial_fig.add_hline(y=BASELINE_UV, line_dash="dot", line_color="#666666", annotation_text=f"Baseline (~{BASELINE_UV:.0f} µV)")
initial_fig.add_hline(y=FIST_MIN_UV, line_dash="dash", line_color="#00FF66", annotation_text=f"Fist Min ({FIST_MIN_UV:.0f} µV)")
initial_fig.add_hline(y=ARM_MIN_UV, line_dash="dash", line_color="#FF3366", annotation_text=f"Full Arm ({ARM_MIN_UV:.0f}+ µV)")

initial_fig.update_layout(
template='plotly_dark',
    xaxis_title='Elapsed Time (s)',
    yaxis_title='Mean RMS (µV)',
    yaxis=dict(autorange=True, rangemode='tozero'),
    margin=dict(l=50, r=20, t=20, b=40),
    height=380,
    uirevision='constant'
)

app.layout = html.Div(style={'backgroundColor': '#111111', 'color': '#FFFFFF', 'padding': '20px', 'fontFamily': 'sans-serif'}, children=[
    html.H2("Octopus 16 — Arm Muscle & Gesture LED Dashboard"),

    # Connection Banner
    html.Div(style={'backgroundColor': '#1E1E1E', 'padding': '12px 16px', 'borderRadius': '8px', 'marginBottom': '16px', 'borderLeft': '4px solid #00FFCC'}, children=[
        html.Div(id='connected-device-banner', style={'fontSize': '16px', 'fontWeight': 'bold', 'color': '#00FFCC'}),
        html.Div(id='ble-status', style={'fontSize': '13px', 'color': '#CCCCCC', 'marginTop': '4px'}),
        html.Div(id='nearby-devices', style={'fontSize': '12px', 'color': '#888888', 'marginTop': '4px'}),
    ]),

    # Live RMS Value + Digital LEDs Panel
    html.Div(style={'display': 'flex', 'flexWrap': 'wrap', 'alignItems': 'center', 'gap': '20px', 'marginBottom': '16px'}, children=[
        html.Div(style={'backgroundColor': '#1E1E1E', 'padding': '16px 24px', 'borderRadius': '8px', 'minWidth': '230px'}, children=[
            html.Div("LIVE MEAN RMS", style={'fontSize': '12px', 'color': '#AAAAAA', 'letterSpacing': '1px'}),
            html.Div(id='mean-rms-banner', style={'fontSize': '34px', 'fontWeight': 'bold', 'color': '#00FFCC', 'marginTop': '4px'})
        ]),

        # LED 1: Fist Detection (40 - 70 µV)
        html.Div(style={'backgroundColor': '#1E1E1E', 'padding': '16px 24px', 'borderRadius': '8px', 'display': 'flex', 'alignItems': 'center', 'minWidth': '240px'}, children=[
            html.Div(id='led-fist', style=make_led_style(False, '#00FF66')),
            html.Div([
                html.Div("FIST / WRIST TENSE", style={'fontSize': '15px', 'fontWeight': 'bold'}),
                html.Div(f"Range: {FIST_MIN_UV:.0f} – {FIST_MAX_UV:.0f} µV (Ref ~{FIST_REF_UV:.0f})", style={'fontSize': '12px', 'color': '#AAAAAA'})
            ])
        ]),

        # LED 2: Full Arm Flex (70+ µV)
        html.Div(style={'backgroundColor': '#1E1E1E', 'padding': '16px 24px', 'borderRadius': '8px', 'display': 'flex', 'alignItems': 'center', 'minWidth': '240px'}, children=[
            html.Div(id='led-arm', style=make_led_style(False, '#FF3366')),
            html.Div([
                html.Div("FULL ARM FLEX", style={'fontSize': '15px', 'fontWeight': 'bold'}),
                html.Div(f"Range: {ARM_MIN_UV:.0f}+ µV (Ref ~{ARM_REF_UV:.0f})", style={'fontSize': '12px', 'color': '#AAAAAA'})
            ])
        ])
    ]),

    # Streaming Timeline Chart
    dcc.Graph(id='rms-timeline-chart', figure=initial_fig),
    dcc.Interval(id='interval-component', interval=100, n_intervals=0)
])

@app.callback(
    [Output('connected-device-banner', 'children'),
     Output('ble-status', 'children'),
     Output('nearby-devices', 'children'),
     Output('mean-rms-banner', 'children'),
     Output('led-fist', 'style'),
     Output('led-arm', 'style'),
     Output('rms-timeline-chart', 'extendData')],
    [Input('interval-component', 'n_intervals')]
)
def update_dashboard(n):
    mean_uv = compute_mean_rms_uv()
    elapsed_sec = round(time.time() - start_time, 2)

    fist_active = FIST_MIN_UV <= mean_uv < FIST_MAX_UV
    arm_active = mean_uv >= ARM_MIN_UV

    fist_style = make_led_style(fist_active, '#00FF66')
    arm_style = make_led_style(arm_active, '#FF3366')

    # Also clamp plotted y-value at Y_AXIS_MAX_UV so the line stays inside the 10k ceiling
    plotted_uv = min(mean_uv, Y_AXIS_MAX_UV)
    extend_payload = ({'x': [[elapsed_sec]], 'y': [[plotted_uv]]}, [0], 300)

    return (
        f"Active Board: {connected_device_label}",
        f"Status: {connection_status}",
        f"Octopus Boards Detected in Room: {nearby_devices_str}",
        f"{mean_uv:.2f} µV",
        fist_style,
        arm_style,
        extend_payload
    )

if __name__ == '__main__':
    app.run(debug=False, port=8050)