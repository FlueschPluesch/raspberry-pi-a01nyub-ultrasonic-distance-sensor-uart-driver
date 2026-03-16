from __future__ import annotations

import argparse
import platform
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

try:
	import serial
	from serial import SerialException
	from serial.tools import list_ports
except ImportError:
	print("PySerial fehlt. Installation: pip install pyserial", file=sys.stderr)
	raise SystemExit(2)


LINUX_FALLBACK_PORTS = [
	"/dev/serial0",
	"/dev/ttyAMA10",
	"/dev/ttyAMA0",
	"/dev/ttyS0",
	"/dev/ttyUSB0",
	"/dev/ttyACM0",
]

ACTIVE_PROBES = [
	("newline", b"\n"),
	("carriage-return", b"\r"),
	("question-mark", b"?"),
	("0x55", b"\x55"),
	("ASCII U", b"U"),
]

PARITY_NAMES = {
	serial.PARITY_NONE: "N",
	serial.PARITY_EVEN: "E",
	serial.PARITY_ODD: "O",
}


@dataclass(frozen=True)
class SerialConfig:
	baudrate: int
	bytesize: int
	parity: str
	stopbits: float

	@property
	def label(self) -> str:
		stopbits = int(self.stopbits) if self.stopbits in (1, 2) else self.stopbits
		return f"{self.baudrate} {self.bytesize}{PARITY_NAMES[self.parity]}{stopbits}"


@dataclass
class ScanResult:
	port: str
	config: SerialConfig
	sample: bytes
	score: int
	summary: str
	details: list[str]
	distance_values: list[int]
	triggered_by: str | None = None


def unique_preserve_order(items: Iterable[str]) -> list[str]:
	seen: set[str] = set()
	result: list[str] = []
	for item in items:
		if item and item not in seen:
			seen.add(item)
			result.append(item)
	return result


def discover_ports(explicit_ports: list[str] | None) -> list[str]:
	if explicit_ports:
		return unique_preserve_order(explicit_ports)

	discovered = [port.device for port in list_ports.comports()]
	if platform.system().lower() == "linux":
		discovered.extend(LINUX_FALLBACK_PORTS)
	return unique_preserve_order(discovered)


def build_configs(baudrates: list[int]) -> list[SerialConfig]:
	configs = [
		SerialConfig(baudrate=baudrate, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE)
		for baudrate in baudrates
	]

	for baudrate in (9600, 19200, 38400, 57600):
		if baudrate in baudrates:
			configs.append(
				SerialConfig(baudrate=baudrate, bytesize=serial.EIGHTBITS, parity=serial.PARITY_EVEN, stopbits=serial.STOPBITS_ONE)
			)
			configs.append(
				SerialConfig(baudrate=baudrate, bytesize=serial.EIGHTBITS, parity=serial.PARITY_ODD, stopbits=serial.STOPBITS_ONE)
			)

	for baudrate in (9600, 115200):
		if baudrate in baudrates:
			configs.append(
				SerialConfig(baudrate=baudrate, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_TWO)
			)

	unique_configs: list[SerialConfig] = []
	seen: set[tuple[int, int, str, float]] = set()
	for config in configs:
		key = (config.baudrate, config.bytesize, config.parity, config.stopbits)
		if key not in seen:
			seen.add(key)
			unique_configs.append(config)
	return unique_configs


def filter_configs(
	configs: list[SerialConfig],
	bytesize: int | None,
	parity: str | None,
	stopbits: float | None,
) -> list[SerialConfig]:
	filtered = configs
	if bytesize is not None:
		filtered = [config for config in filtered if config.bytesize == bytesize]
	if parity is not None:
		filtered = [config for config in filtered if config.parity == parity]
	if stopbits is not None:
		filtered = [config for config in filtered if config.stopbits == stopbits]
	return filtered


def open_serial(port: str, config: SerialConfig, timeout: float) -> serial.Serial:
	return serial.Serial(
		port=port,
		baudrate=config.baudrate,
		bytesize=config.bytesize,
		parity=config.parity,
		stopbits=config.stopbits,
		timeout=timeout,
		write_timeout=timeout,
		xonxoff=False,
		rtscts=False,
		dsrdtr=False,
	)


def collect_sample(ser: serial.Serial, duration: float, read_size: int) -> bytes:
	deadline = time.monotonic() + duration
	sample = bytearray()
	while time.monotonic() < deadline:
		waiting = ser.in_waiting if ser.in_waiting > 0 else read_size
		chunk = ser.read(waiting)
		if chunk:
			sample.extend(chunk)
			continue
		time.sleep(0.02)
	return bytes(sample)


