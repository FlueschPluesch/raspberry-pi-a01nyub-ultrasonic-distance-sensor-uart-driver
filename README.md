# A01NYUB Ultrasonic Distance Sensor — Raspberry Pi

Python scripts to read distance measurements from the **DFRobot A01NYUB** waterproof ultrasonic sensor over UART on a Raspberry Pi (tested on Pi 5).

## Protocol

The sensor outputs distance as 4-byte binary frames at 9600 baud (8N1):

```
FF  HIGH  LOW  CHECKSUM
```

Distance in mm = `(HIGH << 8) | LOW`. Checksum = `(0xFF + HIGH + LOW) & 0xFF`.

## Files

| File | Purpose |
|------|---------|
| `Read_A01NYUB_Ultrasonic_Distance_Sensor.py` | Standalone reader — use this in your project |
| `uart_scanner.py` | Auto-detects UART port, baudrate and protocol — useful if you don't know your sensor's settings |

## Quickstart

```bash
pip install pyserial

# Continuous output
python3 Read_A01NYUB_Ultrasonic_Distance_Sensor.py

# Single reading (e.g. in a shell script)
python3 Read_A01NYUB_Ultrasonic_Distance_Sensor.py --once

# Raw mm value only
python3 Read_A01NYUB_Ultrasonic_Distance_Sensor.py --raw --once
```

## Raspberry Pi Setup

Make sure UART is enabled and the serial console is disabled:

```bash
sudo raspi-config
# Interface Options → Serial Port → Login shell: No → Hardware: Yes
```

Connect sensor TX → Pi GPIO15 (Pin 10, RX), GND → GND.  
**The Pi's UART is 3.3 V only.** If your sensor outputs 5 V on TX, use a level shifter.

On Pi 5 the relevant port is `/dev/ttyAMA0` (GPIO header UART).
