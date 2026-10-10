import math
import threading
import time

from smbus2 import SMBus, i2c_msg


class ADS122C04:
    _lock = threading.Lock()
    _registers = {0: 0x41, 1: 0x02, 2: 0x06, 3: 0x24}
    _coefficient_a = 3.9083e-3
    _coefficient_b = -5.775e-7
    _coefficient_c = -4.183e-12

    def __init__(self, bus: int = 1, address: int = 0x40,
                 rtd_nominal: float = 100, ref_resistor: float = 470,
                 timeout: float = 0.5, wires: int = 4):
        if bus < 0 or not 0x40 <= address <= 0x4F:
            raise ValueError("Invalid ADS122C04 I2C bus or address")
        if not all(math.isfinite(value) and value > 0
                   for value in (rtd_nominal, ref_resistor, timeout)):
            raise ValueError("RTD nominal, reference resistance and timeout must be positive")
        if wires not in (2, 3, 4):
            raise ValueError("RTD wires must be 2, 3 or 4")
        self.bus = bus
        self.address = address
        self.rtd_nominal = rtd_nominal
        self.ref_resistor = ref_resistor
        self.timeout = timeout
        self.wires = wires
        self._registers = dict(type(self)._registers)
        current = 0.0001 if rtd_nominal >= 1000 else 0.001
        reference_voltage = 2 * current * ref_resistor
        if not 0.75 <= reference_voltage <= 2.4:
            raise ValueError(
                "RREF must produce 0.75..2.4 V with both IDACs; "
                "allowed RREF is {:.1f}..{:.1f} ohm at {:.0f} uA per IDAC; "
                "also check supply and RTD/lead voltage headroom".format(
                    0.75 / (2 * current), 2.4 / (2 * current), current * 1e6
                )
            )
        self._registers.update({2: 0x03 if rtd_nominal >= 1000 else 0x06,
                                3: 0x4C if wires == 3 else 0x24})
        with self._lock, SMBus(self.bus) as connection:
            self._command(connection, 0x06)
            time.sleep(0.01)
            for register, value in self._registers.items():
                connection.i2c_rdwr(i2c_msg.write(
                    self.address, [0x40 | (register << 2), value]
                ))
            for register, expected in self._registers.items():
                actual = self._read_register(connection, register)
                mask = 0x7F if register == 2 else 0xFF
                if actual & mask != expected:
                    raise RuntimeError(
                        "ADS122C04 register {}: expected 0x{:02X}, read 0x{:02X}".format(
                            register, expected, actual
                        )
                    )

    def _command(self, connection, command):
        connection.i2c_rdwr(i2c_msg.write(self.address, [command]))

    def _read_register(self, connection, register):
        result = i2c_msg.read(self.address, 1)
        connection.i2c_rdwr(
            i2c_msg.write(self.address, [0x20 | (register << 2)]), result
        )
        return list(result)[0]

    def _temperature_from_resistance(self, resistance):
        ratio = resistance / self.rtd_nominal
        minimum = self._resistance_ratio(-200)
        maximum = self._resistance_ratio(850)
        if not math.isfinite(ratio) or not minimum <= ratio <= maximum:
            raise ValueError("RTD resistance {:.4f} ohm is outside -200..850 C".format(resistance))
        if ratio >= 1:
            discriminant = self._coefficient_a**2 - 4 * self._coefficient_b * (1 - ratio)
            return (-self._coefficient_a + math.sqrt(discriminant)) / (2 * self._coefficient_b)
        low, high = -200.0, 0.0
        for _ in range(50):
            middle = (low + high) / 2
            if self._resistance_ratio(middle) < ratio:
                low = middle
            else:
                high = middle
        return (low + high) / 2

    def _resistance_ratio(self, temperature):
        ratio = 1 + self._coefficient_a * temperature + self._coefficient_b * temperature**2
        if temperature < 0:
            ratio += self._coefficient_c * (temperature - 100) * temperature**3
        return ratio

    @property
    def temperature(self):
        return self.read_measurement()[2]

    def read_measurement(self):
        with self._lock, SMBus(self.bus) as connection:
            if self.wires == 3:
                codes = []
                for routing in (0x4C, 0x68):
                    connection.i2c_rdwr(i2c_msg.write(self.address, [0x4C, routing]))
                    time.sleep(0.01)
                    codes.append(self._read_code(connection))
                # Swapping IDACs cancels their mismatch; RREF carries both currents.
                code = sum(codes) / 2
                resistance = sum(codes) * self.ref_resistor / (1 << 23)
            else:
                code = self._read_code(connection)
                resistance = code * self.ref_resistor / (1 << 23)
            return code, resistance, self._temperature_from_resistance(resistance)

    def _read_code(self, connection):
        self._command(connection, 0x08)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self._read_register(connection, 2) & 0x80:
                result = i2c_msg.read(self.address, 3)
                connection.i2c_rdwr(i2c_msg.write(self.address, [0x10]), result)
                code = int.from_bytes(bytes(result), "big", signed=True)
                if code <= 0 or code == 0x7FFFFF:
                    raise ValueError(
                        "Invalid PT100/PT1000 reading: code={}. "
                        "Check the {}-wire AIN and IDAC connections in README, "
                        "REFP/RREF and REFN/AVSS.".format(code, self.wires)
                    )
                return code
            time.sleep(0.002)
        raise TimeoutError("ADS122C04 conversion did not complete before timeout")