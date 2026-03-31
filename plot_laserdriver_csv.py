from __future__ import annotations

import argparse
import re
from pathlib import Path

import matplotlib.pyplot as plt

# Plot y-axis defaults. Edit these values directly for a fixed y-range.
# Keep as None for automatic y scaling.
DEFAULT_Y_MIN: float | None = None
DEFAULT_Y_MAX: float | None = None

# Optional filename-to-legend label mapping. Edit this in code.

FILE_LABELS: list[tuple[str, str]] = [
    ("3.0A_1550nm_1h_t2_20260313_144350.csv", "1550nm laser, 3A, 1hour, F444"),
    ("3.0A_1470nm_1h_t2_20260313_162207.csv", "1470nm laser, 3A, 1hour, F443"),
]

# FILE_LABELS: list[tuple[str, str]] = []


def _extract_unit(header_text: str) -> str | None:
    match = re.search(r"\[(.*?)\]", header_text)
    return match.group(1).strip() if match else None


def load_laserdriver_csv(path: Path) -> tuple[list[float], list[float], str | None, str | None]:
    values: list[float] = []
    times: list[float] = []
    value_unit: str | None = None
    time_unit: str | None = None

    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue

            if "value unit" in text.lower() and "time unit" in text.lower():
                parts = re.split(r"\t+", text)
                if parts:
                    value_unit = _extract_unit(parts[0])
                if len(parts) > 1:
                    time_unit = _extract_unit(parts[1])
                continue

            fields = re.split(r"[\t,; ]+", text)
            if len(fields) < 2:
                continue

            try:
                value = float(fields[0])
                time_value = float(fields[1])
            except ValueError:
                continue

            values.append(value)
            times.append(time_value)

    if not values:
        raise ValueError(f"No numeric data points found in {path.name}")

    return times, values, time_unit, value_unit


def _time_to_minutes_factor(unit: str | None) -> tuple[float, str]:
    if unit is None:
        # Your file currently uses ms, so keep that as safe default.
        return 1.0 / 60000.0, "ms (assumed)"

    normalized = unit.strip().lower()
    mapping: dict[str, float] = {
        "ns": 1.0 / (60.0 * 1e9),
        "us": 1.0 / (60.0 * 1e6),
        "ms": 1.0 / 60000.0,
        "s": 1.0 / 60.0,
        "sec": 1.0 / 60.0,
        "min": 1.0,
        "h": 60.0,
        "hr": 60.0,
    }
    if normalized in mapping:
        return mapping[normalized], normalized

    # Fallback for unknown unit text.
    return 1.0, f"{unit} (treated as minutes)"


def _value_to_mw_factor(unit: str | None) -> tuple[float, str]:
    if unit is None:
        # Your file currently uses W, so keep that as safe default.
        return 1000.0, "W (assumed)"

    normalized = unit.strip().lower()
    mapping: dict[str, float] = {
        "w": 1000.0,
        "mw": 1.0,
        "uw": 1.0 / 1000.0,
        "nw": 1.0 / 1_000_000.0,
    }
    if normalized in mapping:
        return mapping[normalized], normalized

    # Fallback for unknown unit text.
    return 1.0, f"{unit} (treated as mW)"


def _parse_band_time_to_minutes(text: str) -> float:
    raw = text.strip().lower()
    if not raw:
        raise ValueError("Band time cannot be empty.")

    match = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*([a-z]+)?", raw)
    if not match:
        raise ValueError("Band time must look like 30s, 2m, 0.5h.")

    value = float(match.group(1))
    unit = (match.group(2) or "m").lower()
    scale_to_min: dict[str, float] = {
        "s": 1.0 / 60.0,
        "sec": 1.0 / 60.0,
        "secs": 1.0 / 60.0,
        "second": 1.0 / 60.0,
        "seconds": 1.0 / 60.0,
        "m": 1.0,
        "min": 1.0,
        "mins": 1.0,
        "minute": 1.0,
        "minutes": 1.0,
        "h": 60.0,
        "hr": 60.0,
        "hrs": 60.0,
        "hour": 60.0,
        "hours": 60.0,
    }
    if unit not in scale_to_min:
        raise ValueError("Unknown band time unit. Use s, m, or h.")
    return value * scale_to_min[unit]


def _resolve_csv_path(file_arg: Path, folder: Path) -> Path:
    if file_arg.is_absolute():
        csv_path = file_arg.resolve()
    else:
        # Prefer the path as entered from the current working directory.
        cwd_candidate = (Path.cwd() / file_arg).resolve()
        folder_candidate = (folder / file_arg).resolve()
        if cwd_candidate.exists():
            csv_path = cwd_candidate
        elif folder_candidate.exists():
            csv_path = folder_candidate
        else:
            raise FileNotFoundError(
                "CSV not found. Checked:\n"
                f"  {cwd_candidate}\n"
                f"  {folder_candidate}"
            )

    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")
    return csv_path


def _display_name_for_file(path: Path) -> str:
    filename = path.name.lower()
    for file_name, display_name in FILE_LABELS:
        if filename == file_name.lower():
            return display_name
    return path.stem


