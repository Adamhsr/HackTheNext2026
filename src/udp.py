import socket

UDP_TARGET = "164.76.8.197"
UDP_PORT = 2390

def revolution_packet(motor_id: int, revolution_count: float) -> str:
    return f"{motor_id} {revolution_count}"

def send_udp_packet(message: str, target_ip: str = UDP_TARGET, target_port: int = UDP_PORT) -> None:
    payload = message.encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(payload, (target_ip, int(target_port)))
    finally:
        sock.close()
