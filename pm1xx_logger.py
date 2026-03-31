#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import ExitStack
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pyvisa


@dataclass(frozen=True)
class MeterInfo:
    resource: str
    model: str
    serial: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Log one or more Thorlabs PM1xx/PM400 meters via pyvisa."
    )
    parser.add_argument(
        "--duration",
        type=str,
        default="60s",
        help="Acquisition window with unit, e.g. 10s, 10m, 1h.",
    )
    parser.add_argument(
        "--sample-rate-hz",
        type=float,
        default=5.0,
        help="Software sampling rate in Hz.",
    )
    parser.add_argument(
        "--wavelength-nm",
        type=float,
        default=808.0,
        help="Wavelength to set on all selected meters (nm).",
    )
    parser.add_argument(
        "--bandwidth",
        type=str,
        choices=["low", "high"],
        default="low",
        help="Power measurement bandwidth mode.",
    )
    parser.add_argument(
        "--serials",
        type=str,
        default="",
        help="Comma-separated serials to use (empty means all discovered).",
    )
    parser.add_argument(
        "--out-csv",
        type=Path,
        default=None,
        help="CSV output path (overrides auto name).",
    )
    parser.add_argument(
        "--name",
        type=str,
        default="pm1xx",
        help="Base name for auto-generated files: <name>_<timestamp>.*",
    )
    parser.add_argument(
        "--visa-backend",
        type=str,
        default="@py",
        help="pyvisa backend (default: @py).",
    )
    parser.add_argument(
        "--list-resources",
        action="store_true",
        help="Only list VISA resources and exit.",
    )
    parser.add_argument(
        "--resource-filter",
        type=str,
        default="",
        help="Optional regex to filter VISA resources before probing.",
    )
    parser.add_argument(
        "--probe-all-resources",
        action="store_true",
        help="Probe all VISA resources (including ASRL), not only USB-like resources.",
    )
    parser.add_argument(
        "--resource",
        action="append",
        default=[],
        help="Specific VISA resource to probe (can be passed multiple times).",
    )
    return parser.parse_args()


def parse_duration_to_seconds(duration_text: str) -> float:
    text = duration_text.strip().lower()
    if not text:
        raise ValueError("--duration cannot be empty.")

    match = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*([a-z]+)?", text)
    if not match:
        raise ValueError(
            "--duration must look like 10s, 10m, 1h (number + optional unit)."
        )

    value = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    scale = {
        "s": 1.0,
        "sec": 1.0,
        "secs": 1.0,
        "second": 1.0,
        "seconds": 1.0,
        "m": 60.0,
        "min": 60.0,
        "mins": 60.0,
        "minute": 60.0,
        "minutes": 60.0,
        "h": 3600.0,
        "hr": 3600.0,
        "hrs": 3600.0,
        "hour": 3600.0,
        "hours": 3600.0,
    }
    if unit not in scale:
        raise ValueError(
            "--duration unit not recognized. Use s (seconds), m (minutes), or h (hours)."
        )
    return value * scale[unit]


def infer_serial_from_resource(resource: str) -> str:
    # Typical USBTMC resource:
    # USB0::0x1313::0x8078::P1234567::INSTR
    match = re.search(r"::([A-Za-z0-9_-]+)::INSTR$", resource, re.IGNORECASE)
    if match:
        return match.group(1)
    return resource


