#!/usr/bin/env python3
"""Parallel GUI logger for Thorlabs PM1xx/PM400 meters.

This program does not modify pm1xx_logger.py or plot_laserdriver_csv.py.
It writes one synchronized CSV for all checked meters:
    time [ms]    power 1 ... power N    temperature 1 ... temperature N
Temperature columns are included only when requested and supported by each
connected sensor.

Run it with the same Python environment that has pyvisa, matplotlib, and Tk:
    python pm1xx_gui.py
"""

from __future__ import annotations

import math
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pyvisa
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure


LONG_RUN_THRESHOLD_S = 3600.0
LONG_RUN_LIVE_WINDOW_S = 30.0 * 60.0
LONG_RUN_RENDER_PERIOD_S = 1.0


@dataclass(frozen=True)
class MeterInfo:
    resource: str
    model: str
    serial: str


@dataclass
class RunningPowerStats:
    """Numerically stable full-run statistics without retaining all samples."""

    count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    minimum: float = float("inf")
    maximum: float = float("-inf")

    def add(self, value: float) -> None:
        if not math.isfinite(value):
            return
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)

    @property
    def std(self) -> float:
        return math.sqrt(self.m2 / self.count) if self.count else float("nan")

    @property
    def delta(self) -> float:
        return self.maximum - self.minimum if self.count else float("nan")


def infer_serial_from_resource(resource: str) -> str:
    match = re.search(r"::([A-Za-z0-9_-]+)::INSTR$", resource, re.IGNORECASE)
    return match.group(1) if match else resource


def query_float(inst: Any, command: str) -> float:
    try:
        return float(inst.query(command).strip())
    except Exception:
        return float("nan")


def query_text(inst: Any, command: str) -> str:
    try:
        return inst.query(command).strip()
    except Exception:
        return ""


def query_sensor_id(inst: Any) -> str:
    for command in ("SYST:SENS:IDN?", "SYST:SENS:TYPE?", "SENS:IDN?"):
        value = query_text(inst, command)
        if value:
            return value
    return "UNKNOWN"


def sensor_has_temperature(sensor_id: str) -> bool:
    """Safely determine temperature support from SYST:SENS:IDN? sensor flags.

    Thorlabs PM100-family sensor identification has six comma-separated
    fields. The final field is a bit mask, and bit 256 means a head
    temperature sensor is present. An unparseable or shorter response is
    treated as *not supported* so that MEAS:TEMP? is never sent blindly.
    """
    fields = [field.strip() for field in sensor_id.split(",")]
    if len(fields) < 6:
        return False
    try:
        flags = int(fields[5], 0)
    except ValueError:
        return False
    return bool(flags & 256)


def query_range_mode(inst: Any) -> str:
    for command in ("SENS:POW:RANG:AUTO?", "SENSE:POW:RANGE:AUTO?"):
        auto = query_text(inst, command).upper()
        if auto in {"1", "ON"}:
            return "AUTO"
        if auto in {"0", "OFF"}:
            manual_range_w = query_float(inst, "SENS:POW:RANG?")
            return (
                f"MANUAL {manual_range_w:.6g}W"
                if math.isfinite(manual_range_w)
                else "MANUAL"
            )
    return "UNKNOWN"


def set_bandwidth(inst: Any, mode: str) -> str:
    target = "LOW" if mode.lower() == "low" else "HIGH"
    for command in (
        f"SENSE:POW:DC:BANDWIDTH {target}",
        f"SENSE:POW:DC:BANDWIDTH {target[0]}",
        f"SENS:POW:DC:BAND {target}",
    ):
        try:
            inst.write(command)
            return target
        except Exception:
            pass
    return "UNSET"


def discover_meters(rm: pyvisa.ResourceManager) -> list[MeterInfo]:
    """Return only VISA resources that identify as supported Thorlabs meters."""
    found: list[MeterInfo] = []
    for resource in rm.list_resources():
        upper = resource.upper()
        is_usb_like = "USB" in upper or "0X1313" in upper or (
            "INSTR" in upper and "ASRL" not in upper
        )
        if not is_usb_like:
            continue

        inst = None
        try:
            inst = rm.open_resource(resource, timeout=3000, chunk_size=1024)
            inst.write_termination = "\n"
            inst.read_termination = "\n"
            idn = query_text(inst, "*IDN?")
            idn_upper = idn.upper()
            if "PM100" not in idn_upper and "PM400" not in idn_upper:
                if "0X1313" not in upper:
                    continue
                found.append(
                    MeterInfo(resource, "PM-UNKNOWN", infer_serial_from_resource(resource))
                )
                continue
            parts = [part.strip() for part in idn.split(",")]
            model = parts[1] if len(parts) > 1 else "PM-UNKNOWN"
            serial = parts[2] if len(parts) > 2 else infer_serial_from_resource(resource)
            found.append(MeterInfo(resource, model, serial))
        except Exception:
            continue
        finally:
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass
    return found


def parse_duration(number_text: str, unit: str) -> tuple[float, str]:
    try:
        number = float(number_text.strip())
    except ValueError as exc:
        raise ValueError("Duration must be a number.") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Duration must be greater than zero.")
    seconds_per_unit = {"s": 1.0, "min": 60.0, "h": 3600.0}
    return number * seconds_per_unit[unit], f"{number:g}{unit}"


