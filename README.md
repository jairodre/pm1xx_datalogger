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
