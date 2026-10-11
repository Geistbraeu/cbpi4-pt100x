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
sensor_properties = []


def parameters(options):
    sensor_properties.extend(options)
    return lambda sensor_class: sensor_class


api.parameters = parameters
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
if sys.platform == "win32":
    # smbus2 imports the Linux ioctl API; all bus access below uses FakeBus.
    fcntl_stub = types.ModuleType("fcntl")
    fcntl_stub.ioctl = Mock(side_effect=AssertionError("Unexpected hardware access"))
    fcntl_patch = patch.dict(sys.modules, {"fcntl": fcntl_stub})
    fcntl_patch.start()
    unittest.addModuleCleanup(fcntl_patch.stop)
from pt100x_test_plugin import ads122c04 as ads


class FakeBus:
    code = round(110 / 470 * (1 << 23))
    ready = True
    mismatch = False
    opened = 0
    closed = 0
    circuit = None
    codes = None
    writes = []
    timeout_after_first = False

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
            self.writes.append(((command >> 2) & 3, list(messages[0])[1]))
        elif command & 0xF0 == 0x20:
            register = (command >> 2) & 3
            value = self.registers[register]
            if register == 2 and self.ready:
                value |= 0x80
            if register == 0 and self.mismatch:
                value ^= 0x10
            messages[1].buf[0] = bytes([value])
        elif command == 0x10:
            code = self.code
            if self.codes is not None:
                code = self.codes.pop(0)
            elif self.circuit is not None:
                rtd, reference, lead1, lead2, current1, current2 = self.circuit
                if self.registers[3] == 0x68:
                    current1, current2 = current2, current1
                voltage = current1 * (rtd + lead1) - current2 * lead2
                code = round(voltage / ((current1 + current2) * reference) * (1 << 23))
            for index, value in enumerate((code & 0xFFFFFF).to_bytes(3, "big")):
                messages[1].buf[index] = bytes([value])
            if self.timeout_after_first:
                type(self).ready = False