def collect_with_optional_probes(
	ser: serial.Serial,
	duration: float,
	read_size: int,
	use_probes: bool,
	probe_wait: float,
) -> tuple[bytes, str | None]:
	sample = collect_sample(ser, duration, read_size)
	if sample or not use_probes:
		return sample, None

	for name, payload in ACTIVE_PROBES:
		ser.reset_input_buffer()
		ser.write(payload)
		ser.flush()
		time.sleep(probe_wait)
		sample = collect_sample(ser, duration, read_size)
		if sample:
			return sample, name
	return b"", None


def hex_preview(data: bytes, limit: int = 48) -> str:
	preview = " ".join(f"{byte:02X}" for byte in data[:limit])
	return preview + (" ..." if len(data) > limit else "")


def ascii_preview(data: bytes, limit: int = 96) -> str:
	chars = []
	for byte in data[:limit]:
		if byte in (9, 10, 13) or 32 <= byte <= 126:
			chars.append(chr(byte))
		else:
			chars.append(".")
	preview = "".join(chars).replace("\r", "\\r").replace("\n", "\\n")
	return preview + ("..." if len(data) > limit else "")


def printable_ratio(data: bytes) -> float:
	if not data:
		return 0.0
	printable = sum(1 for byte in data if byte in (9, 10, 13) or 32 <= byte <= 126)
	return printable / len(data)


def ascii_lines(data: bytes) -> list[str]:
	text = data.decode("ascii", errors="ignore")
	return [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]


def detect_ff_distance_frames(data: bytes) -> list[int]:
	distances: list[int] = []
	for index in range(len(data) - 3):
		frame = data[index : index + 4]
		if frame[0] != 0xFF:
			continue
		if ((frame[0] + frame[1] + frame[2]) & 0xFF) != frame[3]:
			continue
		distances.append((frame[1] << 8) | frame[2])
	return distances


def extract_ff_distance_frames(data: bytes) -> list[tuple[int, bytes]]:
	frames: list[tuple[int, bytes]] = []
	index = 0
	while index <= len(data) - 4:
		frame = data[index : index + 4]
		if frame[0] == 0xFF and ((frame[0] + frame[1] + frame[2]) & 0xFF) == frame[3]:
			distance_mm = (frame[1] << 8) | frame[2]
			frames.append((distance_mm, frame))
			index += 4
			continue
		index += 1
	return frames


def detect_fixed_spacing(data: bytes) -> tuple[int, int, int] | None:
	best: tuple[int, int, int] | None = None
	for header in range(256):
		positions = [index for index, value in enumerate(data) if value == header]
		if len(positions) < 4:
			continue

		distances = [
			later - earlier
			for earlier, later in zip(positions, positions[1:])
			if 2 <= later - earlier <= 32
		]
		if not distances:
			continue

		frame_length, count = Counter(distances).most_common(1)[0]
		if count < 3:
			continue

		candidate = (header, frame_length, count)
		if best is None or candidate[2] > best[2]:
			best = candidate
	return best


def analyze_sample(data: bytes) -> tuple[int, str, list[str], list[int]]:
	if not data:
		return 0, "keine Daten", [], []

	ratio = printable_ratio(data)
	lines = ascii_lines(data)
	numeric_lines = [line for line in lines if re.search(r"\d", line)]
	ff_distances = detect_ff_distance_frames(data)
	fixed_spacing = detect_fixed_spacing(data)

	score = len(data)
	details = [f"Bytes empfangen: {len(data)}", f"Hex-Vorschau: {hex_preview(data)}"]

	if ratio >= 0.7:
		score += 120
		details.append(f"ASCII-Vorschau: {ascii_preview(data)}")
		if numeric_lines:
			score += 50
			details.append(f"ASCII-Zeilen mit Zahlen: {numeric_lines[:5]}")
		return score, "wahrscheinlich ASCII/Text-Ausgabe", details, []

	if ff_distances:
		score += 900 + min(len(ff_distances), 30) * 8
		details.append(
			"Typisches 4-Byte-Distanzformat erkannt: 0xFF HIGH LOW CHECKSUM"
		)
		details.append(f"Distanzkandidaten (mm): {ff_distances[:10]}")
		return score, "wahrscheinlich binare Distanz-Frames", details, ff_distances

	if fixed_spacing:
		header, frame_length, count = fixed_spacing
		score += 100 + count * 8
		details.append(
			f"Wiederkehrende Frames vermutet: Header 0x{header:02X}, ungefaehre Laenge {frame_length}, Treffer {count}"
		)
		return score, "wahrscheinlich binare Frames", details, []

	if ratio >= 0.35:
		score += 40
		details.append(f"ASCII-Vorschau: {ascii_preview(data)}")
		return score, "gemischte oder teilweise lesbare Daten", details, []

	return score, "Rohdaten ohne klares Muster", details, []


