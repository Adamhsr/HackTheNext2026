import gpype as gp
import udp

if __name__ == "__main__":
    udp.send_udp_packet(udp.revolution_packet(0, -1.0))
    print("Sent")