class ADSDriverTests(unittest.TestCase):
    def setUp(self):
        FakeBus.code = round(110 / 470 * (1 << 23))
        FakeBus.ready = True
        FakeBus.mismatch = False
        FakeBus.opened = FakeBus.closed = 0
        FakeBus.circuit = FakeBus.codes = None
        FakeBus.writes = []
        FakeBus.timeout_after_first = False
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
            sensor = ads.ADS122C04(rtd_nominal=nominal,
                                 ref_resistor=4700 if nominal == 1000 else 470)
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

    def test_registers_for_all_wire_counts(self):
        for wires in (2, 3, 4):
            for nominal, reference in ((100, 470), (1000, 4700)):
                with self.subTest(wires=wires, nominal=nominal):
                    FakeBus.writes = []
                    ads.ADS122C04(wires=wires, rtd_nominal=nominal,
                                 ref_resistor=reference)
                    expected = {0: 0x41, 1: 0x02,
                                2: 0x06 if nominal == 100 else 0x03,
                                3: 0x4C if wires == 3 else 0x24}
                    self.assertEqual(dict(FakeBus.writes), expected)
        for wires in (0, 1, 5, "3"):
            with self.assertRaises(ValueError):
                ads.ADS122C04(wires=wires)
        with self.assertRaisesRegex(ValueError, "RREF"):
            ads.ADS122C04(wires=3, rtd_nominal=1000, ref_resistor=470)

    def test_universal_reference_ranges_and_headroom(self):
        for nominal, references, current in (
            (100, (430, 470, 510), 0.001),
            (1000, (4300, 4700, 5100), 0.0001),
        ):
            for wires in (2, 3, 4):
                for reference in references:
                    with self.subTest(nominal=nominal, wires=wires, reference=reference):
                        sensor = ads.ADS122C04(
                            rtd_nominal=nominal, ref_resistor=reference, wires=wires
                        )
                        self.assertGreaterEqual(2 * current * reference, 0.75)
                        resistance = nominal * sensor._resistance_ratio(850)
                        if wires == 3:
                            voltage = current * (resistance + 15) + 2 * current * (reference + 15)
                        else:
                            voltage = 2 * current * (resistance + reference + 30)
                        self.assertLess(voltage, 3.3 - 0.9)
        for wires in (2, 3, 4):
            with self.assertRaisesRegex(ValueError, "RREF"):
                ads.ADS122C04(wires=wires, ref_resistor=100)

    def test_custom_reference_resistance(self):
        for nominal, reference in ((100, 562.25), (1000, 5622.5)):
            for wires in (2, 3, 4):
                with self.subTest(nominal=nominal, wires=wires):
                    sensor = ads.ADS122C04(
                        wires=wires, rtd_nominal=nominal, ref_resistor=reference
                    )
                    resistance = nominal * sensor._resistance_ratio(25)
                    if wires == 3:
                        FakeBus.circuit = (resistance, reference, 5, 5, 1.01, 0.99)
                    else:
                        FakeBus.circuit = None
                        FakeBus.code = round(resistance / reference * (1 << 23))
                    self.assertAlmostEqual(sensor.temperature, 25, delta=0.001)

    def test_three_wire_compensates_leads_and_idac_mismatch(self):
        for nominal, reference in ((100, 470), (1000, 4700)):
            sensor = ads.ADS122C04(wires=3, rtd_nominal=nominal,
                                 ref_resistor=reference)
            for temperature in (-100, 0, 25, 100, 800):
                for lead in (0, 1, 15):
                    with self.subTest(nominal=nominal, temperature=temperature, lead=lead):
                        resistance = nominal * sensor._resistance_ratio(temperature)
                        FakeBus.circuit = (resistance, reference, lead, lead, 1.01, 0.99)
                        FakeBus.writes = []
                        code, actual, result = sensor.read_measurement()
                        self.assertAlmostEqual(actual, resistance, delta=0.001)
                        self.assertAlmostEqual(result, temperature, delta=0.001)
                        self.assertAlmostEqual(code * 2 * reference / (1 << 23), actual)
                        self.assertEqual(FakeBus.writes, [(3, 0x4C), (3, 0x68)])

    def test_three_wire_unequal_leads_leave_residual_error(self):
        sensor = ads.ADS122C04(wires=3)
        FakeBus.circuit = (110, 470, 5, 2, 1.01, 0.99)
        self.assertAlmostEqual(sensor.read_measurement()[1], 113, delta=0.001)

    def test_three_wire_rejects_each_invalid_conversion(self):
        sensor = ads.ADS122C04(wires=3, timeout=0.005)
        valid = round(110 / (2 * 470) * (1 << 23))
        for code in (0, -1, -8388608, 8388607):
            for position in (0, 1):
                with self.subTest(code=code, position=position):
                    FakeBus.codes = [code, valid] if position == 0 else [valid, code]
                    with self.assertRaisesRegex(ValueError, "code={}".format(code)):
                        sensor.temperature
        FakeBus.codes = None
        FakeBus.ready = False
        with self.assertRaises(TimeoutError):
            sensor.temperature
        FakeBus.ready = True
        FakeBus.timeout_after_first = True
        with self.assertRaises(TimeoutError):
            sensor.temperature


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

    def test_reference_properties(self):
        properties = {option["label"]: option for option in sensor_properties}
        self.assertEqual(properties["RefRest"]["options"], [430, 4300])
        self.assertTrue(properties["ADSRefRest"]["configurable"])
        self.assertNotIn("options", properties["ADSRefRest"])
        self.assertNotIn("default_value", properties["ADSRefRest"])

    def test_ads_reference_priority_and_legacy_settings(self):
        cases = [
            ({}, 470),
            ({"ResSens": 1000}, 4700),
            ({"RefRest": 430}, 430),
            ({"RefRest": 4700, "ResSens": 1000}, 4700),
            ({"ADSRefRest": None, "RefRest": 430}, 430),
            ({"ADSRefRest": "", "RefRest": 470}, 470),
            ({"ADSRefRest": " ", "RefRest": 4300}, 4300),
            ({"ADSRefRest": "469.8", "RefRest": 430}, 469.8),
            ({"ADSRefRest": 562.25, "RefRest": 470}, 562.25),
        ]
        for props, expected in cases:
            with self.subTest(props=props):
                self.cbpi.reset_mock()
                with patch.object(
                    ads, "ADS122C04",
                    return_value=types.SimpleNamespace(temperature=25),
                ) as factory:
                    sensor = plugin.CustomSensor(
                        self.cbpi, "ads", {"ADC": "ADS122C04", **props}
                    )
                self.assertEqual(sensor.RefRest, expected)
                self.assertEqual(factory.call_args.kwargs["ref_resistor"], expected)
                self.cbpi.notify.assert_not_called()

    def test_invalid_ads_reference_is_reported_without_fallback(self):
        for reference in (0, -1, math.nan, math.inf, "invalid"):
            with self.subTest(reference=reference):
                self.cbpi.reset_mock()
                with patch.object(ads, "ADS122C04") as factory:
                    sensor = plugin.CustomSensor(self.cbpi, "ads", {
                        "ADC": "ADS122C04", "ADSRefRest": reference, "RefRest": 470,
                    })
                factory.assert_not_called()
                self.cbpi.notify.assert_called_once()
                with self.assertRaises(RuntimeError):
                    sensor.read()

    def test_max_ignores_ads_reference(self):
        for reference in (430, 4300, 470):
            with self.subTest(reference=reference):
                self.max_factory.reset_mock()
                sensor = plugin.CustomSensor(self.cbpi, "max", {
                    "RefRest": reference, "ADSRefRest": "invalid",
                })
                self.assertEqual(sensor.RefRest, reference)
                self.assertEqual(
                    self.max_factory.call_args.kwargs["ref_resistor"], reference
                )

    def make_ads_sensor(self):
        with patch.object(ads, "ADS122C04", return_value=types.SimpleNamespace(temperature=25)) as factory:
            sensor = plugin.CustomSensor(self.cbpi, "ads", {
                "ADC": "ADS122C04", "ResSens": 100, "RefRest": 470,
                "I2CBus": 1, "I2CAddress": "0x40", "Wires": 4,
                "offset": 2, "sample_delay_ms": 0,
            })
            factory.assert_called_once_with(
                bus=1, address=64, rtd_nominal=100, ref_resistor=470, wires=4
            )
        return sensor

    def test_ads_configuration_and_units(self):
        sensor = self.make_ads_sensor()
        self.board.SPI.assert_not_called()
        self.digitalio.DigitalInOut.assert_not_called()
        self.assertEqual(sensor.Wires, 4)
        self.assertEqual(sensor.read(), 27)
        with patch.object(sensor, "get_config_value", return_value="F"):
            self.assertEqual(sensor.read(), 79)

    def test_ads_wire_count_selection(self):
        for wires in (None, 2, 3, 4):
            with self.subTest(wires=wires):
                props = {"ADC": "ADS122C04"}
                if wires is not None:
                    props["Wires"] = wires
                with patch.object(
                    ads, "ADS122C04",
                    return_value=types.SimpleNamespace(temperature=25),
                ) as factory:
                    sensor = plugin.CustomSensor(self.cbpi, "ads", props)
                self.assertEqual(sensor.Wires, 4 if wires is None else wires)
                factory.assert_called_once_with(
                    bus=1, address=64, rtd_nominal=100, ref_resistor=470,
                    wires=4 if wires is None else wires,
                )
                self.assertEqual(sensor.read(), 25)
                self.cbpi.notify.assert_not_called()

    def test_standalone_uses_shared_driver_and_wire_selection(self):
        script_spec = importlib.util.spec_from_file_location("adstest", root / "adstest.py")
        script = importlib.util.module_from_spec(script_spec)
        script_spec.loader.exec_module(script)
        driver = script.load_driver()
        self.assertEqual(driver.__module__, "pt100x_ads_driver")
        with patch.object(sys, "argv", ["adstest.py", "--wires", "3", "--rtd", "1000",
                                       "--rref", "4700"]):
            args = script.parse_args()
        self.assertEqual((args.wires, args.rtd, args.rref), (3, 1000, 4700))
        sensor = Mock()
        sensor.read_measurement.side_effect = KeyboardInterrupt
        with patch.object(script, "parse_args", return_value=args), \
                patch.object(script, "load_driver", return_value=Mock(return_value=sensor)) as load, \
                patch("builtins.print"):
            with self.assertRaises(KeyboardInterrupt):
                script.main()
        load.return_value.assert_called_once_with(
            bus=1, address=64, rtd_nominal=1000, ref_resistor=4700,
            timeout=0.5, wires=3,
        )
        sensor.read_measurement.assert_called_once_with()

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

    def test_unavailable_sensor_is_logged_once_and_recovery_is_logged(self):
        sensor = self.make_ads_sensor()
        sensor.sample_delay_ms = 0

        with patch.object(plugin.logging, "error") as log_error, \
                patch.object(plugin.logging, "info") as log_info:
            with patch.object(sensor, "read", side_effect=TimeoutError("ADC timeout")):
                self.assertEqual(asyncio.run(sensor.read_median()), 0)
                self.assertEqual(asyncio.run(sensor.read_median()), 0)
            log_error.assert_called_once()
            self.assertIn("no valid readings from ADS122C04", log_error.call_args[0][0])
            self.assertIn("ADC timeout", log_error.call_args[0][0])

            with patch.object(sensor, "read", return_value=25):
                self.assertEqual(asyncio.run(sensor.read_median()), 25)
            log_info.assert_called_once_with(
                "Sensor test sensor: readings from ADS122C04 recovered"
            )

    def test_initialization_failure(self):
        with patch.object(ads, "ADS122C04", side_effect=OSError("no I2C")):
            sensor = plugin.CustomSensor(self.cbpi, "ads", {"ADC": "ADS122C04"})
        self.cbpi.notify.assert_called_once()
        with self.assertRaises(RuntimeError):
            sensor.read()


if __name__ == "__main__":
    unittest.main()