def discover_meters(
    rm: pyvisa.ResourceManager,
    resource_filter: str = "",
    probe_all_resources: bool = False,
    resource_hints: List[str] | None = None,
) -> Tuple[List[MeterInfo], List[str]]:
    meters: List[MeterInfo] = []
    debug_lines: List[str] = []
    resources_raw = list(rm.list_resources())
    resources: List[str] = []
    seen: set[str] = set()
    resource_hints = resource_hints or []
    ordered_inputs = resource_hints + resources_raw
    for resource in ordered_inputs:
        if resource not in seen:
            seen.add(resource)
            resources.append(resource)

    patt = re.compile(resource_filter, re.IGNORECASE) if resource_filter else None

    for resource in resources:
        up_resource = resource.upper()
        if patt and not patt.search(resource):
            debug_lines.append(f"skip(filter): {resource}")
            continue

        # Fast path: probe USB-like resources first unless user asked for full probing.
        is_usb_like = (
            "USB" in up_resource
            or "0X1313" in up_resource
            or "INSTR" in up_resource and "ASRL" not in up_resource
        )
        if not probe_all_resources and not is_usb_like:
            debug_lines.append(f"skip(bus): {resource}")
            continue

        inst = None
        try:
            inst = rm.open_resource(resource, timeout=3000, chunk_size=1024)
            inst.write_termination = "\n"
            inst.read_termination = "\n"
            idn = ""
            try:
                idn = inst.query("*IDN?").strip()
            except Exception:
                idn = ""
            idn_up = idn.upper()
            if "THORLABS" in idn_up and ("PM100" in idn_up or "PM400" in idn_up):
                parts = [p.strip() for p in idn.split(",")]
                model = parts[1] if len(parts) > 1 else "UNKNOWN"
                serial = parts[2] if len(parts) > 2 else resource
                meters.append(MeterInfo(resource=resource, model=model, serial=serial))
                debug_lines.append(f"ok(idn): {resource} -> {idn}")
            elif ("PM100" in idn_up or "PM400" in idn_up) and idn:
                # Some stacks omit vendor in IDN.
                parts = [p.strip() for p in idn.split(",")]
                model = parts[1] if len(parts) > 1 else "PM-UNKNOWN"
                serial = parts[2] if len(parts) > 2 else infer_serial_from_resource(resource)
                meters.append(MeterInfo(resource=resource, model=model, serial=serial))
                debug_lines.append(f"ok(idn-generic): {resource} -> {idn}")
            elif "0X1313" in up_resource:
                # Fallback for devices that do not respond to *IDN? via this stack.
                serial = infer_serial_from_resource(resource)
                meters.append(MeterInfo(resource=resource, model="PM-UNKNOWN", serial=serial))
                debug_lines.append(f"ok(vidpid-fallback): {resource}")
            else:
                debug_lines.append(f"skip(not-thorlabs): {resource} -> {idn or 'no-idn'}")
        except Exception:
            debug_lines.append(f"skip(open-fail): {resource}")
        finally:
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass
    return meters, debug_lines


def set_bandwidth(inst, mode: str) -> str:
    # PM-family SCPI differs slightly across models/firmware, so try common forms.
    target = "LOW" if mode.lower() == "low" else "HIGH"
    attempts = [
        f"SENSE:POW:DC:BANDWIDTH {target}",
        f"SENSE:POW:DC:BANDWIDTH {target[0]}",
        f"SENS:POW:DC:BAND {target}",
    ]
    for cmd in attempts:
        try:
            inst.write(cmd)
            return target
        except Exception:
            continue
    return "UNSET"


def query_float(inst, cmd: str) -> float:
    try:
        return float(inst.query(cmd).strip())
    except Exception:
        return float("nan")


def query_text(inst, cmd: str) -> str:
    try:
        return inst.query(cmd).strip()
    except Exception:
        return ""


def query_sensor_id(inst) -> str:
    # Different PM models/firmware may expose sensor ID via different queries.
    for cmd in ("SYST:SENS:IDN?", "SYST:SENS:TYPE?", "SENS:IDN?"):
        try:
            value = inst.query(cmd).strip()
            if value:
                return value
        except Exception:
            continue
    return "UNKNOWN"


