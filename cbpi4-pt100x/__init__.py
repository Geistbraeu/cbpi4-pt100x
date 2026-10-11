# -*- coding: utf-8 -*-
import asyncio
import logging
import math
import statistics
import time
from subprocess import call

from cbpi.api import *
from cbpi.api.dataclasses import NotificationAction, NotificationType

logger = logging.getLogger(__name__)


@parameters(
    [
        Property.Select(
            label="ADC",
            options=["MAX31865", "ADS122C04"],
            description="ADC type (default: MAX31865 for existing sensors)",
        ),
        Property.Number(
            label="I2CBus",
            configurable=True,
            default_value=1,
            description="ADS122C04 I2C bus number (default: 1; ignored for MAX31865)",
        ),
        Property.Number(
            label="I2CAddress",
            configurable=True,
            default_value=64,
            description="ADS122C04 address in decimal (64 = 0x40; ignored for MAX31865)",
        ),
        Property.Select(
            label="csPin",
            options=[
                0,
                1,
                2,
                3,
                4,
                5,
                6,
                7,
                8,
                12,
                13,
                14,
                15,
                16,
                17,
                18,
                19,
                20,
                21,
                22,
                23,
                24,
                25,
                26,
                27,
            ],
            description="GPIO Pin connected to the CS Pin of the MAX31865 - For MISO, MOSI, CLK no choice by default it's PIN 9, 10, 11",
        ),
        Property.Select(
            label="ResSens",
            options=[100, 1000],
            description="Select 100 for PT100 or 1000 for PT1000",
        ),
        Property.Select(
            label="RefRest",
            options=[430, 4300],
            description="MAX31865 reference resistor in ohms: 430 for PT100 or 4300 for PT1000",
        ),
        Property.Number(
            label="ADSRefRest",
            configurable=True,
            description="ADS122C04 measured reference resistor in ohms (decimals allowed). Leave empty to use saved RefRest, or 470/4700 for a new PT100/PT1000 sensor. Ignored for MAX31865",
        ),
        Property.Number(
            label="offset",
            configurable=True,
            default_value=0,
            description="Offset for the PT Sensor (Default is 0)",
        ),
        Property.Number(
            label="ignore_below",
            configurable=True,
            default_value=0,
            description="Readings below this value will be ignored",
        ),
        Property.Number(
            label="ignore_above",
            configurable=True,
            default_value=100,
            description="Readings above this value will be ignored",
        ),
        Property.Number(
            label="ignore_delta",
            configurable=True,
            default_value=1,
            description="Ignore reading if delta between two readings is above this value. Must be positive (0: inactive)",
        ),
        Property.Number(
            label="alpha",
            configurable=True,
            default_value=1,
            description="Calculate Average between 2 values. Must be between 0 and 1. 1 deactivates averaging",
        ),
        Property.Number(
            label="sample_count",
            configurable=True,
            default_value=5,
            description="Read sensor this many times and use median value. Must be >= 1",
        ),
        Property.Number(
            label="sample_delay_ms",
            configurable=True,
            default_value=30,
            description="Delay between sampled readings in milliseconds. Must be >= 0",
        ),
        Property.Select(
            label="Wires",
            options=[2, 3, 4],
            description="Select RTD wiring. ADS122C04: 3 wires enables dual-IDAC compensation and requires the 3-wire diagram in README; 2 wires is uncompensated",
        ),
        Property.Select(
            label="Interval",
            options=[1, 5, 10, 30, 60],
            description="Interval in Seconds",
        ),
        Property.Kettle(
            label="Kettle",
            description="Reduced logging if Kettle is inactive (only Kettle or Fermenter to be selected)",
        ),
        Property.Fermenter(
            label="Fermenter",
            description="Reduced logging in seconds if Fermenter is inactive (only Kettle or Fermenter to be selected)",
        ),
        Property.Number(
            label="ReducedLogging",
            configurable=True,
            description="Reduced logging frequency in seconds if selected Kettle or Fermenter is inactive (default: 60 sec | disabled: 0)",
        ),
    ]
)

