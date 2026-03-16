from __future__ import annotations

import argparse
import sys
import time

try:
	import serial
except ImportError:
	print("PySerial fehlt. Installation: pip install pyserial", file=sys.stderr)
	raise SystemExit(2)


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Liest Distanzwerte vom UART-Ultraschallsensor A01NYUB."
	)
	parser.add_argument("--port", default="/dev/ttyAMA0", help="Serieller Port")
	parser.add_argument("--baudrate", type=int, default=9600, help="UART-Baudrate")
	parser.add_argument("--timeout", type=float, default=0.2, help="Read-Timeout in Sekunden")
	parser.add_argument(
		"--once",
		action="store_true",
		help="Nur einen gueltigen Messwert lesen und danach beenden",
	)
	parser.add_argument(
		"--raw",
		action="store_true",
		help="Nur den numerischen Distanzwert in mm ausgeben (kein Header, kein Frame)",
	)
	return parser.parse_args()


def open_sensor(port: str, baudrate: int, timeout: float) -> serial.Serial:
	return serial.Serial(
		port=port,
		baudrate=baudrate,
		bytesize=serial.EIGHTBITS,
		parity=serial.PARITY_NONE,
		stopbits=serial.STOPBITS_ONE,
		timeout=timeout,
		xonxoff=False,
		rtscts=False,
		dsrdtr=False,
	)


def read_distance_frame(ser: serial.Serial, buffer: bytearray) -> tuple[int, bytes] | None:
	chunk = ser.read(ser.in_waiting if ser.in_waiting > 0 else 1)
	if chunk:
		buffer.extend(chunk)

	while len(buffer) >= 4:
		if buffer[0] != 0xFF:
			buffer.pop(0)
			continue

		frame = bytes(buffer[:4])
		checksum = (frame[0] + frame[1] + frame[2]) & 0xFF
		if checksum != frame[3]:
			buffer.pop(0)
			continue

		distance_mm = (frame[1] << 8) | frame[2]
		del buffer[:4]
		return distance_mm, frame

	return None


def main() -> int:
	args = parse_args()
	buffer = bytearray()

	if not args.raw:
		print(f"Verbinde mit {args.port} @ {args.baudrate} 8N1")
		print("Abbruch mit Ctrl+C")

	try:
		with open_sensor(args.port, args.baudrate, args.timeout) as ser:
			ser.reset_input_buffer()
			while True:
				result = read_distance_frame(ser, buffer)
				if result is None:
					time.sleep(0.01)
					continue

				distance_mm, frame = result
				if args.raw:
					print(distance_mm)
				else:
					distance_cm = distance_mm / 10.0
					print(
						f"Distanz: {distance_mm:5d} mm | {distance_cm:6.1f} cm | Frame: {' '.join(f'{byte:02X}' for byte in frame)}"
					)
				if args.once:
					return 0
	except KeyboardInterrupt:
		print("\nBeendet.")
		return 0
	except serial.SerialException as exc:
		print(f"Serieller Fehler: {exc}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