def choose_live_result(results: list[ScanResult]) -> ScanResult | None:
	if not results:
		return None

	distance_results = [result for result in results if result.distance_values]
	if distance_results:
		return max(distance_results, key=lambda item: (len(item.distance_values), item.score))

	return max(results, key=lambda item: item.score)


def live_decode_distance(port: str, config: SerialConfig, timeout: float) -> int:
	print("=== Live-Dekodierung ===")
	print(f"Port: {port}")
	print(f"UART: {config.label}")
	print("Abbruch mit Ctrl+C")
	print()

	buffer = bytearray()
	try:
		with open_serial(port, config, timeout=timeout) as ser:
			ser.reset_input_buffer()
			while True:
				chunk = ser.read(ser.in_waiting if ser.in_waiting > 0 else 1)
				if not chunk:
					continue
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
					distance_cm = distance_mm / 10.0
					print(
						f"Distanz: {distance_mm:5d} mm | {distance_cm:6.1f} cm | Frame: {' '.join(f'{byte:02X}' for byte in frame)}"
					)
					del buffer[:4]
	except KeyboardInterrupt:
		print("\nLive-Dekodierung beendet.")
		return 0

	return 0


def print_scan_header(ports: list[str], configs: list[SerialConfig]) -> None:
	print(f"Gefundene Ports: {', '.join(ports)}")
	print(f"Zu testende UART-Konfigurationen: {len(configs)}")
	print()


def scan_port(
	port: str,
	configs: list[SerialConfig],
	duration: float,
	read_size: int,
	timeout: float,
	active_probes: bool,
	probe_wait: float,
) -> list[ScanResult]:
	results: list[ScanResult] = []
	print(f"=== Port {port} ===")

	for config in configs:
		try:
			with open_serial(port, config, timeout=timeout) as ser:
				ser.reset_input_buffer()
				ser.reset_output_buffer()
				sample, triggered_by = collect_with_optional_probes(
					ser=ser,
					duration=duration,
					read_size=read_size,
					use_probes=active_probes,
					probe_wait=probe_wait,
				)
		except SerialException as exc:
			print(f"  {config.label:<12} -> Fehler beim Oeffnen: {exc}")
			exc_text = str(exc).lower()
			if "could not open port" in exc_text or "permission" in exc_text or "file not found" in exc_text:
				break
			continue

		score, summary, details, distance_values = analyze_sample(sample)
		trigger_suffix = f" via Probe '{triggered_by}'" if triggered_by else ""
		print(f"  {config.label:<12} -> {summary}, {len(sample)} Bytes{trigger_suffix}")

		if sample:
			results.append(
				ScanResult(
					port=port,
					config=config,
					sample=sample,
					score=score,
					summary=summary,
					details=details,
					distance_values=distance_values,
					triggered_by=triggered_by,
				)
			)

	print()
	return results


def best_results(results: list[ScanResult], limit: int) -> list[ScanResult]:
	return sorted(results, key=lambda item: item.score, reverse=True)[:limit]


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Sucht automatisch nach UART-Daten eines unbekannten Sensors."
	)
	parser.add_argument(
		"--port",
		dest="ports",
		action="append",
		help="Konkreter Port. Mehrfach nutzbar, z. B. --port /dev/serial0 --port /dev/ttyUSB0",
	)
	parser.add_argument(
		"--baudrates",
		default="9600,19200,38400,57600,115200,230400",
		help="Kommagetrennte Baudratenliste",
	)
	parser.add_argument(
		"--bytesize",
		type=int,
		choices=[7, 8],
		help="Nur diese Datenbits testen",
	)
	parser.add_argument(
		"--parity",
		choices=["N", "E", "O", "n", "e", "o"],
		help="Nur diese Paritaet testen",
	)
	parser.add_argument(
		"--stopbits",
		type=float,
		choices=[1.0, 1.5, 2.0],
		help="Nur diese Stopbits testen",
	)
	parser.add_argument(
		"--duration",
		type=float,
		default=1.5,
		help="Messdauer pro Konfiguration in Sekunden",
	)
	parser.add_argument(
		"--timeout",
		type=float,
		default=0.15,
		help="UART-Read-Timeout in Sekunden",
	)
	parser.add_argument(
		"--read-size",
		type=int,
		default=128,
		help="Maximale Blockgroesse pro Read",
	)
	parser.add_argument(
		"--no-probes",
		action="store_true",
		help="Keine aktiven Trigger senden, nur passiv lauschen",
	)
	parser.add_argument(
		"--probe-wait",
		type=float,
		default=0.2,
		help="Wartezeit nach einer aktiven Probe in Sekunden",
	)
	parser.add_argument(
		"--top",
		type=int,
		default=5,
		help="Wie viele beste Treffer am Ende angezeigt werden",
	)
	parser.add_argument(
		"--decode-distance",
		action="store_true",
		help="Nach dem Scan die wahrscheinlichste 4-Byte-Distanzausgabe live dekodieren",
	)
	return parser.parse_args()


