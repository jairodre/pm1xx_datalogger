# Data logger for pm1xx powermeter

Python script to adquire data from pm1xx powermeters. Tested on windows with python3.13.

Packages needed (may be missing someones): ` python3.13.exe -m pip install -U pyvisa pyvisa-py pyserial pyusb libusb-package matplotlib`

Usage: `python3.13.exe .\pm1xx_logger.py --duration 1h --sample-rate-hz 10 --wavelength-nm 1470 --bandwidth high --name 3.0A_1470nm_1h_t3`
