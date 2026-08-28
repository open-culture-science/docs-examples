#!/usr/bin/env python3
"""Read CO2, temperature, and humidity from the sensor's USB serial port.

The sensor sends one line of comma-separated values per second:

    co2,temperature_1,temperature_2,humidity

This script reads those lines, prints the values, and optionally saves them
to a CSV file.

Note: CO2 is reported as **percent by volume, not ppm**. For example,
0.04 means about 400 ppm (roughly the CO2 level in outdoor air).

Usage:
    python3 read_co2_sensor.py                     # auto-detect the port, print forever
    python3 read_co2_sensor.py --port /dev/ttyACM0  # use a specific port
    python3 read_co2_sensor.py --out readings.csv   # also save readings to a file
"""

import argparse
import csv
import sys
from datetime import datetime

import serial
from serial.tools import list_ports

BAUD_RATE = 115200
CANDIDATE_PORTS = ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0"]


def open_port(path: str) -> serial.Serial:
    # DTR/RTS are turned off before opening so the board doesn't reboot
    # when the connection opens (common on USB microcontroller boards).
    ser = serial.Serial()
    ser.port = path
    ser.baudrate = BAUD_RATE
    ser.timeout = 2.0
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def parse_line(line: str):
    """Turn one line of text into (co2_pct, temperature_c, humidity_pct),
    or return None if the line isn't a valid reading."""
    parts = line.strip().split(",")
    if len(parts) != 4:
        return None
    try:
        co2, temp_1, temp_2, humidity = (float(p) for p in parts)
    except ValueError:
        return None
    temperature_c = (temp_1 + temp_2) / 2
    return co2, temperature_c, humidity


def find_port() -> str:
    """Try each likely port and use the first one that sends a valid reading."""
    candidates = list(CANDIDATE_PORTS)
    for info in list_ports.comports():
        if info.device not in candidates:
            candidates.append(info.device)

    for path in candidates:
        try:
            ser = open_port(path)
        except (OSError, serial.SerialException):
            continue
        try:
            for _ in range(20):
                line = ser.readline().decode("ascii", errors="replace")
                if parse_line(line) is not None:
                    return path
        finally:
            ser.close()

    raise SystemExit(
        f"Couldn't find the sensor. Tried: {candidates}\n"
        "Pass the correct port with --port."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="Serial port (default: auto-detect)")
    parser.add_argument("--out", help="Also save readings to this CSV file")
    args = parser.parse_args()

    port = args.port or find_port()
    print(f"Reading from {port} (Ctrl-C to stop)")

    csv_file = open(args.out, "a", newline="") if args.out else None
    csv_writer = csv.writer(csv_file) if csv_file else None
    if csv_writer and csv_file.tell() == 0:
        csv_writer.writerow(["timestamp", "co2_pct", "temperature_c", "humidity_pct"])

    ser = open_port(port)
    try:
        while True:
            line = ser.readline().decode("ascii", errors="replace")
            reading = parse_line(line)
            if reading is None:
                continue  # skip blank/garbled lines
            co2, temperature_c, humidity = reading
            timestamp = datetime.now().isoformat(timespec="seconds")
            print(f"{timestamp}  CO2: {co2:.3f}%  Temp: {temperature_c:.1f}C  Humidity: {humidity:.1f}%")
            if csv_writer:
                csv_writer.writerow([timestamp, co2, temperature_c, humidity])
                csv_file.flush()
    except KeyboardInterrupt:
        print("\nStopped.")
    except serial.SerialException as e:
        print(f"\nLost connection to the sensor: {e}")
        sys.exit(1)
    finally:
        ser.close()
        if csv_file:
            csv_file.close()


if __name__ == "__main__":
    main()
