"""Read a PT100 connected to an ADS122C04 using the wiring in the project notes.

Requires smbus2. Default I2C address is 0x40 and default reference resistor is
470 ohm. Check the address straps and measure the actual reference resistor.

Expected current path: AIN0 -> PT100 -> RREF -> AVSS. AIN1 senses the
PT100 high side; AIN2 and REFP sense the PT100/RREF junction. REFN connects
to the low side of RREF (AVSS). This is not a generic three-wire RTD setup.
"""

import argparse
import math
import time

try:
    from smbus2 import SMBus, i2c_msg
except ImportError as exc:
    raise SystemExit(
        "Missing dependency: install smbus2 in the Python environment running this script."
    ) from exc


REGISTER_0 = 0x00
REGISTER_1 = 0x01
REGISTER_2 = 0x02
REGISTER_3 = 0x03

COMMAND_RESET = 0x06
COMMAND_START = 0x08
COMMAND_RDATA = 0x10
COMMAND_RREG = 0x20
COMMAND_WREG = 0x40

PT100_R0 = 100.0
CALLENDAR_A = 3.9083e-3
CALLENDAR_B = -5.775e-7
CALLENDAR_C = -4.183e-12


def write_command(bus, address, command):
    bus.i2c_rdwr(i2c_msg.write(address, [command]))


def write_register(bus, address, register, value):
    command = COMMAND_WREG | (register << 2)
    bus.i2c_rdwr(i2c_msg.write(address, [command, value]))


def read_register(bus, address, register):
    command = COMMAND_RREG | (register << 2)
    write = i2c_msg.write(address, [command])
    read = i2c_msg.read(address, 1)
    bus.i2c_rdwr(write, read)
    return list(read)[0]


def read_conversion(bus, address):
    write = i2c_msg.write(address, [COMMAND_RDATA])
    read = i2c_msg.read(address, 3)
    bus.i2c_rdwr(write, read)
    data = list(read)
    code = (data[0] << 16) | (data[1] << 8) | data[2]
    if code & 0x800000:
        code -= 1 << 24
    return code


def resistance_to_temperature(resistance_ohm):
    if resistance_ohm <= 0:
        raise ValueError("RTD resistance must be positive")

    ratio = resistance_ohm / PT100_R0
    if resistance_ohm >= PT100_R0:
        discriminant = CALLENDAR_A**2 - 4 * CALLENDAR_B * (1 - ratio)
        return (-CALLENDAR_A + math.sqrt(discriminant)) / (2 * CALLENDAR_B)

    low = -200.0
    high = 0.0
    for _ in range(50):
        middle = (low + high) / 2
        expected_ratio = (
            1
            + CALLENDAR_A * middle
            + CALLENDAR_B * middle**2
            + CALLENDAR_C * (middle - 100) * middle**3
        )
        if expected_ratio < ratio:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def configure_ads122c04(bus, address):
    write_command(bus, address, COMMAND_RESET)
    time.sleep(0.01)

    # AIN1-AIN2, gain 1 with PGA bypass; 20 SPS, single-shot, external REFP/REFN;
    # 1.5 mA IDAC1 routed to AIN0. AIN0 supplies the RTD, then RREF to ground.
    registers = {
        REGISTER_0: 0x41,
        REGISTER_1: 0x02,
        REGISTER_2: 0x07,
        REGISTER_3: 0x20,
    }
    for register, value in registers.items():
        write_register(bus, address, register, value)
    for register, expected in registers.items():
        actual = read_register(bus, address, register)
        mask = 0x7F if register == REGISTER_2 else 0xFF
        if actual & mask != expected:
            raise RuntimeError(
                "ADS122C04 register {}: wrote 0x{:02X}, read 0x{:02X}. "
                "Check the I2C address and device type.".format(
                    register, expected, actual
                )
            )


def read_temperature(bus, address, reference_ohm, timeout):
    write_command(bus, address, COMMAND_START)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_register(bus, address, REGISTER_2) & 0x80:
            code = read_conversion(bus, address)
            resistance = code * reference_ohm / (1 << 23)
            if code <= 0 or code == 0x7FFFFF:
                raise ValueError(
                    "Invalid PT100 reading: code={} (0x{:06X}), R={:.4f} ohm. "
                    "Check AIN1 (PT100 high side), AIN2 (PT100 low side), "
                    "the AIN0 excitation path and REFP/REFN across RREF. "
                    "Do not use abs(code) to hide a wiring/reference fault.".format(
                        code, code & 0xFFFFFF, resistance
                    )
                )
            temperature = resistance_to_temperature(resistance)
            return code, resistance, temperature
        time.sleep(0.002)
    raise TimeoutError("ADS122C04 conversion did not complete before timeout")


def parse_args():
    parser = argparse.ArgumentParser(description="Read a PT100 using ADS122C04")
    parser.add_argument("--bus", type=int, default=1, help="I2C bus number (default: 1)")
    parser.add_argument(
        "--address",
        type=lambda value: int(value, 0),
        default=0x40,
        help="7-bit I2C address, e.g. 0x40 (default: 0x40)",
    )
    parser.add_argument(
        "--rref",
        type=float,
        default=470.0,
        help="Measured reference resistance in ohms (default: 470)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Seconds between readings (default: 1)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.5,
        help="Conversion-ready timeout in seconds (default: 0.5)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.rref <= 0 or args.interval <= 0 or args.timeout <= 0:
        raise SystemExit("--rref, --interval and --timeout must be positive")

    with SMBus(args.bus) as bus:
        configure_ads122c04(bus, args.address)
        print(
            "ADS122C04 at 0x{:02X}; Rref={:.3f} ohm. Press Ctrl+C to stop.".format(
                args.address, args.rref
            )
        )
        while True:
            started = time.monotonic()
            code, resistance, temperature = read_temperature(
                bus, args.address, args.rref, args.timeout
            )
            print(
                "code={:8d}  R={:9.4f} ohm  T={:8.2f} C".format(
                    code, resistance, temperature
                )
            )
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        raise SystemExit("Measurement failed: {}".format(exc)) from None