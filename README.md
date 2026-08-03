## Data logger and plotter for pm1xx powermeter

Python script to adquire data from pm1xx powermeters. Tested on Windows with python3.13.

Packages needed (may be missing someones): ` python3.13.exe -m pip install -U pyvisa pyvisa-py pyserial pyusb libusb-package matplotlib`

**Usage**: `python3.13.exe .\pm1xx_logger.py --duration 1h --sample-rate-hz 10 --wavelength-nm 1470 --bandwidth high --name 3.0A_1470nm_1h_t3`

There is also a plotting code that can use the output of the logger.

- **For plotting all csv in a folder**:

`python3.13.exe .\plot_laserdriver_csv.py --folder .\Power_Test_CSVs --band-start 4min --y-min 1.0 --y-max 1.12 --all-in-folder --all-folder-band-mode individual`

Result with options above:

<img width="1375" height="875" alt="Power_Test_CSVs_overlay_plot" src="https://github.com/user-attachments/assets/be3b1d99-0b23-4b9a-813e-6f7047965ef5" />  

- **Individual files plotting**:

`python3.13.exe .\plot_laserdriver_csv.py --file .\Power_Test_CSVs\3.0A_1470nm_1h_t2_20260313_162207.csv --band-start 4min --y-min 1.0 --y-max 1.12 --all-folder-band-mode individual`

Result with options above:

<img width="1375" height="875" alt="3 0A_1470nm_1h_t2_20260313_162207_plot" src="https://github.com/user-attachments/assets/20459098-c2c9-499f-b6f8-4c43125197ad" />

## GUI Logger Alternative: Multi-Sensor Acquisition

`pm1xx_temperature_gui.py` is a graphical alternative to the command-line logger. It does not replace `pm1xx_logger.py`.

It supports Thorlabs PM100/PM400-compatible VISA meters and provides:

- Duration, sample rate, wavelength, bandwidth, output filename, and output-folder controls.
- Refreshable list of discovered meters with checkboxes.
- One synchronized acquisition from all checked meters.
- Optional sensor-head temperature logging, only when the connected sensor advertises temperature capability.
- Live multi-trace power plot with mean, min/max, standard deviation, and ΔP.
- Saved-CSV plotting with the same statistics.

### Requirements

Install the same VISA backend required by the command-line logger, plus Matplotlib:

```powershell
pip install pyvisa matplotlib
```

Tkinter is normally included with the standard Windows Python installer. A working VISA installation and the Thorlabs USB driver are required for meter discovery.

### Run

```powershell
python pm1xx_temperature_gui.py
```

1. Click **Refresh Resources**.
2. Check the meter(s) to acquire.
3. Set duration, sample rate, wavelength, bandwidth, filename, and save folder.
4. Optionally enable **Log and print sensor temperature**.
5. Click **Start Logging**.

The selected output folder is created automatically if it does not exist.

### Combined Multi-Sensor CSV Format

One CSV is written per acquisition using the original filename pattern:

```text
<name>_<YYYYMMDD_HHMMSS>.csv
```

For example:

```text
1470nm_0.75W_ref_20260803_153000.csv
```

Metadata appears first. The data-column header is directly above the data rows.

```text
Time [ms]    Power [W] P0044739    Power [W] P0051234    Temperature [C] P0044739    Temperature [C] P0051234
0            5.432E-01            6.118E-01            20.90                         21.15
200          5.441E-01            6.127E-01            20.91                         21.16
```

Column order is always:

```text
time, power sensor 1, power sensor 2, ..., temperature sensor 1, temperature sensor 2, ...
```

Temperature columns are omitted for sensors without a supported temperature capability, or when temperature logging is unchecked.

### Plotting

The GUI’s **Saved CSV Plot** panel reads both legacy two-column logger files and the combined multi-sensor CSV format. It plots every `Power [W] <serial>` column as a separate trace.

`plot_laserdriver_csv.py` remains unchanged and is intended for the original two-column CSV format.