class CustomSensor(CBPiSensor):

    def __init__(self, cbpi, id, props):
        super(CustomSensor, self).__init__(cbpi, id, props)
        self.value = 0
        self.temp = 0
        self.value_old = 0
        self.misoPin = 9
        self.mosiPin = 10
        self.clkPin = 11
        self.adc = self.props.get("ADC", "MAX31865")
        self.csPin = int(self.props.get("csPin", 17))
        self.ResSens = int(self.props.get("ResSens", 100 if self.adc == "ADS122C04" else 1000))
        self.offset = float(self.props.get("offset", 0))
        self.low_filter = float(self.props.get("ignore_below", 0))
        self.high_filter = float(self.props.get("ignore_above", 100))
        self.Wires = int(
            self.props.get("Wires", 4 if self.adc == "ADS122C04" else 2)
        )
        self.Interval = int(self.props.get("Interval", 5))
        self.delta_filter = float(self.props.get("ignore_delta", 0))
        if self.delta_filter < 0:
            self.delta_filter = 0

        self.alpha = float(self.props.get("alpha", 1))
        if self.alpha >= 1 or self.alpha <= 0:
            self.alpha = 1

        self.sample_count = int(self.props.get("sample_count", 5))
        if self.sample_count < 1:
            self.sample_count = 1

        self.sample_delay_ms = float(self.props.get("sample_delay_ms", 30))
        if self.sample_delay_ms < 0:
            self.sample_delay_ms = 0

        self.value_old = 9999
        self._adc_unavailable = False
        # defines how often a large delta can be rejected
        self.max_counter = 2
        # counts subsequent rejected values
        self.counter = 0

        self.lastlog = 0
        self.sensor = self.get_sensor(self.id)

        self.reducedfrequency = float(self.props.get("ReducedLogging", 60))
        if self.reducedfrequency < 0:
            self.reducedfrequency = 0

        self.kettleid = self.props.get("Kettle", None)
        self.fermenterid = self.props.get("Fermenter", None)

        self.reducedlogging = True if self.kettleid or self.fermenterid else False

        if self.kettleid is not None and self.fermenterid is not None:
            self.reducedlogging = False
            self.cbpi.notify(
                "OneWire Sensor",
                "Sensor '"
                + str(self.sensor.name)
                + "' cant't have Fermenter and Kettle defined for reduced logging.",
                NotificationType.WARNING,
                action=[NotificationAction("OK", self.Confirm)],
            )

        if (self.reducedfrequency != 0) and (self.Interval >= self.reducedfrequency):
            self.reducedlogging = False
            self.cbpi.notify(
                "OneWire Sensor",
                "Sensor '"
                + str(self.sensor.name)
                + "' has shorter or equal 'reduced logging' compared to regular interval.",
                NotificationType.WARNING,
                action=[NotificationAction("OK", self.Confirm)],
            )

        self.adc_driver = None
        try:
            if self.adc == "ADS122C04":
                from .ads122c04 import ADS122C04

                reference = self.props.get("ADSRefRest")
                if reference is None or (
                    isinstance(reference, str) and not reference.strip()
                ):
                    reference = self.props.get(
                        "RefRest", 4700 if self.ResSens == 1000 else 470
                    )
                self.RefRest = float(reference)
                if not math.isfinite(self.RefRest) or self.RefRest <= 0:
                    raise ValueError("ADSRefRest must be finite and positive")
                self.adc_driver = ADS122C04(
                    bus=int(self.props.get("I2CBus", 1)),
                    address=int(str(self.props.get("I2CAddress", 64)), 0),
                    rtd_nominal=self.ResSens,
                    ref_resistor=self.RefRest,
                    wires=self.Wires,
                )
            elif self.adc == "MAX31865":
                self.RefRest = float(self.props.get("RefRest", 4300))
                import board
                import digitalio
                from adafruit_max31865 import MAX31865

                spi = board.SPI()
                cs = digitalio.DigitalInOut(getattr(board, "D{}".format(self.csPin)))
                self.adc_driver = MAX31865(
                    spi,
                    cs,
                    rtd_nominal=self.ResSens,
                    ref_resistor=self.RefRest,
                    wires=self.Wires,
                )
            else:
                raise ValueError("Unknown ADC type: {}".format(self.adc))
        except Exception as exc:
            message = "Sensor '{}': failed to initialize {}: {}".format(
                self.sensor.name, self.adc, exc
            )
            self.cbpi.notify(
                "PT100X Sensor",
                message,
                NotificationType.ERROR,
                action=[NotificationAction("OK", self.Confirm)],
            )
            logging.error(message)

    def read(self):
        # get current Unit setting for temperature (Sensor needs to be saved again or system restarted for now)
        self.TEMP_UNIT = self.get_config_value("TEMP_UNIT", "C")
        if self.adc_driver is not None:
            temp = self.adc_driver.temperature
        else:
            raise RuntimeError("{} is not initialized".format(self.adc))

        if (
            self.TEMP_UNIT == "C"
        ):  # Report temp in C if nothing else is selected in settings
            temp = round((temp + self.offset), 2)
        else:  # Report temp in F if unit selected in settings
            temp = round((9.0 / 5.0 * temp + 32 + self.offset), 2)
        return temp

    async def read_median(self):
        values = []
        errors = []
        for index in range(self.sample_count):
            try:
                if self.adc == "ADS122C04":
                    values.append(await asyncio.to_thread(self.read))
                else:
                    values.append(self.read())
            except Exception as e:
                errors.append(e)

            if index < self.sample_count - 1 and self.sample_delay_ms > 0:
                await asyncio.sleep(self.sample_delay_ms / 1000)

        if len(values) == 0:
            last_value = self.value_old if self.value_old != 9999 else 0
            if not self._adc_unavailable:
                logging.error(
                    "Sensor {}: no valid readings from {} after {} attempts; "
                    "keeping value {}. Last error: {}".format(
                        self.sensor.name, self.adc, self.sample_count,
                        last_value, errors[-1]
                    )
                )
                self._adc_unavailable = True
            return last_value

        for error in errors:
            logging.warning(
                "Sensor {}: sampled read failed: {}".format(self.sensor.name, error)
            )
        if self._adc_unavailable:
            logging.info(
                "Sensor {}: readings from {} recovered".format(
                    self.sensor.name, self.adc
                )
            )
            self._adc_unavailable = False

        return round(statistics.median(values), 2)

    async def Confirm(self, **kwargs):
        pass

    async def run(self):
        self.kettle = (
            self.get_kettle(self.kettleid) if self.kettleid is not None else None
        )
        self.fermenter = (
            self.get_fermenter(self.fermenterid)
            if self.fermenterid is not None
            else None
        )

        while self.running == True:
            self.temp = await self.read_median()
            if self.value_old == 9999:
                self.value_old = self.temp

            if self.temp < self.low_filter or self.temp > self.high_filter:
                self.temp = self.value_old
            ## 0 deactivates delta Filter
            if self.delta_filter == 0:
                self.counter = 0
                self.value = round(
                    (self.temp * self.alpha + self.value_old * (1 - self.alpha)), 2
                )
                self.value_old = self.value
                self.push_update(self.value)
                if self.reducedlogging:
                    await self.logvalue()
                else:
                    self.log_data(self.value)
                    self.lastlog = time.time()
            # active delta filter
            else:
                delta = abs(self.temp - self.value_old)
                if delta < self.delta_filter:
                    self.value = round(
                        (self.temp * self.alpha + self.value_old * (1 - self.alpha)), 2
                    )
                    self.push_update(self.value)
                    if self.reducedlogging:
                        await self.logvalue()
                    else:
                        self.log_data(self.value)
                        self.lastlog = time.time()
                    self.value_old = self.value
                    self.counter = 0
                else:
                    logging.warning(
                        "Sensor {}: High Delta temp {}".format(
                            self.sensor.name, self.temp
                        )
                    )
                    if self.counter < self.max_counter:
                        self.value = self.value_old
                        self.push_update(self.value_old)
                        if self.reducedlogging:
                            await self.logvalue(self.value_old)
                        else:
                            self.log_data(self.value_old)
                            self.lastlog = time.time()
                        self.counter += 1
                    else:
                        self.counter = 0
                        self.value = round(
                            (
                                self.temp * self.alpha
                                + self.value_old * (1 - self.alpha)
                            ),
                            2,
                        )
                        self.value_old = self.value
                        self.log_data(self.value)
                        self.push_update(self.value)
                        if self.reducedlogging:
                            await self.logvalue()
                        else:
                            self.log_data(self.value)
                            self.lastlog = time.time()

            await asyncio.sleep(self.Interval)

    async def logvalue(self, value=None):
        currentvalue = self.value if value is None else value
        now = time.time()
        if self.kettle is not None:
            try:
                kettlestatus = self.kettle.instance.state
            except:
                kettlestatus = False
            if kettlestatus:
                self.log_data(currentvalue)
                logging.info("Kettle Active")
                self.lastlog = time.time()
            else:
                logging.info("Kettle Inactive")
                if self.reducedfrequency != 0:
                    if now >= self.lastlog + self.reducedfrequency:
                        self.log_data(currentvalue)
                        self.lastlog = time.time()
                        logging.info("Logged with reduced freqency")
                        pass

        if self.fermenter is not None:
            try:
                fermenterstatus = self.fermenter.instance.state
            except:
                fermenterstatus = False
            if fermenterstatus:
                self.log_data(currentvalue)
                logging.info("Fermenter Active")
                self.lastlog = time.time()
            else:
                logging.info("Fermenter Inactive")
                if self.reducedfrequency != 0:
                    if now >= self.lastlog + self.reducedfrequency:
                        self.log_data(currentvalue)
                        self.lastlog = time.time()
                        logging.info("Logged with reduced freqency")
                        pass

    def get_state(self):
        return dict(value=self.value)


def setup(cbpi):
    cbpi.plugin.register("PT100X_Sensor", CustomSensor)
    pass
