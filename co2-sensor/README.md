# CO2 sensor

A simple example: plug the sensor into your computer over USB, run this
script on that same computer, and watch a live stream of CO2, temperature,
and humidity readings. Works on Windows, macOS, and Linux — no other
hardware or setup required.

## How it works

The sensor sends one line of text per second over USB, like this:

```
0.041,21.3,21.6,45.2
```

That's `co2, temperature_1, temperature_2, humidity`. This script reads each
line, splits it into those four numbers, and prints them in a readable
format.

**Note:** CO2 is reported as a percentage, not parts per million (ppm). For
example, `0.041` means about 410 ppm — roughly the CO2 level in outdoor air.
There are also two temperature readings; this script averages them.

## Requirements

- Python 3.8+
- `pip install -r requirements.txt` (installs `pyserial`)
- On Linux, you may need to add your user to the `dialout` group to access
  the USB port without `sudo`. Windows and macOS don't need this.

## Usage

1. Plug the sensor into your computer with a USB cable.
2. Run the script:

   ```bash
   python3 read_co2_sensor.py
   ```

   It looks through the serial ports on your computer, finds the sensor
   automatically, and starts printing readings.

3. Press Ctrl-C to stop.

If auto-detect doesn't find it, pass the port directly. What that looks like
depends on your OS:

```bash
python3 read_co2_sensor.py --port /dev/ttyACM0      # Linux
python3 read_co2_sensor.py --port /dev/cu.usbmodem14201   # macOS
python3 read_co2_sensor.py --port COM3               # Windows
```

To also save the readings to a file:

```bash
python3 read_co2_sensor.py --out readings.csv
```

Example output:

```
Reading from /dev/ttyACM0 (Ctrl-C to stop)
2026-08-28T13:46:24  CO2: 0.041%  Temp: 21.5C  Humidity: 45.2%
2026-08-28T13:46:25  CO2: 0.041%  Temp: 21.4C  Humidity: 45.3%
```