def parse_optional_float(text: str, field_name: str) -> float | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        value = float(stripped)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a number or left blank.") from exc
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite.")
    return value


def parse_optional_minutes(number_text: str, unit: str, field_name: str) -> float | None:
    value = parse_optional_float(number_text, field_name)
    if value is None:
        return None
    if value < 0:
        raise ValueError(f"{field_name} cannot be negative.")
    return value * {"s": 1.0 / 60.0, "min": 1.0, "h": 60.0}[unit]


def make_csv_path(folder: Path, base_name: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = base_name.strip() or "pm1xx"
    return folder / f"{safe_name}_{timestamp}.csv"


def extract_unit(header_text: str) -> str | None:
    match = re.search(r"\[(.*?)\]", header_text)
    return match.group(1).strip() if match else None


@dataclass
class PowerSeries:
    label: str
    times: list[float]
    powers: list[float]
    time_unit: str | None
    power_unit: str | None


def load_power_csv_series(path: Path) -> list[PowerSeries]:
    """Read original two-column files and combined multi-sensor GUI files."""
    header_parts: list[str] | None = None
    time_index: int | None = None
    power_indexes: list[int] = []
    time_unit: str | None = None
    power_units: dict[int, str | None] = {}
    powers_by_index: dict[int, list[float]] = {}
    times: list[float] = []

    with path.open("r", encoding="utf-8-sig", errors="ignore") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            parts = re.split(r"\t+", text)
            lower_parts = [part.lower() for part in parts]
            candidate_time = next((i for i, part in enumerate(lower_parts) if "time" in part), None)
            candidate_powers = [
                i for i, part in enumerate(lower_parts)
                if "power" in part or "value unit" in part
            ]
            if candidate_time is not None and candidate_powers:
                header_parts = parts
                time_index = candidate_time
                power_indexes = candidate_powers
                time_unit = extract_unit(parts[time_index])
                power_units = {index: extract_unit(parts[index]) for index in power_indexes}
                powers_by_index = {index: [] for index in power_indexes}
                continue

            if header_parts is None or time_index is None:
                continue
            try:
                time_value = float(parts[time_index])
                power_values = {index: float(parts[index]) for index in power_indexes}
            except (ValueError, IndexError):
                continue
            times.append(time_value)
            for index, value in power_values.items():
                powers_by_index[index].append(value)

    if not times or header_parts is None:
        raise ValueError(f"No numeric power/time rows found in {path.name}")

    series: list[PowerSeries] = []
    for index in power_indexes:
        # Header tail after [W] is the meter serial in the combined format.
        tail = re.sub(r"^.*?\]", "", header_parts[index]).strip()
        label = f"{path.stem} - {tail}" if tail else path.stem
        series.append(PowerSeries(label, list(times), powers_by_index[index], time_unit, power_units[index]))
    return series


def time_to_minutes_factor(unit: str | None) -> float:
    normalized = (unit or "ms").strip().lower()
    return {
        "ns": 1.0 / (60.0 * 1e9),
        "us": 1.0 / (60.0 * 1e6),
        "ms": 1.0 / 60000.0,
        "s": 1.0 / 60.0,
        "sec": 1.0 / 60.0,
        "min": 1.0,
        "h": 60.0,
        "hr": 60.0,
    }.get(normalized, 1.0)


def value_to_mw_factor(unit: str | None) -> float:
    normalized = (unit or "W").strip().lower()
    return {"w": 1000.0, "mw": 1.0, "uw": 0.001, "nw": 0.000001}.get(normalized, 1.0)


def band_stats(values: list[float]) -> tuple[float, float, float, float]:
    low = min(values)
    high = max(values)
    mean = sum(values) / len(values)
    std = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
    return low, high, std, high - low


class PM1xxTemperatureGUI(tk.Tk):
    """Acquisition panel, live power plot, and independent saved-CSV plot panel."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Thorlabs PM1xx Logger - Power, Time, Temperature")
        self.geometry("1500x880")
        self.minsize(1180, 720)

        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False
        self.discovered_meters: list[MeterInfo] = []
        self.meter_selected_vars: dict[str, tk.BooleanVar] = {}
        self.live_data: dict[str, dict[str, Any]] = {}
        self.live_dirty = False
        self.live_time_unit = "s"
        self.long_run_live_mode = False
        self.last_live_render_time = 0.0

        self.duration_value = tk.StringVar(value="3")
        self.duration_unit = tk.StringVar(value="min")
        self.sample_rate = tk.StringVar(value="5")
        self.wavelength = tk.StringVar(value="1470")
        self.bandwidth = tk.StringVar(value="low")
        self.file_name = tk.StringVar(value="1470nm_0.75W_ref")
        self.save_folder = tk.StringVar(value=str(Path.cwd() / "Power_Test_CSVs"))
        self.include_temperature = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Choose Refresh Resources to find connected meters.")
        self.live_readout = tk.StringVar(value="Latest measurement: waiting to start.")

        self.plot_folder = tk.StringVar(value=str(Path.cwd() / "Power_Test_CSVs"))
        self.plot_all_folder = tk.BooleanVar(value=True)
        self.plot_file = tk.StringVar()
        self.plot_band_start = tk.StringVar(value="0.1")
        self.plot_band_start_unit = tk.StringVar(value="min")
        self.plot_band_end = tk.StringVar()
        self.plot_band_end_unit = tk.StringVar(value="min")
        self.plot_y_min = tk.StringVar(value="")
        self.plot_y_max = tk.StringVar(value="")
        self.plot_band_mode = tk.StringVar(value="individual")

        self._build_interface()
        self.after(80, self._process_events)

    def _build_interface(self) -> None:
        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        left = ttk.Frame(main, padding=8)
        right = ttk.Frame(main, padding=4)
        main.add(left, weight=0)
        main.add(right, weight=1)

        acquisition = ttk.LabelFrame(left, text="Power Meter Acquisition", padding=10)
        acquisition.pack(fill=tk.X)
        acquisition.columnconfigure(1, weight=1)

        self._labeled_entry(acquisition, 0, "Duration", self.duration_value, width=12)
        self.duration_box = ttk.Combobox(
            acquisition, textvariable=self.duration_unit, values=("s", "min", "h"), state="readonly", width=8
        )
        self.duration_box.grid(row=0, column=2, padx=(4, 0), pady=4, sticky="w")

        ttk.Label(acquisition, text="Sample rate").grid(row=1, column=0, sticky="w", pady=4)
        self.sample_box = ttk.Combobox(
            acquisition, textvariable=self.sample_rate, values=("1", "2", "3", "4", "5"), state="readonly", width=12
        )
        self.sample_box.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(acquisition, text="Hz").grid(row=1, column=2, padx=(4, 0), sticky="w")

        self._labeled_entry(acquisition, 2, "Wavelength", self.wavelength, width=12)
        ttk.Label(acquisition, text="nm").grid(row=2, column=2, padx=(4, 0), sticky="w")

        ttk.Label(acquisition, text="Bandwidth").grid(row=3, column=0, sticky="w", pady=4)
        self.bandwidth_box = ttk.Combobox(
            acquisition, textvariable=self.bandwidth, values=("low", "high"), state="readonly", width=12
        )
        self.bandwidth_box.grid(row=3, column=1, sticky="ew", pady=4)

        self._labeled_entry(acquisition, 4, "File name", self.file_name, width=24)

        ttk.Label(acquisition, text="Save folder").grid(row=5, column=0, sticky="nw", pady=4)
        save_folder_row = ttk.Frame(acquisition)
        save_folder_row.grid(row=5, column=1, columnspan=2, sticky="ew", pady=4)
        save_folder_row.columnconfigure(0, weight=1)
        ttk.Entry(save_folder_row, textvariable=self.save_folder, width=32).grid(row=0, column=0, sticky="ew")
        ttk.Button(save_folder_row, text="Browse", command=self._browse_save_folder).grid(row=0, column=1, padx=(5, 0))

        ttk.Label(acquisition, text="Meters to log").grid(row=6, column=0, sticky="nw", pady=4)
        resource_row = ttk.Frame(acquisition)
        resource_row.grid(row=6, column=1, columnspan=2, sticky="ew", pady=4)
        resource_row.columnconfigure(0, weight=1)
        ttk.Label(resource_row, text="Check every connected meter to acquire.").grid(row=0, column=0, sticky="w")
        self.refresh_button = ttk.Button(resource_row, text="Refresh Resources", command=self._refresh_resources)
        self.refresh_button.grid(row=0, column=1, padx=(5, 0))

        meter_list = ttk.Frame(acquisition)
        meter_list.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(1, 4))
        meter_list.columnconfigure(0, weight=1)
        self.meter_canvas = tk.Canvas(meter_list, height=82, highlightthickness=1, highlightbackground="#c8c8c8")
        meter_scroll = ttk.Scrollbar(meter_list, orient=tk.VERTICAL, command=self.meter_canvas.yview)
        self.meter_canvas.configure(yscrollcommand=meter_scroll.set)
        self.meter_canvas.grid(row=0, column=0, sticky="ew")
        meter_scroll.grid(row=0, column=1, sticky="ns")
        self.meter_checks_frame = ttk.Frame(self.meter_canvas)
        self._meter_checks_window = self.meter_canvas.create_window((0, 0), window=self.meter_checks_frame, anchor="nw")
        self.meter_checks_frame.bind("<Configure>", self._resize_meter_checks)
        self.meter_canvas.bind("<Configure>", self._resize_meter_checks)
        meter_actions = ttk.Frame(acquisition)
        meter_actions.grid(row=8, column=0, columnspan=3, sticky="w", pady=(0, 4))
        ttk.Button(meter_actions, text="Select All", command=lambda: self._set_all_meter_checks(True)).pack(side=tk.LEFT)
        ttk.Button(meter_actions, text="Clear All", command=lambda: self._set_all_meter_checks(False)).pack(side=tk.LEFT, padx=(5, 0))

        ttk.Checkbutton(
            acquisition,
            text="Log and print sensor temperature (only if supported)",
            variable=self.include_temperature,
        ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(5, 0))

        controls = ttk.Frame(acquisition)
        controls.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.start_button = ttk.Button(controls, text="Start Logging", command=self._start_logging)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(controls, text="Stop Safely", command=self._request_stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=(6, 0))

        log_box = ttk.LabelFrame(left, text="Acquisition Information", padding=5)
        log_box.pack(fill=tk.BOTH, expand=True)
        ttk.Label(log_box, textvariable=self.live_readout, anchor="w").pack(fill=tk.X, pady=(0, 4))
        self.log_view = ScrolledText(log_box, width=57, height=28, wrap=tk.WORD, state=tk.DISABLED)
        self.log_view.pack(fill=tk.BOTH, expand=True)

        live_box = ttk.LabelFrame(right, text="Live Power Plot", padding=3)
        live_box.pack(fill=tk.BOTH, expand=True)
        self.live_figure = Figure(figsize=(8.5, 5.0), dpi=100)
        self.live_axes = self.live_figure.add_subplot(111)
        self._draw_empty_live_plot()
        self.live_canvas = FigureCanvasTkAgg(self.live_figure, master=live_box)
        self.live_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        plot_box = ttk.LabelFrame(right, text="Saved CSV Plot (Independent of Acquisition)", padding=8)
        plot_box.pack(fill=tk.X, pady=(8, 0))
        for column in range(5):
            plot_box.columnconfigure(column, weight=1 if column in (1, 3) else 0)

        ttk.Label(plot_box, text="CSV folder").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(plot_box, textvariable=self.plot_folder, width=45).grid(row=0, column=1, columnspan=3, sticky="ew", pady=3)
        ttk.Button(plot_box, text="Browse", command=self._browse_plot_folder).grid(row=0, column=4, padx=(5, 0))

        self.all_folder_check = ttk.Checkbutton(
            plot_box, text="All CSV files in folder", variable=self.plot_all_folder, command=self._update_plot_file_state
        )
        self.all_folder_check.grid(row=1, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Label(plot_box, text="Band mode").grid(row=1, column=2, sticky="e", pady=3)
        ttk.Combobox(plot_box, textvariable=self.plot_band_mode, values=("global", "individual"), state="readonly", width=12).grid(
            row=1, column=3, sticky="w", pady=3
        )

        ttk.Label(plot_box, text="One CSV file").grid(row=2, column=0, sticky="w", pady=3)
        self.plot_file_entry = ttk.Entry(plot_box, textvariable=self.plot_file, width=45)
        self.plot_file_entry.grid(row=2, column=1, columnspan=3, sticky="ew", pady=3)
        self.plot_file_browse = ttk.Button(plot_box, text="Browse", command=self._browse_plot_file)
        self.plot_file_browse.grid(row=2, column=4, padx=(5, 0))

        ttk.Label(plot_box, text="Band start").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Entry(plot_box, textvariable=self.plot_band_start, width=10).grid(row=3, column=1, sticky="w", pady=3)
        ttk.Combobox(plot_box, textvariable=self.plot_band_start_unit, values=("s", "min", "h"), state="readonly", width=7).grid(
            row=3, column=1, padx=(82, 0), sticky="w", pady=3
        )
        ttk.Label(plot_box, text="Band end (optional)").grid(row=3, column=2, sticky="e", pady=3)
        ttk.Entry(plot_box, textvariable=self.plot_band_end, width=10).grid(row=3, column=3, sticky="w", pady=3)
        ttk.Combobox(plot_box, textvariable=self.plot_band_end_unit, values=("s", "min", "h"), state="readonly", width=7).grid(
            row=3, column=3, padx=(82, 0), sticky="w", pady=3
        )

        ttk.Label(plot_box, text="Y min (optional)").grid(row=4, column=0, sticky="w", pady=3)
        ttk.Entry(plot_box, textvariable=self.plot_y_min, width=13).grid(row=4, column=1, sticky="w", pady=3)
        ttk.Label(plot_box, text="Y max (optional)").grid(row=4, column=2, sticky="e", pady=3)
        ttk.Entry(plot_box, textvariable=self.plot_y_max, width=13).grid(row=4, column=3, sticky="w", pady=3)
        ttk.Button(plot_box, text="Plot and Save PNG", command=self._plot_saved_csv).grid(row=4, column=4, padx=(5, 0))
        ttk.Label(plot_box, text="Blank plot fields are ignored. Legend is placed outside the plot on the right.").grid(
            row=5, column=0, columnspan=5, sticky="w", pady=(5, 0)
        )
        self._update_plot_file_state()

    @staticmethod
    def _labeled_entry(parent: ttk.LabelFrame, row: int, label: str, variable: tk.StringVar, width: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable, width=width).grid(row=row, column=1, sticky="ew", pady=4)

    def _append_log(self, message: str) -> None:
        self.log_view.configure(state=tk.NORMAL)
        self.log_view.insert(tk.END, message.rstrip() + "\n")
        self.log_view.see(tk.END)
        self.log_view.configure(state=tk.DISABLED)

    def _resize_meter_checks(self, _event: tk.Event[Any] | None = None) -> None:
        self.meter_canvas.configure(scrollregion=self.meter_canvas.bbox("all"))
        self.meter_canvas.itemconfigure(self._meter_checks_window, width=self.meter_canvas.winfo_width())

    def _set_all_meter_checks(self, selected: bool) -> None:
        for variable in self.meter_selected_vars.values():
            variable.set(selected)

    def _refresh_resources(self) -> None:
        if self.running:
            return
        self.refresh_button.configure(state=tk.DISABLED)
        self.status.set("Looking for valid Thorlabs PM1xx/PM400 VISA resources...")

        def worker() -> None:
            rm = None
            try:
                rm = pyvisa.ResourceManager()
                self.events.put(("resources", discover_meters(rm)))
            except Exception as exc:
                self.events.put(("error", f"Resource refresh failed: {exc}"))
            finally:
                if rm is not None:
                    try:
                        rm.close()
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def _set_resources(self, meters: list[MeterInfo]) -> None:
        self.refresh_button.configure(state=tk.NORMAL)
        self.discovered_meters = meters
        self.meter_selected_vars.clear()
        for child in self.meter_checks_frame.winfo_children():
            child.destroy()

        if meters:
            for row, meter in enumerate(meters):
                variable = tk.BooleanVar(value=True)
                self.meter_selected_vars[meter.resource] = variable
                ttk.Checkbutton(
                    self.meter_checks_frame,
                    text=f"{meter.model} {meter.serial} @ {meter.resource}",
                    variable=variable,
                ).grid(row=row, column=0, sticky="w", padx=4, pady=1)
            self.status.set(f"Found {len(meters)} valid power meter resource(s); all are checked.")
            self._append_log("Available meters:")
            for meter in meters:
                self._append_log(f"  {meter.model} {meter.serial} @ {meter.resource}")
        else:
            self.status.set("No valid PM1xx/PM400 VISA resources found.")
            self._append_log("No valid PM1xx/PM400 VISA resources found.")
        self._resize_meter_checks()

    def _start_logging(self) -> None:
        if self.running:
            return
        selected_meters = [
            meter for meter in self.discovered_meters
            if self.meter_selected_vars.get(meter.resource) and self.meter_selected_vars[meter.resource].get()
        ]
        if not selected_meters:
            messagebox.showwarning("Choose meter(s)", "Refresh resources and check at least one meter to log.")
            return
        try:
            duration_seconds, duration_text = parse_duration(self.duration_value.get(), self.duration_unit.get())
            sample_rate = float(self.sample_rate.get())
            wavelength_nm = float(self.wavelength.get().strip())
            if not math.isfinite(wavelength_nm) or wavelength_nm <= 0:
                raise ValueError("Wavelength must be greater than zero.")
        except ValueError as exc:
            messagebox.showerror("Acquisition settings", str(exc))
            return

        settings = {
            "meters": selected_meters,
            "duration_seconds": duration_seconds,
            "duration_text": duration_text,
            "sample_rate": sample_rate,
            "wavelength_nm": wavelength_nm,
            "bandwidth": self.bandwidth.get(),
            "file_name": self.file_name.get(),
            "save_folder": Path(self.save_folder.get().strip() or "Power_Test_CSVs").expanduser(),
            "temperature_requested": self.include_temperature.get(),
            "long_run_live_mode": duration_seconds > LONG_RUN_THRESHOLD_S,
        }
        self.live_time_unit = self.duration_unit.get()
        self.long_run_live_mode = settings["long_run_live_mode"]
        self.last_live_render_time = 0.0
        self.live_data.clear()
        self.live_readout.set("Latest measurement: connecting to selected meter...")
        self._draw_empty_live_plot()
        self.live_canvas.draw_idle()
        self.stop_event.clear()
        self.running = True
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.refresh_button.configure(state=tk.DISABLED)
        self.status.set("Acquisition running...")
        threading.Thread(target=self._acquisition_worker, args=(settings,), daemon=True).start()

    def _request_stop(self) -> None:
        if self.running:
            self.stop_event.set()
            self.stop_button.configure(state=tk.DISABLED)
            self.status.set("Stopping after the current measurement...")

    def _acquisition_worker(self, settings: dict[str, Any]) -> None:
        meters: list[MeterInfo] = settings["meters"]
        rm = None
        opened: list[dict[str, Any]] = []
        handle = None
        csv_path: Path | None = None
        try:
            rm = pyvisa.ResourceManager()
            self.events.put(("log", "Selected meters:"))
            self.events.put(("log", "Attached sensors:"))
            used_labels: set[str] = set()
            for meter in meters:
                inst = rm.open_resource(meter.resource, timeout=5000, chunk_size=4096)
                inst.write_termination = "\n"
                inst.read_termination = "\n"
                label = meter.serial or infer_serial_from_resource(meter.resource)
                base_label = label
                suffix = 2
                while label in used_labels:
                    label = f"{base_label}_{suffix}"
                    suffix += 1
                used_labels.add(label)
                sensor_id = query_sensor_id(inst)
                temperature_enabled = bool(settings["temperature_requested"]) and sensor_has_temperature(sensor_id)
                self.events.put(("log", f"  {meter.model} {label} @ {meter.resource}"))
                self.events.put(("log", f"  meter {label}: {sensor_id}"))
                if temperature_enabled:
                    self.events.put(("log", f"  temperature {label}: enabled"))
                elif settings["temperature_requested"]:
                    self.events.put(("log", f"  temperature {label}: skipped safely (not advertised by sensor)"))

                try:
                    inst.write(f"SENSE:CORR:WAV {settings['wavelength_nm']}")
                except Exception as exc:
                    self.events.put(("log", f"Warning: failed to set wavelength for {label}: {exc}"))
                opened.append({
                    "meter": meter,
                    "inst": inst,
                    "label": label,
                    "sensor_id": sensor_id,
                    "temperature_enabled": temperature_enabled,
                    "bandwidth": set_bandwidth(inst, settings["bandwidth"]),
                    "wavelength_nm": query_float(inst, "SENSE:CORR:WAV?"),
                    "range_mode": query_range_mode(inst),
                })

            if not settings["temperature_requested"]:
                self.events.put(("log", "Temperature logging: disabled by checkbox."))

            csv_path = make_csv_path(settings["save_folder"], settings["file_name"])
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            handle = csv_path.open("w", encoding="utf-8", newline="")
            started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            handle.write(f"combined PM log\t{started_at}\n")
            handle.write("sensors\t" + "\t".join(
                f"{record['label']}={record['sensor_id']}" for record in opened
            ) + "\n")
            # Grouped columns: one time, all powers, then all supported temperatures.
            column_headers = ["Time [ms]"]
            column_headers.extend(f"Power [W] {record['label']}" for record in opened)
            column_headers.extend(
                f"Temperature [C] {record['label']}"
                for record in opened if record["temperature_enabled"]
            )
            for record in opened:
                wavelength = record["wavelength_nm"]
                wavelength_text = f"{wavelength:.3f}nm" if math.isfinite(wavelength) else "UNKNOWN"
                meter = record["meter"]
                handle.write(f"wavelength {record['label']}\t{wavelength_text}\n")
                handle.write(f"range {record['label']}\t{record['range_mode']}\n")
                handle.write(f"meter {record['label']}\t{meter.model} {meter.serial}\n")
                handle.write(f"bandwidth {record['label']}\t{record['bandwidth']}\n")
            handle.write(f"sample_rate\t{settings['sample_rate']:.6f}Hz\n")
            handle.write(f"duration\t{settings['duration_text']}\n")
            # Keep the data schema immediately above the first acquired row.
            handle.write("\t".join(column_headers) + "\n")

            self.events.put(("log", f"Logging {len(opened)} meter(s) for {settings['duration_seconds']:.2f} s at {settings['sample_rate']:.3f} Hz..."))
            if settings["long_run_live_mode"]:
                self.events.put((
                    "log",
                    "Long-run live mode: plotting the latest 30 minutes; live statistics cover the full run and refresh once per second.",
                ))
            sample_period = 1.0 / settings["sample_rate"]
            t0 = time.perf_counter()
            while not self.stop_event.is_set():
                loop_start = time.perf_counter()
                elapsed = loop_start - t0
                if elapsed >= settings["duration_seconds"]:
                    break
                elapsed_ms = elapsed * 1000.0
                powers: list[float] = []
                temperatures: list[float] = []
                power_text: list[str] = []
                temperature_text: list[str] = []
                for record in opened:
                    power_w = query_float(record["inst"], "MEAS:POW?")
                    powers.append(power_w)
                    power_text.append(f"{record['label']} {power_w:.3e} W")
                    self.events.put(("live", (record["label"], elapsed, power_w)))
                for record in opened:
                    if record["temperature_enabled"]:
                        temperature_c = query_float(record["inst"], "MEAS:TEMP?")
                        temperatures.append(temperature_c)
                        temperature_text.append(f"{record['label']} {temperature_c:.2f} C")
                row = [f"{elapsed_ms:.0f}"]
                row.extend(f"{value:.6E}" for value in powers)
                row.extend(f"{value:.3f}" for value in temperatures)
                handle.write("\t".join(row) + "\n")
                handle.flush()
                display_line = f"{elapsed:6.2f}s  " + "  |  ".join(power_text + temperature_text)
                self.events.put(("status_line", display_line))
                remaining = sample_period - (time.perf_counter() - loop_start)
                if remaining > 0:
                    self.stop_event.wait(remaining)

            if self.stop_event.is_set():
                self.events.put(("log", "Ctrl+C equivalent requested. Stopping acquisition safely..."))
            self.events.put(("log", "Saved combined PM log file:"))
            self.events.put(("log", f"  {csv_path.resolve()}"))
            if not self.stop_event.is_set():
                self.events.put(("log", "Acquisition complete."))
        except Exception as exc:
            self.events.put(("error", f"Acquisition failed: {exc}"))
        finally:
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
            for record in opened:
                try:
                    record["inst"].close()
                except Exception:
                    pass
            if rm is not None:
                try:
                    rm.close()
                except Exception:
                    pass
            self.events.put(("finished", None))

    def _draw_empty_live_plot(self) -> None:
        self.live_axes.clear()
        title = "Live Power"
        if self.long_run_live_mode:
            title += " (Latest 30 min; Full-Run Statistics)"
        self.live_axes.set_title(title)
        self.live_axes.set_xlabel(f"Time ({self.live_time_unit})")
        self.live_axes.set_ylabel("Power (W)")
        self.live_axes.grid(True, alpha=0.25)
        # Compact outer whitespace while preserving a right-side legend column.
        self.live_figure.subplots_adjust(left=0.09, right=0.70, bottom=0.11, top=0.91)

    def _render_live_plot(self) -> None:
        self._draw_empty_live_plot()
        any_data = False
        seconds_to_display = {"s": 1.0, "min": 1.0 / 60.0, "h": 1.0 / 3600.0}[self.live_time_unit]
        for serial, series in sorted(self.live_data.items()):
            if series["time"]:
                if self.long_run_live_mode:
                    stats: RunningPowerStats = series["stats"]
                    if stats.count:
                        low, high, std, delta, mean = (
                            stats.minimum,
                            stats.maximum,
                            stats.std,
                            stats.delta,
                            stats.mean,
                        )
                    else:
                        low = high = std = delta = mean = float("nan")
                else:
                    finite_powers = [value for value in series["power"] if math.isfinite(value)]
                    if finite_powers:
                        low, high, std, delta = band_stats(finite_powers)
                        mean = sum(finite_powers) / len(finite_powers)
                    else:
                        low = high = std = delta = mean = float("nan")

                if math.isfinite(mean):
                    label = (
                        f"{serial}\n"
                        f"Mean: {mean:.3f} W\n"
                        f"Power min/max: {low:.3f} W, {high:.3f} W\n"
                        f"Std: {std * 1000.0:.3f} mW\n"
                        f"ΔP: {delta * 1000.0:.3f} mW"
                    )
                else:
                    label = f"{serial}\nNo finite power values"
                displayed_time = [value * seconds_to_display for value in series["time"]]
                self.live_axes.plot(displayed_time, list(series["power"]), linewidth=1.2, label=label)
                any_data = True
        if any_data:
            self.live_axes.legend(loc="upper left", bbox_to_anchor=(1.02, 1.0), borderaxespad=0.0, fontsize=8)
        self.live_canvas.draw_idle()
        self.live_dirty = False

    def _process_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "resources":
                    self._set_resources(payload)
                elif kind == "log":
                    self._append_log(str(payload))
                elif kind == "status_line":
                    self.status.set(str(payload))
                    self.live_readout.set(f"Latest measurement: {payload}")
                elif kind == "live":
                    serial, elapsed, power = payload
                    if self.long_run_live_mode:
                        series = self.live_data.setdefault(
                            "%s" % serial,
                            {"time": deque(), "power": deque(), "stats": RunningPowerStats()},
                        )
                    else:
                        series = self.live_data.setdefault("%s" % serial, {"time": [], "power": []})
                    series["time"].append(elapsed)
                    series["power"].append(power)
                    if self.long_run_live_mode:
                        series["stats"].add(power)
                        cutoff = elapsed - LONG_RUN_LIVE_WINDOW_S
                        while series["time"] and series["time"][0] < cutoff:
                            series["time"].popleft()
                            series["power"].popleft()
                    self.live_dirty = True
                elif kind == "error":
                    self._append_log(str(payload))
                    self.status.set(str(payload))
                    self.refresh_button.configure(state=tk.NORMAL)
                    messagebox.showerror("PM1xx Logger", str(payload))
                elif kind == "finished":
                    self.running = False
                    self.start_button.configure(state=tk.NORMAL)
                    self.stop_button.configure(state=tk.DISABLED)
                    self.refresh_button.configure(state=tk.NORMAL)
                    if not self.stop_event.is_set():
                        self.status.set("Acquisition complete.")
        except queue.Empty:
            pass
        should_render = (
            self.live_dirty
            and (
                not self.long_run_live_mode
                or time.monotonic() - self.last_live_render_time >= LONG_RUN_RENDER_PERIOD_S
            )
        )
        if should_render:
            self._render_live_plot()
            self.last_live_render_time = time.monotonic()
        self.after(80, self._process_events)

    def _update_plot_file_state(self) -> None:
        state = tk.DISABLED if self.plot_all_folder.get() else tk.NORMAL
        self.plot_file_entry.configure(state=state)
        self.plot_file_browse.configure(state=state)

    def _browse_plot_folder(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.plot_folder.get() or str(Path.cwd()))
        if folder:
            self.plot_folder.set(folder)

    def _browse_save_folder(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.save_folder.get() or str(Path.cwd()))
        if folder:
            self.save_folder.set(folder)

    def _browse_plot_file(self) -> None:
        path = filedialog.askopenfilename(
            initialdir=self.plot_folder.get() or str(Path.cwd()), filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if path:
            self.plot_file.set(path)

    def _plot_saved_csv(self) -> None:
        try:
            folder = Path(self.plot_folder.get().strip() or "Power_Test_CSVs").expanduser().resolve()
            if self.plot_all_folder.get():
                csv_paths = sorted(folder.glob("*.csv"))
                if not csv_paths:
                    raise FileNotFoundError(f"No CSV files found in folder: {folder}")
            else:
                text = self.plot_file.get().strip()
                if not text:
                    self._append_log("Saved CSV plot skipped: no file chosen and All CSV files is not selected.")
                    return
                one_path = Path(text).expanduser()
                if not one_path.is_absolute():
                    one_path = folder / one_path
                csv_paths = [one_path.resolve()]
                if not csv_paths[0].exists():
                    raise FileNotFoundError(f"CSV not found: {csv_paths[0]}")

            band_start = parse_optional_minutes(self.plot_band_start.get(), self.plot_band_start_unit.get(), "Band start")
            band_end = parse_optional_minutes(self.plot_band_end.get(), self.plot_band_end_unit.get(), "Band end")
            if band_start is None and band_end is not None:
                band_start = 0.0
            if band_start is not None and band_end is not None and band_end <= band_start:
                raise ValueError("Band end must be greater than band start.")
            y_min = parse_optional_float(self.plot_y_min.get(), "Y min")
            y_max = parse_optional_float(self.plot_y_max.get(), "Y max")
            if y_min is not None and y_max is not None and y_max <= y_min:
                raise ValueError("Y max must be greater than Y min.")
        except Exception as exc:
            messagebox.showerror("Saved CSV plot", str(exc))
            return

        try:
            figure, output_path = self._build_saved_csv_figure(
                csv_paths,
                folder,
                band_start,
                band_end,
                y_min,
                y_max,
                self.plot_band_mode.get(),
            )
            figure.savefig(output_path, dpi=250)
            self._show_plot_window(figure, output_path)
            self._append_log(f"Saved plot: {output_path}")
        except Exception as exc:
            messagebox.showerror("Saved CSV plot", str(exc))

    def _build_saved_csv_figure(
        self,
        csv_paths: list[Path],
        folder: Path,
        band_start: float | None,
        band_end: float | None,
        y_min: float | None,
        y_max: float | None,
        band_mode: str,
    ) -> tuple[Figure, Path]:
        series: list[PowerSeries] = []
        for path in csv_paths:
            for loaded in load_power_csv_series(path):
                series.append(PowerSeries(
                    loaded.label,
                    [value * time_to_minutes_factor(loaded.time_unit) for value in loaded.times],
                    loaded.powers,
                    loaded.time_unit,
                    loaded.power_unit,
                ))

        figure = Figure(figsize=(13.2, 7.2), dpi=100)
        axes = figure.add_subplot(111)
        first_power_unit = series[0].power_unit or "W"
        to_mw = value_to_mw_factor(first_power_unit)
        maximum_time = max(max(item.times) for item in series if item.times)
        effective_band_end = band_end if band_end is not None else maximum_time

        if band_start is not None and effective_band_end <= band_start:
            raise ValueError("Band range contains no positive time span.")

        global_values: list[float] = []
        for item in series:
            line, = axes.plot(item.times, item.powers, linewidth=1.2, label=item.label)
            if band_start is None:
                continue
            selected = [
                value for current_time, value in zip(item.times, item.powers)
                if band_start <= current_time <= effective_band_end
            ]
            if not selected:
                self._append_log(f"Plot note: no band data for {item.label}; trace plotted without band statistics.")
                continue
            if band_mode == "global":
                global_values.extend(selected)
                continue
            low, high, std, delta = band_stats(selected)
            mean = sum(selected) / len(selected)
            color = line.get_color()
            axes.fill_between(
                [band_start, maximum_time],
                [low, low],
                [high, high],
                color=color,
                alpha=0.10,
                label=(
                    f"Mean: {mean:.3f} {first_power_unit}\n"
                    f"Power min/max: {low:.3f} {first_power_unit}, {high:.3f} {first_power_unit}\n"
                    f"Std: {std * to_mw:.3f} mW\nΔP: {delta * to_mw:.3f} mW"
                ),
            )
            axes.hlines([low, high], band_start, maximum_time, colors=color, linewidth=1.0, linestyles="--")

        if band_start is not None and band_mode == "global" and global_values:
            low, high, std, delta = band_stats(global_values)
            mean = sum(global_values) / len(global_values)
            axes.fill_between(
                [band_start, maximum_time],
                [low, low],
                [high, high],
                color="orange",
                alpha=0.20,
                label=(
                    f"Global band\nMean: {mean:.3f} {first_power_unit}\n"
                    f"Power min/max: {low:.3f} {first_power_unit}, {high:.3f} {first_power_unit}\n"
                    f"Std: {std * to_mw:.3f} mW\nΔP: {delta * to_mw:.3f} mW"
                ),
            )
            axes.hlines([low, high], band_start, maximum_time, colors="orange", linewidth=1.2, linestyles="--")

        axes.set_title("Laser Driver Plot")
        axes.set_xlabel("Time (min)")
        axes.set_ylabel(f"Power ({first_power_unit})")
        axes.grid(True, alpha=0.25)
        if y_min is not None or y_max is not None:
            axes.set_ylim(bottom=y_min, top=y_max)
        axes.tick_params(labelsize=10)
        axes.legend(
            loc="upper left",
            bbox_to_anchor=(1.015, 1.0),
            borderaxespad=0.0,
            fontsize=9,
            labelspacing=0.45,
            framealpha=0.95,
        )
        # Outer canvas margins only; this does not change x/y axis limits.
        figure.subplots_adjust(left=0.075, right=0.71, bottom=0.09, top=0.93)

        output_path = folder / f"{folder.name}_overlay_plot.png" if len(csv_paths) > 1 else csv_paths[0].with_name(f"{csv_paths[0].stem}_plot.png")
        return figure, output_path

    def _show_plot_window(self, figure: Figure, output_path: Path) -> None:
        window = tk.Toplevel(self)
        window.title(f"Saved CSV Plot - {output_path.name}")
        window.geometry("1440x820")
        canvas = FigureCanvasTkAgg(figure, master=window)
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        canvas.draw_idle()


if __name__ == "__main__":
    PM1xxTemperatureGUI().mainloop()
