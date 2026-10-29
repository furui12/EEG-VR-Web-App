"""Write a trial to disk while it is being recorded, and turn it into an XDF file.

Each trial gets a folder data/.trials/trial-<id>/ with append-only files:
    meta.json        stream headers, formats, channel count
    eeg_ts.f64       EEG timestamps (float64)
    eeg_raw.f64      raw EEG (float64, n x C)
    eeg_filt.f32     filtered EEG (float32, n x C)
    markers.jsonl    VR markers  {"t": ts, "v": [values]}
    notes.jsonl      experimenter annotations {"t": ts, "v": ["label"]}
    metrics.f64      live metrics rows [ts, m1, m2, ...]
If the app or the computer crashes, the folder survives and the trial is recovered
into an XDF file at the next start.
"""
import json
import shutil
from pathlib import Path

import numpy as np

from app.xdf_writer import write_xdf

FILES = ("eeg_ts.f64", "eeg_raw.f64", "eeg_filt.f32", "markers.jsonl", "notes.jsonl", "metrics.f64")


class TrialWriter:
    def __init__(self, folder: Path, meta: dict):
        self.folder = Path(folder)
        if self.folder.exists():
            shutil.rmtree(self.folder)
        self.folder.mkdir(parents=True)
        (self.folder / "meta.json").write_text(json.dumps(meta, indent=1))
        self.C = int(meta["channels"])
        self.n_metrics = len(meta.get("metric_names", []))
        self._f = {name: open(self.folder / name, "ab") for name in FILES}
        self.closed = False

    def write_eeg(self, ts, raw, filt):
        self._f["eeg_ts.f64"].write(np.asarray(ts, dtype="<f8").tobytes())
        self._f["eeg_raw.f64"].write(np.asarray(raw, dtype="<f8").reshape(-1, self.C).tobytes())
        self._f["eeg_filt.f32"].write(np.asarray(filt, dtype="<f4").reshape(-1, self.C).tobytes())

    def write_event(self, kind, ts, values):
        name = "markers.jsonl" if kind == "markers" else "notes.jsonl"
        line = json.dumps({"t": float(ts), "v": [str(v) for v in values]}) + "\n"
        self._f[name].write(line.encode("utf-8"))

    def write_metrics(self, ts, values):
        row = np.asarray([ts, *values], dtype="<f8")
        self._f["metrics.f64"].write(row.tobytes())

    def flush(self):
        for f in self._f.values():
            f.flush()

    def close(self):
        if not self.closed:
            for f in self._f.values():
                f.close()
            self.closed = True


def _read_events(path):
    ts, vals = [], []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue          # partially written last line after a crash
            ts.append(d["t"])
            vals.append(d["v"])
    return ts, vals


def load_trial(folder):
    folder = Path(folder)
    meta = json.loads((folder / "meta.json").read_text())
    C = int(meta["channels"])
    ts = np.fromfile(folder / "eeg_ts.f64", dtype="<f8")
    raw = np.fromfile(folder / "eeg_raw.f64", dtype="<f8")
    filt = np.fromfile(folder / "eeg_filt.f32", dtype="<f4")
    n = min(len(ts), len(raw) // C, len(filt) // C)      # tolerate a torn last write
    data = {
        "meta": meta,
        "ts": ts[:n],
        "raw": raw[: n * C].reshape(n, C),
        "filt": filt[: n * C].reshape(n, C),
    }
    data["marker_ts"], data["marker_vals"] = _read_events(folder / "markers.jsonl")
    data["note_ts"], data["note_vals"] = _read_events(folder / "notes.jsonl")
    m = np.fromfile(folder / "metrics.f64", dtype="<f8") if (folder / "metrics.f64").exists() else np.empty(0)
    k = 1 + len(meta.get("metric_names", []))
    m = m[: (len(m) // k) * k].reshape(-1, k) if k > 1 else np.empty((0, 1))
    data["metric_ts"] = m[:, 0] if len(m) else np.empty(0)
    data["metrics"] = m[:, 1:] if len(m) else np.empty((0, k - 1))
    return data


def sample_count(folder):
    p = Path(folder) / "eeg_ts.f64"
    return p.stat().st_size // 8 if p.exists() else 0


def export_trial(folder, export_type, out_path):
    """Write the trial stored in `folder` to an XDF file.
    export_type 'raw' -> raw EEG only; 'all' -> raw + filtered EEG + VR markers +
    experimenter annotations + live metrics."""
    d = load_trial(folder)
    meta = d["meta"]
    if len(d["ts"]) == 0:
        raise RuntimeError("No EEG samples were recorded in this trial.")
    streams = [{"header_xml": meta["eeg_header"], "channel_format": meta["eeg_format"],
                "timestamps": d["ts"], "samples": d["raw"]}]
    if export_type == "all":
        streams.append({"header_xml": meta["filt_header"], "channel_format": "float32",
                        "timestamps": d["ts"], "samples": d["filt"]})
        if meta.get("marker_header"):
            streams.append({"header_xml": meta["marker_header"], "channel_format": meta["marker_format"],
                            "timestamps": d["marker_ts"], "samples": d["marker_vals"]})
        if meta.get("notes_header"):
            streams.append({"header_xml": meta["notes_header"], "channel_format": "string",
                            "timestamps": d["note_ts"], "samples": d["note_vals"]})
        if meta.get("metrics_header") and len(d["metric_ts"]):
            streams.append({"header_xml": meta["metrics_header"], "channel_format": "float32",
                            "timestamps": d["metric_ts"], "samples": d["metrics"]})
    write_xdf(out_path, streams)
    return {
        "streams": len(streams),
        "samples": int(len(d["ts"])),
        "markers": len(d["marker_ts"]),
        "notes": len(d["note_ts"]),
        "duration_s": round(float(d["ts"][-1] - d["ts"][0]), 2) if len(d["ts"]) > 1 else 0.0,
    }


def remove_trial(folder):
    shutil.rmtree(folder, ignore_errors=True)


def _header_info(xml):
    """srate, channel labels and a few desc values from a stream header."""
    import xml.etree.ElementTree as ET
    out = {"srate": 250.0, "labels": [], "desc": {}}
    if not xml:
        return out
    try:
        root = ET.fromstring(xml.split("?>", 1)[1] if xml.startswith("<?xml") else xml)
    except ET.ParseError:
        return out
    try:
        out["srate"] = float(root.findtext("nominal_srate") or 250.0) or 250.0
    except ValueError:
        pass
    desc = root.find("desc")
    if desc is not None:
        out["labels"] = [ch.findtext("label") or "" for ch in desc.findall("./channels/channel")]
        for child in desc:
            if child.tag != "channels" and (child.text or "").strip():
                out["desc"][child.tag] = child.text.strip()
    return out


def folder_stats(folder, n_used=5, line_freq=60.0):
    """Quality / metric summary of a recorded trial (see analysis.trial_stats)."""
    from app.analysis import trial_stats
    d = load_trial(folder)
    meta = d["meta"]
    eeg = _header_info(meta.get("eeg_header"))
    met = _header_info(meta.get("metrics_header"))
    labels = eeg["labels"] or [f"Ch {i + 1}" for i in range(int(meta["channels"]))]
    stats = trial_stats(
        d["raw"], d["ts"], eeg["srate"], labels, n_used, line_freq,
        metrics=d["metrics"] if len(d["metric_ts"]) else None,
        note_labels=[v[0] for v in d["note_vals"] if v], n_markers=len(d["marker_ts"]))
    stats["frontal_label"] = met["desc"].get("frontal_channel")
    stats["parietal_label"] = met["desc"].get("parietal_channel")
    return stats
