"""Read a PT100/PT1000 using the shared ADS122C04 driver and README wiring.

Requires smbus2, but not CraftBeerPi. With --wires 3, use the compensated
dual-IDAC wiring, NOT the former AIN0 excitation / AIN1 sense connection.
"""

import argparse
import importlib.util
import math
from pathlib import Path
import time


def load_driver():
    driver_path = Path(__file__).resolve().parent / "cbpi4-pt100x" / "ads122c04.py"
    spec = importlib.util.spec_from_file_location("pt100x_ads_driver", driver_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load ADS122C04 driver from {}".format(driver_path))
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as exc:
        if exc.name != "smbus2":
            raise
        raise SystemExit(
            "Missing dependency: install smbus2 in the environment running this script."
        ) from exc
    return module.ADS122C04


def parse_args():
    parser = argparse.ArgumentParser(description="Read a PT100/PT1000 using ADS122C04")
    parser.add_argument("--bus", type=int, default=1, help="I2C bus (default: 1)")
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x40,
                        help="7-bit I2C address (default: 0x40)")
    parser.add_argument("--wires", type=int, choices=(2, 3, 4), default=4,
                        help="3 enables dual-IDAC compensation; see README (default: 4)")
    parser.add_argument("--rtd", type=int, choices=(100, 1000), default=100,
                        help="RTD nominal resistance (default: 100)")
    parser.add_argument("--rref", type=float, default=470.0,
                        help="Measured RREF in ohms; use 4700 for PT1000 (default: 470)")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="Seconds between readings (default: 1)")
    parser.add_argument("--timeout", type=float, default=0.5,
                        help="Timeout per conversion in seconds (default: 0.5)")
    return parser.parse_args()


def main():
    args = parse_args()
    if not all(math.isfinite(value) and value > 0
               for value in (args.rref, args.interval, args.timeout)):
        raise SystemExit("--rref, --interval and --timeout must be finite and positive")
    driver = load_driver()
    sensor = driver(bus=args.bus, address=args.address, rtd_nominal=args.rtd,
                    ref_resistor=args.rref, timeout=args.timeout, wires=args.wires)
    print(
        "ADS122C04 at 0x{:02X}; {} wires; Rref={:.3f} ohm. Press Ctrl+C to stop.".format(
            args.address, args.wires, args.rref
        )
    )
    while True:
        started = time.monotonic()
        code, resistance, temperature = sensor.read_measurement()
        print("code={:10.1f}  R={:9.4f} ohm  T={:8.2f} C".format(
            code, resistance, temperature
        ))
        time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        raise SystemExit("Measurement failed: {}".format(exc)) from None
