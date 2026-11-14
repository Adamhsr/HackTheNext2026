
import argparse
import socket

import gpype as gp


def send_udp_test_packet(target_ip: str, target_port: int = 2390, message: str = "hello from gpype") -> None:
    """Send a tiny UDP packet to a remote device, such as an Arduino WiFi board."""
    payload = message.encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(payload, (target_ip, int(target_port)))
    finally:
        sock.close()


def run_gpype_demo() -> None:
    # Create main app and pipeline
    app = gp.MainApp()
    p = gp.Pipeline()

    # Create signal source and scope
    source = gp.Generator(channel_count=8,
                          signal_frequency=0.4,
                          signal_shape=gp.Generator.SHAPE_SINUSOID,
                          signal_amplitude=25,
                          noise_amplitude=10)

    scope = gp.TimeSeriesScope(time_window=10,
                               amplitude_limit=50,
                               hidden_channels=[])

    # Connect nodes
    p.connect(source, scope)

    # Add widget to main app
    app.add_widget(scope)

    # Start pipeline and run application
    p.start()
    app.run()  # blocking until window is closed
    p.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GPype demo and Arduino UDP test sender")
    parser.add_argument("--udp-target", help="Arduino IP address to send a UDP test packet to")
    parser.add_argument("--udp-port", type=int, default=2390, help="Port to send the UDP packet to")
    parser.add_argument("--udp-message", default="hello from gpype", help="Message to send in the UDP packet")
    args = parser.parse_args()

    if args.udp_target:
        message = args.udp_message or "hello from gpype"
        send_udp_test_packet(args.udp_target, args.udp_port, message)
    else:
        run_gpype_demo()
