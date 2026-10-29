"""Repair XDF files recorded from AuraLSL v1.1 before the app decoded its stream.

AuraLSL pushes its whole 9x8 ring buffer on every update, so those recordings contain
~9x too many, scrambled "samples" (see app/aura_decoder.py). This script rebuilds the
real 250 Hz EEG and writes <name>_repaired.xdf next to each input file:
    - the EEG stream, decoded
    - a newly filtered copy is NOT created (the old *_filtered stream was computed from
      scrambled data and is dropped)
    - every other stream (VR markers, annotations, ...) is copied unchanged

Usage (from the v0.x folder, inside the virtual environment):
    pip install pyxdf
    python tools/repair_aura_xdf.py data/sub-001_trial-01_*.xdf
Files that are not affected are reported and left alone.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.aura_decoder import decode_recording  # noqa: E402
from app.xdf_writer import write_xdf  # noqa: E402

try:
    import pyxdf
except ImportError:
    sys.exit("pyxdf is required: pip install pyxdf")


def _xml(tag, value):
    return f"<{tag}>{value}</{tag}>"


def info_to_xml(info, extra_desc=None, srate=None):
    """Rebuild a minimal <info> header from pyxdf's parsed info dict."""
    get = lambda k, d="": (info.get(k) or [d])[0]
    parts = [_xml("name", get("name")), _xml("type", get("type")),
             _xml("channel_count", get("channel_count")),
             _xml("nominal_srate", srate if srate is not None else get("nominal_srate")),
             _xml("channel_format", get("channel_format")), _xml("source_id", get("source_id"))]
    desc = ""
    d = (info.get("desc") or [None])[0]
    if d and "channels" in d:
        chans = d["channels"][0]["channel"]
        desc += "<channels>" + "".join(
            "<channel>" + _xml("label", (c.get("label") or [""])[0]) + "</channel>" for c in chans) + "</channels>"
    for k, v in (extra_desc or {}).items():
        desc += _xml(k, v)
    parts.append(f"<desc>{desc}</desc>")
    return "<info>" + "".join(parts) + "</info>"


def repair(path):
    streams, _ = pyxdf.load_xdf(str(path), synchronize_clocks=False, dejitter_timestamps=False)
    out, fixed = [], False
    for s in streams:
        info = s["info"]
        fmt = info["channel_format"][0]
        name = info["name"][0]
        if fmt != "string" and info["type"][0].upper() == "EEG" and not name.endswith("_filtered"):
            C = int(info["channel_count"][0])
            srate = float(info["nominal_srate"][0])
            rows = np.asarray(s["time_series"], dtype=np.float32)
            samples, ts, dec = decode_recording(rows, s["time_stamps"], C, srate)
            if dec.mode == "ring":
                fixed = True
                print(f"  {name}: {len(rows)} rows -> {len(ts)} samples "
                      f"({len(ts) / max(ts[-1] - ts[0], 1e-9):.1f} Hz)")
                out.append({"header_xml": info_to_xml(info, {"decoding": dec.description,
                                                             "repaired_from": Path(path).name}),
                            "channel_format": fmt, "timestamps": ts, "samples": samples})
                continue
        if name.endswith("_filtered"):
            continue
        out.append({"header_xml": info_to_xml(info), "channel_format": fmt,
                    "timestamps": s["time_stamps"],
                    "samples": s["time_series"] if fmt == "string" else np.asarray(s["time_series"])})
    if not fixed:
        print("  not an AuraLSL ring-buffer recording, nothing to do")
        return None
    target = Path(path).with_name(Path(path).stem + "_repaired.xdf")
    write_xdf(str(target), out)
    return target


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for p in sys.argv[1:]:
        print(p)
        t = repair(p)
        if t:
            print(f"  written {t}")