def parse_baudrates(raw_value: str) -> list[int]:
	baudrates: list[int] = []
	for part in raw_value.split(","):
		part = part.strip()
		if not part:
			continue
		try:
			baudrates.append(int(part))
		except ValueError:
			raise SystemExit(f"Ungueltige Baudrate: {part}") from None

	if not baudrates:
		raise SystemExit("Keine gueltigen Baudraten angegeben")
	return baudrates


def normalize_parity(value: str | None) -> str | None:
	if value is None:
		return None
	lookup = {
		"N": serial.PARITY_NONE,
		"E": serial.PARITY_EVEN,
		"O": serial.PARITY_ODD,
	}
	return lookup[value.upper()]


def normalize_stopbits(value: float | None) -> float | None:
	if value is None:
		return None
	lookup = {
		1.0: serial.STOPBITS_ONE,
		1.5: serial.STOPBITS_ONE_POINT_FIVE,
		2.0: serial.STOPBITS_TWO,
	}
	return lookup[value]


def main() -> int:
	args = parse_args()
	baudrates = parse_baudrates(args.baudrates)
	ports = discover_ports(args.ports)

	if not ports:
		print("Keine seriellen Ports gefunden.")
		print("Falls du auf dem Raspberry Pi bist, pruefe /dev/serial0 und ob UART aktiviert ist.")
		return 1

	configs = filter_configs(
		configs=build_configs(baudrates),
		bytesize=args.bytesize,
		parity=normalize_parity(args.parity),
		stopbits=normalize_stopbits(args.stopbits),
	)

	if not configs:
		print("Keine UART-Konfigurationen mehr uebrig. Bitte Filter pruefen.")
		return 1

	print_scan_header(ports, configs)

	all_results: list[ScanResult] = []
	for port in ports:
		all_results.extend(
			scan_port(
				port=port,
				configs=configs,
				duration=args.duration,
				read_size=args.read_size,
				timeout=args.timeout,
				active_probes=not args.no_probes,
				probe_wait=args.probe_wait,
			)
		)

	if not all_results:
		print("Keine verwertbaren Daten gefunden.")
		print("Pruefe Verkabelung, Pegelwandler, RX/TX-Kreuzung und ob UART auf dem Pi aktiviert ist.")
		return 2

	winners = best_results(all_results, args.top)
	print("=== Beste Treffer ===")
	for index, result in enumerate(winners, start=1):
		print(f"{index}. Port {result.port} mit {result.config.label}: {result.summary}")
		for detail in result.details:
			print(f"   - {detail}")
		if result.triggered_by:
			print(f"   - Daten erschienen erst nach Probe: {result.triggered_by}")
		print()

	best = winners[0]
	print("=== Wahrscheinlichste Lesekonfiguration ===")
	print(f"Port:        {best.port}")
	print(f"UART:        {best.config.label}")
	print(f"Einschaetung: {best.summary}")
	print()
	print("Beispiel fuer den Raspberry Pi:")
	print(
		"python3 uart_scanner.py "
		f"--port {best.port} "
		f"--baudrates {best.config.baudrate} "
		f"--bytesize {best.config.bytesize} "
		f"--parity {PARITY_NAMES[best.config.parity]} "
		f"--stopbits {float(best.config.stopbits)} "
		"--duration 3 --top 1"
	)

	if args.decode_distance:
		live_result = choose_live_result(all_results)
		if live_result is None or not live_result.distance_values:
			print()
			print("Keine eindeutigen 4-Byte-Distanzframes erkannt. Live-Dekodierung wird uebersprungen.")
			return 3
		print()
		return live_decode_distance(
			port=live_result.port,
			config=live_result.config,
			timeout=args.timeout,
		)

	return 0


if __name__ == "__main__":
	raise SystemExit(main())