def _compute_band_stats(
    times_min: list[float], values: list[float], band_start: float, band_end: float
) -> tuple[list[float], float, float, float, float]:
    in_band_values = [v for t, v in zip(times_min, values) if band_start <= t <= band_end]
    if not in_band_values:
        raise ValueError(
            f"No data points in band range [{band_start:.3f}, {band_end:.3f}] minutes."
        )

    band_min = min(in_band_values)
    band_max = max(in_band_values)
    band_mean = sum(in_band_values) / len(in_band_values)
    band_std = (sum((v - band_mean) ** 2 for v in in_band_values) / len(in_band_values)) ** 0.5
    band_dp = band_max - band_min
    return in_band_values, band_min, band_max, band_std, band_dp


def _format_band_label(
    prefix: str, band_min: float, band_max: float, band_std_mw: float, band_dp_mw: float, y_unit: str
) -> str:
    return (
        # f"{prefix}\n"
        f"Power min/max: {band_min:.3f} {y_unit}, {band_max:.3f} {y_unit}\n"
        f"Std: {band_std_mw:.3f} mW\n"
        f"\u03B4P: {band_dp_mw:.3f} mW"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plot one CSV with optional band stats, or overlay all CSVs in a folder."
    )
    parser.add_argument(
        "--folder",
        type=Path,
        default=Path.cwd(),
        help="Fallback base folder for relative --file paths. Default: current working directory.",
    )
    parser.add_argument(
        "--file",
        type=Path,
        required=False,
        help="CSV file to plot (single-file mode).",
    )
    parser.add_argument(
        "--all-in-folder",
        "--all-folder",
        action="store_true",
        help="Plot and overlap all CSV files in --folder.",
    )
    parser.add_argument(
        "--all-folder-band-mode",
        choices=("global", "individual"),
        default="global",
        help="When using --all-in-folder, compute one global band or one band per CSV trace.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output PNG path. Default: <same folder>/<csv_stem>_plot.png",
    )
    parser.add_argument(
        "--band-start",
        type=str,
        default="1m",
        help="Band start time with unit, e.g. 30s, 1m, 0.5h (default: 1m).",
    )
    parser.add_argument(
        "--band-end",
        type=str,
        default=None,
        help="Band end time with unit, e.g. 90s, 5m, 1h (default: end of data).",
    )
    parser.add_argument(
        "--y-min",
        type=float,
        default=DEFAULT_Y_MIN,
        help="Y-axis minimum. Default comes from DEFAULT_Y_MIN in code.",
    )
    parser.add_argument(
        "--y-max",
        type=float,
        default=DEFAULT_Y_MAX,
        help="Y-axis maximum. Default comes from DEFAULT_Y_MAX in code.",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Display the plot window in addition to saving the PNG.",
    )
    args = parser.parse_args()

    folder = args.folder.resolve()
    if args.all_in_folder:
        csv_paths = sorted(folder.glob("*.csv"))
        if not csv_paths:
            raise FileNotFoundError(f"No CSV files found in folder: {folder}")
    else:
        if args.file is None:
            raise ValueError("Provide --file for single-file mode, or use --all-in-folder.")
        csv_paths = [_resolve_csv_path(args.file, folder)]

    fig, ax = plt.subplots(figsize=(11, 7))
    times_raw, values, time_unit, value_unit = load_laserdriver_csv(csv_paths[0])
    to_min_factor, unit_note = _time_to_minutes_factor(time_unit)
    to_mw_factor, value_unit_note = _value_to_mw_factor(value_unit)
    times_min = [t * to_min_factor for t in times_raw]

    y_unit = value_unit if value_unit else "arb."
    band_start = _parse_band_time_to_minutes(args.band_start)
    if args.all_in_folder:
        all_series: list[tuple[Path, list[float], list[float]]] = []
        global_max_time = float("-inf")
        for csv_path in csv_paths:
            t_raw, v_raw, t_unit, _ = load_laserdriver_csv(csv_path)
            t_factor, _ = _time_to_minutes_factor(t_unit)
            t_min = [t * t_factor for t in t_raw]
            all_series.append((csv_path, t_min, v_raw))
            if t_min:
                global_max_time = max(global_max_time, max(t_min))

        if global_max_time == float("-inf"):
            raise ValueError("No time data found in CSV files.")

        band_end = (
            _parse_band_time_to_minutes(args.band_end)
            if args.band_end is not None
            else global_max_time
        )
        band_draw_end = global_max_time if args.band_end is not None else band_end
        if band_end <= band_start:
            raise ValueError("--band-end must be greater than --band-start.")

        if args.all_folder_band_mode == "global":
            global_in_band_values: list[float] = []
            for csv_path, t_min, v_raw in all_series:
                display_name = _display_name_for_file(csv_path)
                ax.plot(t_min, v_raw, linewidth=1.2, label=display_name)
                global_in_band_values.extend(
                    v for t, v in zip(t_min, v_raw) if band_start <= t <= band_end
                )

            if not global_in_band_values:
                raise ValueError(
                    f"No data points in global band range [{band_start:.3f}, {band_end:.3f}] minutes. "
                    "Adjust --band-start / --band-end."
                )

            band_min = min(global_in_band_values)
            band_max = max(global_in_band_values)
            band_mean = sum(global_in_band_values) / len(global_in_band_values)
            band_std_mw = (
                sum((v - band_mean) ** 2 for v in global_in_band_values) / len(global_in_band_values)
            ) ** 0.5
            band_std_mw *= to_mw_factor
            band_dp_mw = (band_max - band_min) * to_mw_factor
            ax.fill_between(
                [band_start, band_draw_end],
                [band_min, band_min],
                [band_max, band_max],
                color="orange",
                alpha=0.2,
                label=_format_band_label(
                    "Global band", band_min, band_max, band_std_mw, band_dp_mw, y_unit
                ),
            )
            ax.hlines(
                [band_min, band_max],
                band_start,
                band_draw_end,
                colors="orange",
                linewidth=1.2,
                linestyles="--",
            )
        else:
            band_min = band_max = band_std_mw = band_dp_mw = 0.0
            for csv_path, t_min, v_raw in all_series:
                display_name = _display_name_for_file(csv_path)
                line, = ax.plot(t_min, v_raw, linewidth=1.2, label=display_name)
                _, local_min, local_max, local_std, local_dp = _compute_band_stats(
                    t_min, v_raw, band_start, band_end
                )
                local_std_mw = local_std * to_mw_factor
                local_dp_mw = local_dp * to_mw_factor
                color = line.get_color()
                ax.fill_between(
                    [band_start, band_draw_end],
                    [local_min, local_min],
                    [local_max, local_max],
                    color=color,
                    alpha=0.10,
                    label=_format_band_label(
                        f"{display_name} band",
                        local_min,
                        local_max,
                        local_std_mw,
                        local_dp_mw,
                        y_unit,
                    ),
                )
                ax.hlines(
                    [local_min, local_max],
                    band_start,
                    band_draw_end,
                    colors=color,
                    linewidth=1.0,
                    linestyles="--",
                )
        # ax.set_title(f"Laser Driver Plot (Overlay {len(csv_paths)} files)")
        ax.set_title(f"Laser Driver Plot")
    else:
        csv_path = csv_paths[0]
        display_name = _display_name_for_file(csv_path)
        ax.plot(times_min, values, linewidth=1.3, label=display_name, color="tab:blue")

        plot_end = max(times_min)
        band_end = _parse_band_time_to_minutes(args.band_end) if args.band_end is not None else plot_end
        band_draw_end = plot_end if args.band_end is not None else band_end
        if band_end <= band_start:
            raise ValueError("--band-end must be greater than --band-start.")

        _, band_min, band_max, band_std, band_dp = _compute_band_stats(
            times_min, values, band_start, band_end
        )
        band_std_mw = band_std * to_mw_factor
        band_dp_mw = band_dp * to_mw_factor
        ax.fill_between(
            [band_start, band_draw_end],
            [band_min, band_min],
            [band_max, band_max],
            color="orange",
            alpha=0.2,
            label=_format_band_label(display_name, band_min, band_max, band_std_mw, band_dp_mw, y_unit),
        )
        ax.hlines([band_min, band_max], band_start, band_draw_end, colors="orange", linewidth=1.2, linestyles="--")
        ax.set_title("Laser Driver Plot")

    ax.set_xlabel("Time (min)")
    ax.set_ylabel(f"Power ({y_unit})")
    if args.y_min is not None and args.y_max is not None and args.y_max <= args.y_min:
        raise ValueError("--y-max must be greater than --y-min.")
    if args.y_min is not None or args.y_max is not None:
        ax.set_ylim(bottom=args.y_min, top=args.y_max)
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best", fontsize=8)

    fig.tight_layout()

    if args.output:
        output_path = args.output.resolve()
    elif args.all_in_folder:
        output_path = folder / f"{folder.name}_overlay_plot.png"
    else:
        single_path = csv_paths[0]
        output_path = single_path.parent / f"{single_path.stem}_plot.png"
    fig.savefig(output_path, dpi=250)
    print(f"Saved plot: {output_path}")
    print(f"Time unit parsed: {unit_note}")
    print(f"Value unit parsed: {value_unit_note}")
    if args.all_in_folder:
        print(f"Overlay files: {len(csv_paths)}")
        print(f"Band mode: {args.all_folder_band_mode}")
    print(f"Band calculation range (min): {band_start:.3f} to {band_end:.3f}")
    if args.band_end is not None:
        print(f"Band drawn to plot end (min): {band_draw_end:.3f}")
    if not args.all_in_folder or args.all_folder_band_mode == "global":
        print(f"Band min/max ({y_unit}): {band_min:.3f} / {band_max:.3f}")
        print(f"Band std (mW): {band_std_mw:.3f}")
        print(f"Band \u03B4P (mW): {band_dp_mw:.3f}")
    else:
        print("Band stats shown individually in the legend for each CSV trace.")

    if args.show:
        plt.show()


if __name__ == "__main__":
    main()
