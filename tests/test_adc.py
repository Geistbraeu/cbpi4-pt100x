import asyncio
import importlib.util
import math
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


class SensorBase:
    def __init__(self, cbpi, sensor_id, props):
        self.cbpi = cbpi
        self.id = sensor_id
        self.props = props

    def get_sensor(self, sensor_id):
        return types.SimpleNamespace(name="test sensor")

    def get_config_value(self, key, default):
        return default


property_api = types.SimpleNamespace(
    Select=lambda **kwargs: kwargs,
    Number=lambda **kwargs: kwargs,
    Kettle=lambda **kwargs: kwargs,
    Fermenter=lambda **kwargs: kwargs,
)
api = types.ModuleType("cbpi.api")
api.CBPiSensor = SensorBase
api.Property = property_api
api.parameters = lambda options: lambda sensor_class: sensor_class
dataclasses_api = types.ModuleType("cbpi.api.dataclasses")
dataclasses_api.NotificationAction = lambda *args: args
dataclasses_api.NotificationType = types.SimpleNamespace(WARNING="warning", ERROR="error")
root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "pt100x_test_plugin", root / "cbpi4-pt100x" / "__init__.py",
    submodule_search_locations=[str(root / "cbpi4-pt100x")],
)
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
with patch.dict(sys.modules, {"cbpi": types.ModuleType("cbpi"),
                              "cbpi.api": api, "cbpi.api.dataclasses": dataclasses_api}):
    spec.loader.exec_module(plugin)
from pt100x_test_plugin import ads122c04 as ads


class FakeBus:
    code = round(110 / 470 * (1 << 23))
    ready = True
    mismatch = False
    opened = 0
    closed = 0

    def __init__(self, bus):
        self.registers = dict(ads.ADS122C04._registers)
        type(self).opened += 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        type(self).closed += 1

    def i2c_rdwr(self, *messages):
        command = list(messages[0])[0]
        if command & 0xF0 == 0x40:
            self.registers[(command >> 2) & 3] = list(messages[0])[1]
        elif command & 0xF0 == 0x20:
            register = (command >> 2) & 3
            value = self.registers[register]
            if register == 2 and self.ready:
                value |= 0x80
            if register == 0 and self.mismatch:
                value ^= 0x10
            messages[1].buf[0] = bytes([value])
        elif command == 0x10:
            for index, value in enumerate((self.code & 0xFFFFFF).to_bytes(3, "big")):
                messages[1].buf[index] = bytes([value])


class ADSDriverTests(unittest.TestCase):
    def setUp(self):
        FakeBus.code = round(110 / 470 * (1 << 23))
        FakeBus.ready = True
        FakeBus.mismatch = False
        FakeBus.opened = FakeBus.closed = 0
        self.bus_patch = patch.object(ads, "SMBus", FakeBus)
        self.bus_patch.start()
        self.addCleanup(self.bus_patch.stop)

    def tearDown(self):
        self.assertEqual(FakeBus.opened, FakeBus.closed)

    def test_temperature_and_signed_codes(self):
        sensor = ads.ADS122C04()
        self.assertAlmostEqual(sensor.temperature, 25.684, places=2)
        for code in (0, -1, -8388608, 8388607):
            with self.subTest(code=code):
                FakeBus.code = code
                with self.assertRaisesRegex(ValueError, "code={}".format(code)):
                    sensor.temperature

    def test_callendar_van_dusen(self):
        for nominal in (100, 1000):
            sensor = ads.ADS122C04(rtd_nominal=nominal)
            for temperature in (-200, -50, 0, 25, 100, 850):
                resistance = nominal * sensor._resistance_ratio(temperature)
                self.assertAlmostEqual(sensor._temperature_from_resistance(resistance), temperature)
            for resistance in (0, nominal * 0.1, nominal * 5, math.nan):
                with self.assertRaises(ValueError):
                    sensor._temperature_from_resistance(resistance)

    def test_timeout_and_register_mismatch(self):
        sensor = ads.ADS122C04(timeout=0.005)
        FakeBus.ready = False
        with self.assertRaises(TimeoutError):
            sensor.temperature
        FakeBus.mismatch = True
        with self.assertRaisesRegex(RuntimeError, "register 0"):
            ads.ADS122C04()


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.cbpi = Mock()
        self.board = types.SimpleNamespace(SPI=Mock(return_value="spi"), D17="pin17")
        self.digitalio = types.SimpleNamespace(DigitalInOut=Mock(return_value="cs"))
        self.max_factory = Mock(return_value=types.SimpleNamespace(temperature=20))
        modules = {"board": self.board, "digitalio": self.digitalio,
                   "adafruit_max31865": types.SimpleNamespace(MAX31865=self.max_factory)}
        hardware_patch = patch.dict(sys.modules, modules)
        hardware_patch.start()
        self.addCleanup(hardware_patch.stop)

    def test_existing_max_sensor(self):
        sensor = plugin.CustomSensor(self.cbpi, "max", {})
        self.assertEqual(sensor.adc, "MAX31865")
        self.max_factory.assert_called_once_with(
            "spi", "cs", rtd_nominal=1000, ref_resistor=4300, wires=2
        )
        self.assertEqual(sensor.read(), 20)

    def make_ads_sensor(self):
        with patch.object(ads, "ADS122C04", return_value=types.SimpleNamespace(temperature=25)) as factory:
            sensor = plugin.CustomSensor(self.cbpi, "ads", {
                "ADC": "ADS122C04", "ResSens": 100, "RefRest": 470,
                "I2CBus": 1, "I2CAddress": "0x40", "Wires": "ignored",
                "offset": 2, "sample_delay_ms": 0,
            })
            factory.assert_called_once_with(bus=1, address=64, rtd_nominal=100, ref_resistor=470)
        return sensor

    def test_ads_configuration_and_units(self):
        sensor = self.make_ads_sensor()
        self.board.SPI.assert_not_called()
        self.digitalio.DigitalInOut.assert_not_called()
        self.assertEqual(sensor.Wires, 4)
        self.assertEqual(sensor.read(), 27)
        with patch.object(sensor, "get_config_value", return_value="F"):
            self.assertEqual(sensor.read(), 79)

    def test_median_for_both_drivers(self):
        sensors = [self.make_ads_sensor(), plugin.CustomSensor(self.cbpi, "max", {})]
        for sensor in sensors:
            sensor.sample_delay_ms = 0
            with patch.object(sensor, "read", side_effect=[20, 21, 99, 19, 22]):
                self.assertEqual(asyncio.run(sensor.read_median()), 21)
            with patch.object(sensor, "read", side_effect=[20, ValueError("bad"), 99, 19, 22]):
                self.assertEqual(asyncio.run(sensor.read_median()), 21)
            sensor.value_old = 23
            with patch.object(sensor, "read", side_effect=ValueError("bad")):
                self.assertEqual(asyncio.run(sensor.read_median()), 23)

    def test_initialization_failure(self):
        with patch.object(ads, "ADS122C04", side_effect=OSError("no I2C")):
            sensor = plugin.CustomSensor(self.cbpi, "ads", {"ADC": "ADS122C04"})
        self.cbpi.notify.assert_called_once()
        with self.assertRaises(RuntimeError):
            sensor.read()


if __name__ == "__main__":
    unittest.main()