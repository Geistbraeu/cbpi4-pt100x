# Craftbeerpi4 Plugin for PT100/PT1000 Sensor

PT100 / PT1000 probes using MAX31865 (SPI) or ADS122C04 (I2C).
For MAX31865 wiring go to [this page](https://learn.adafruit.com/adafruit-max31865-rtd-pt100-amplifier/).

You will need to set the reference resistors on the craftbeerpi hardware page!  My max31865 chip uses a 4,3 kohm resistor for the PT1000. It might be 430 ohm for a PT100 board

Using a max31865 board like: https://learn.adafruit.com/adafruit-max31865-rtd-pt100-amplifier/

## Software installation:

This version requires Craftbeerpi V4 version 4.6.0 and above.

Please have a look at the [Craftbeerpi4 Documentation](https://openbrewing.gitbook.io/craftbeerpi4_support/readme/plugin-installation)

- Package name: cbpi4-pt100x
- Package link: https://github.com/PiBrewing/cbpi4-pt100x/archive/main.zip


## Hardware Installation:

- on your pi use the following GPIO pins:
- csPin = 8  *this one can be any GPIO you want, if using multiple probes you change this on each one, but keep the other 3 pins the same*
- misoPin = 9
- mosiPin = 10
- clkPin = 11

SPI must be enabled on the Pi via raspi-config for MAX31865.
ADS122C04 uses I2C instead and does not initialize SPI or a CS pin.

The code is installing the adafruit circuitpython max31865 library. It requires the RPi.GPIO package for older Pis (<=4) nd the rpi-lgpio pacakge for the Pi 5.

The installation routine (setup.py in cbpi has been modified to match these requirements). Existing installations on Pi4 and older boards must be redone, but your config can be still used (pipx uninstall cbpi4 and then install cbpi4 as described in the documentation. This will be updated later. Then install your plugins)

### ADS122C04 Wiring

Enable I2C on the Pi. Connect SDA/SCL directly to its I2C bus with compatible
supply/logic levels and a common ground. The driver uses AIN1-AIN2, gain 1
with PGA bypass, 20 SPS single-shot conversion, an external REFP/REFN
reference and 1.5 mA excitation from AIN0.

Use separate force and sense leads for a four-wire RTD measurement:

```text
AIN0 -------- RTD high-side force lead
AIN1 -------- RTD high-side sense lead
AIN2 -------- RTD low-side sense lead
REFP --+----- RTD low-side force lead
			 |
		 RREF (470 ohm for the PT100 example)
			 |
REFN --+----- AVSS / GND
```

REFN and the low side of RREF must connect to AVSS/GND to complete the
current path. The same current must pass through the RTD and RREF.
`Wires` is ignored for ADS122C04; there is no three-wire compensation.
The tested connection with two red leads and one shared white lead also
reads temperature, but includes the resistance of that shared lead.

For the PT100 example select `ResSens=100` and `RefRest=470`.
For PT1000 select `ResSens=1000` and use a suitable larger reference resistor,
for example `4700` ohm, not the PT100 example's 470 ohm resistor.

The plugin installs `smbus2`. It must be available in the Python environment
running CraftBeerPi, not just a separate test venv. After updating/reinstalling
the plugin, restart CraftBeerPi to load the new settings and driver.
The standalone `adstest.py` is not required by the plugin.

## Sensor Configuration

The sensor must be configured on the hardware page. The following parameters must be set and will affect your readings:

- ADC: `MAX31865` or `ADS122C04`. Existing sensors without this setting continue to use MAX31865.
- I2CBus: ADS122C04 I2C bus number (default `1`); ignored for MAX31865.
- I2CAddress: ADS122C04 address in decimal (default `64`, equivalent to `0x40`). Addresses 64 through 79 depend on the address straps; ignored for MAX31865.
- csPin: The BCM number of the GPIO your MAX31865 CS pin is connected to; ignored for ADS122C04.
- ResSens: Reistance in ohm of your sensor. Select 1000 for PT1000 or 100 for PT100.
- RefRest: Reference resistor in ohms. Options: `430`, `470`, `4300`, `4700`. MAX31865 typically uses 430 for PT100 or 4300 for PT1000; the ADS122C04 PT100 example uses 470. Select the actual resistor when changing ADC type; saved settings are not automatically replaced.
- offset: Defines a temperature offset for your temp probe.
- ignore_above: Ignores Temp readings above this value (0: deactivated)
- ignore_below: Ignores Temp readings below this value (0: deactivated)
- sample_count: Reads the sensor multiple times per cycle and uses the median value. Default is 5. Set to 1 to approximate single-read behavior.
- sample_delay_ms: Delay in milliseconds between sampled readings used for the median. Default is 30 ms.
- ignore_delta: If you experience issues with 'jumping' temp readings, you can define a threshold (only positive values are allowed) for the difference between two readings. If the difference between two subsequent readings is higher than the threshold, the value will be ignored. If the next reading is also higher (lower), this value is used / logged. (0: deactivated)
- alpha: calculates the average between 2 subsequent values. Must be between 0 and 1. (1: deactivated). 0.5 yields for instance in the average of two subsequent readings.
- Wires: MAX31865 uses the selected 2, 3 or 4 wire configuration. ADS122C04 ignores this setting and uses the fixed measurement configuration above.
- Interval: defines the frequency of sensor readings in seconds.
- Kettle/Fermenter: You can select a Kettle OR Fermenter if you want to activate reduced sensor logging during inactivity of the Kettle / Fermenter
- Reducedlogging: Defines the logging interval for reduced logging in seconds (e.g. 300 would result in logging every 5 Minutes when the Kettle / Fermenter logic is not active) (0: no sensor logging during inactivity)

Filtering order: first a median value is calculated from sampled readings (`sample_count` + `sample_delay_ms`). This median is then used for `ignore_below/ignore_above`, `ignore_delta`, and `alpha` processing.

Both ADC types use the same median filter. ADS122C04 conversions run in a
worker thread so waiting for the ADC does not block the asyncio event loop.
Invalid or timed-out samples are excluded. If all samples fail, the existing
filter returns the last value, or zero before the first valid value.

## Tests

Hardware-independent tests require `smbus2`, but no running CraftBeerPi server:

```bash
python -m unittest discover -s tests -v
```


Please note that the max board delivers readings above 900C if no sensor is connected to the board.

![Sensor Configuration](https://github.com/PiBrewing/cbpi4-pt100x/blob/main/Hardware_Configuration.png?raw=true)

### Changelog:

- 13.10.25: (0.2.1) Add pyproject.toml file to support pip 25.3+
- 20.03.25: (0.2.0) Retire the old mx31865 library and move to the adafruit-circuitpython-max31865 library. This should prevent the false readings and the jumps between data points (requires Server 4.6.0 for Pi4 and older boards)
- 25.06.23: (0.1.10) Better logging description on high delta value
- 09.06.23: (0.1.9) Updated descriptions for Sensor parameters on Hardware page. Updated README.
- 28.03.23: (0.1.8) No logging options in case of Kettle or Fermenter inactivity (set reducedlogging to 0)
- 25.03.23: (0.1.4) Reduced logging options in case of Kettle or Fermenter inactivity
- 10.05.22: (0.1.3) updated readme (removed cbpi add)
- 10.05.22: (0.1.2) Removed cbpi dependency
- 08.12.21: (0.1.1) Removed resampling as it caused issues with respect to long term stability (sensor unresponsivness)
- 01.12.21: (0.1.0) Improved handling of resampling in case of issues (test)	
- 01.11.21: (0.0.11) Experimental update under development branch: Read another sample if delta between two values is too high
- 10.06.21: (0.0.10) Calculating average of 2 subsequent values.
- 29.05.21: (0.0.9) Added filter function to filter out outliers
- 14.04.21: (0.0.8) Adaption for properties (updated dataclass.py in cbpi)
- 09.04.21: (0.0.7) Fix in offset handling in case value is not None but empty
- 15.03.21: (0.0.6) Change to supprt cbpi >= 4.0.0.31
- 20.02.21: (0.0.4) Change to support cbpi >= 4.0.0.24. Added sensor log fucntionality
- 14.02.21: (0.0.3) Updated function to retrieve temperature unit. Units will be changed automatically for sensor when settigs saved properly. No need to update the sensor 
- 09.02.21: (0.0.2) Added offset parameter and changed the GPIO.setmode. Currnetly, warnings are displayed during startup of CBPi. However, they cause typically no issues
- 05.02.21: (0.0.1) Adpated code to run under cbpi4