def query_range_mode(inst) -> str:
    for cmd in ("SENS:POW:RANG:AUTO?", "SENSE:POW:RANGE:AUTO?"):
        auto = query_text(inst, cmd).upper()
        if auto:
            if auto in {"1", "ON"}:
                return "AUTO"
            if auto in {"0", "OFF"}:
                manual_range_w = query_float(inst, "SENS:POW:RANG?")
                if np.isfinite(manual_range_w):
                    return f"MANUAL {manual_range_w:.6g}W"
                return "MANUAL"
    return "UNKNOWN"


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def make_default_csv_path(base_name: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = base_name.strip() or "pm1xx"
    csv_path = Path("Power_Test_CSVs") / f"{safe_name}_{ts}.csv"
    return csv_path


def open_selected_meters(
    rm: pyvisa.ResourceManager, meters: List[MeterInfo]
) -> List[Tuple[object, MeterInfo]]:
    opened: List[Tuple[object, MeterInfo]] = []
    for meter in meters:
        inst = rm.open_resource(meter.resource, timeout=5000, chunk_size=4096)
        inst.write_termination = "\n"
        inst.read_termination = "\n"
        opened.append((inst, meter))
    return opened


def main() -> None:
    args = parse_args()
    duration_sec = parse_duration_to_seconds(args.duration)
    if duration_sec <= 0:
        raise ValueError("--duration must be > 0.")
    if args.sample_rate_hz <= 0:
        raise ValueError("--sample-rate-hz must be > 0.")

    sample_period_sec = 1.0 / args.sample_rate_hz
    requested_serials = {
        s.strip() for s in args.serials.split(",") if s.strip()
    }

    default_csv = make_default_csv_path(args.name)
    csv_path = args.out_csv or default_csv
    ensure_parent(csv_path)

    rm = pyvisa.ResourceManager(args.visa_backend)
    opened: List[Tuple[object, MeterInfo]] = []

    try:
        all_resources = list(rm.list_resources())
        if args.list_resources:
            print(f"VISA backend: {args.visa_backend}")
            if not all_resources:
                print("No VISA resources found.")
            for res in all_resources:
                print(f"  {res}")
            return

        discovered, debug_lines = discover_meters(
            rm,
            resource_filter=args.resource_filter,
            probe_all_resources=args.probe_all_resources,
            resource_hints=args.resource,
        )
        if not discovered and args.resource_filter:
            debug_lines.append("retry(no-filter): resource filter removed")
            discovered, retry_log = discover_meters(
                rm,
                resource_filter="",
                probe_all_resources=args.probe_all_resources,
                resource_hints=args.resource,
            )
            debug_lines.extend(retry_log)
        if not discovered and not args.probe_all_resources:
            debug_lines.append("retry(probe-all): enabling all-resource probing")
            discovered, retry_log = discover_meters(
                rm,
                resource_filter="",
                probe_all_resources=True,
                resource_hints=args.resource,
            )
            debug_lines.extend(retry_log)

        if not discovered:
            msg = [
                "No Thorlabs PM1xx/PM400 devices were discovered.",
                f"VISA backend: {args.visa_backend}",
                f"Total VISA resources seen: {len(all_resources)}",
            ]
            if all_resources:
                msg.append("Resources:")
                msg.extend([f"  {r}" for r in all_resources])
            if debug_lines:
                msg.append("Probe log:")
                msg.extend([f"  {line}" for line in debug_lines[:50]])
            msg.append("Tip: run with --list-resources to inspect what pyvisa can see.")
            msg.append("Tip: do not set --resource-filter unless needed; it can hide devices.")
            msg.append("Tip: use --probe-all-resources for ASRL-only environments.")
            msg.append("Tip: you can force a resource with --resource ASRLx::INSTR.")
            raise RuntimeError("\n".join(msg))

        if requested_serials:
            selected = [m for m in discovered if m.serial in requested_serials]
            missing = sorted(requested_serials - {m.serial for m in selected})
            if missing:
                print(f"Warning: requested serials not found: {', '.join(missing)}")
            if not selected:
                raise RuntimeError("None of the requested serials were discovered.")
        else:
            selected = discovered

        print("Selected meters:")
        for meter in selected:
            print(f"  {meter.model} {meter.serial} @ {meter.resource}")

        opened = open_selected_meters(rm, selected)
        print("Attached sensors:")
        sensor_ids: Dict[str, str] = {}
        for inst, meter in opened:
            sensor_id = query_sensor_id(inst)
            sensor_ids[meter.serial] = sensor_id
            print(f"  meter {meter.serial}: {sensor_id}")

        configured: Dict[str, Dict[str, float | str]] = {}
        for inst, meter in opened:
            try:
                inst.write(f"SENSE:CORR:WAV {args.wavelength_nm}")
            except Exception as e:
                print(f"Warning: failed to set wavelength for {meter.serial}: {e}")

            bw_set = set_bandwidth(inst, args.bandwidth)
            wl_report_nm = query_float(inst, "SENSE:CORR:WAV?")
            configured[meter.serial] = {
                "model": meter.model,
                "sensor_id": sensor_ids.get(meter.serial, "UNKNOWN"),
                "bandwidth": bw_set,
                "wavelength_nm": wl_report_nm,
                "range_mode": query_range_mode(inst),
            }

        t0 = time.perf_counter()
        csv_paths: Dict[str, Path] = {}
        base_suffix = csv_path.suffix if csv_path.suffix else ".csv"
        for _, meter in opened:
            out_path = csv_path
            if len(opened) > 1:
                out_path = csv_path.with_name(f"{csv_path.stem}_{meter.serial}{base_suffix}")
            ensure_parent(out_path)
            csv_paths[meter.serial] = out_path

        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        interrupted = False
        with ExitStack() as stack:
            handles: Dict[str, object] = {}
            for _, meter in opened:
                handle = stack.enter_context(csv_paths[meter.serial].open("w", encoding="utf-8", newline=""))
                handles[meter.serial] = handle
                info = configured[meter.serial]
                wl = info["wavelength_nm"]
                wl_text = f"{float(wl):.3f}nm" if np.isfinite(float(wl)) else "UNKNOWN"
                handle.write(f"{info['sensor_id']}\t{started_at}\n")
                handle.write("value unit [W]\ttime unit [ms]\n")
                handle.write(f"wavelength\t{wl_text}\n")
                handle.write(f"range\t{info['range_mode']}\n")
                handle.write(f"meter\t{info['model']} {meter.serial}\n")
                handle.write(f"bandwidth\t{info['bandwidth']}\n")
                handle.write(f"sample_rate\t{args.sample_rate_hz:.6f}Hz\n")
                handle.write(f"duration\t{args.duration}\n")

            print(
                f"Logging {len(opened)} meter(s) for {duration_sec:.2f} s "
                f"at {args.sample_rate_hz:.3f} Hz..."
            )

            try:
                while True:
                    loop_start = time.perf_counter()
                    elapsed = loop_start - t0
                    if elapsed >= duration_sec:
                        break

                    elapsed_ms = elapsed * 1000.0
                    row_values = [elapsed]
                    for inst, meter in opened:
                        pw = query_float(inst, "MEAS:POW?")
                        row_values.append(pw)
                        handles[meter.serial].write(f"{pw:.6E}\t{elapsed_ms:.0f}\n")

                    print(
                        "  ".join(
                            f"{val:.3e}" if i else f"{val:6.2f}s"
                            for i, val in enumerate(row_values)
                        ),
                        end="\r",
                        flush=True,
                    )
                    spent = time.perf_counter() - loop_start
                    if spent < sample_period_sec:
                        time.sleep(sample_period_sec - spent)
            except KeyboardInterrupt:
                interrupted = True
                print("\nCtrl+C received. Stopping acquisition safely...")
            print()

        print("Saved PM log files:")
        for serial, out_path in csv_paths.items():
            print(f"  {serial}: {out_path.resolve()}")
        if interrupted:
            print("Run interrupted by user. Partial data was saved.")

    except KeyboardInterrupt:
        print("\nCtrl+C received. Exiting safely...")

    finally:
        for inst, _ in opened:
            try:
                inst.close()
            except Exception:
                pass
        try:
            rm.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
