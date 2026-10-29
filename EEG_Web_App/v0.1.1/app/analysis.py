"""Signal-quality checks and live EEG metrics computed on short windows of raw data."""
import numpy as np
from scipy.signal import butter, sosfiltfilt, welch

# Quality thresholds (amplitudes in the device units, normally microvolts)
FLAT_STD = 0.5          # below this the channel is considered flat / disconnected
WARN_AMP = 50.0         # std of the 1-40 Hz signal
BAD_AMP = 100.0
WARN_LINE = 0.50        # fraction of 1-100 Hz power at the power-line frequency (a notch can remove it)
BAD_LINE = 0.85

BANDS = {"delta": (1.0, 4.0), "theta": (4.0, 8.0), "alpha": (8.0, 13.0), "beta": (13.0, 30.0)}


def _bandpass(x, srate, lo=1.0, hi=40.0):
    hi = min(hi, 0.45 * srate)
    sos = butter(4, [lo, hi], btype="band", fs=srate, output="sos")
    return sosfiltfilt(sos, x, axis=0)


def channel_quality(raw, srate, line_freq=60.0):
    """raw: (n, C) window (~2 s). Returns one dict per channel:
    level: good | warn | bad | flat, amp (std 1-40 Hz), line (fraction of power at line_freq)."""
    raw = np.asarray(raw, dtype=np.float64)
    n, C = raw.shape
    out = []
    if n < srate:          # need at least 1 s
        return [{"level": "unknown", "amp": None, "line": None} for _ in range(C)]
    centered = raw - raw.mean(axis=0)
    bp = _bandpass(centered, srate)
    amp = bp.std(axis=0)
    nper = int(min(n, srate))
    f, P = welch(centered, fs=srate, nperseg=nper, axis=0)
    band = (f >= 1) & (f <= min(100, 0.5 * srate))
    total = P[band].sum(axis=0) + 1e-12
    if line_freq and line_freq < 0.5 * srate - 2:
        line = P[(f >= line_freq - 2) & (f <= line_freq + 2)].sum(axis=0) / total
    else:
        line = np.zeros(C)
    flat = centered.std(axis=0) < FLAT_STD
    for c in range(C):
        if flat[c]:
            level = "flat"
        elif amp[c] > BAD_AMP or line[c] > BAD_LINE:
            level = "bad"
        elif amp[c] > WARN_AMP or line[c] > WARN_LINE:
            level = "warn"
        else:
            level = "good"
        out.append({"level": level, "amp": round(float(amp[c]), 1), "line": round(float(line[c]), 2)})
    return out


def band_powers(x, srate):
    """Absolute band powers of one channel (window of a few seconds)."""
    x = np.asarray(x, dtype=np.float64)
    x = x - x.mean()
    nper = int(min(len(x), 2 * srate))
    f, P = welch(x, fs=srate, nperseg=nper)
    df = f[1] - f[0] if len(f) > 1 else 1.0
    return {name: float(P[(f >= lo) & (f < hi)].sum() * df) for name, (lo, hi) in BANDS.items()}


def live_metrics(raw, srate, frontal, parietal):
    """Theta/alpha ratio (cognitive-load index) at a frontal and a parietal channel,
    plus relative band powers for display. raw: (n, C) window of ~4 s."""
    raw = np.asarray(raw, dtype=np.float64)
    if raw.shape[0] < 2 * srate:
        return None
    res = {}
    for key, ch in (("frontal", frontal), ("parietal", parietal)):
        if ch is None or ch >= raw.shape[1]:
            continue
        bp = band_powers(raw[:, ch], srate)
        total = sum(bp.values()) or 1e-12
        res[key] = {
            "channel": int(ch),
            "theta_alpha": round(bp["theta"] / (bp["alpha"] + 1e-12), 3),
            "relative": {k: round(v / total, 3) for k, v in bp.items()},
            "theta": bp["theta"],
            "alpha": bp["alpha"],
        }
    return res


# --------------------------------------------------------------------------- per-trial summary
WINDOW_S = 2.0      # quality is judged on 2-s windows
SEGMENT_S = 30.0    # and summarised on 30-s segments (report heat map)


def trial_stats(raw, ts, srate, labels, n_used, line_freq=60.0, metrics=None,
                frontal=0, parietal=1, note_labels=None, n_markers=0):
    """Summary stored with each trial: signal quality of the first `n_used` channels,
    theta/alpha (cognitive-load index) and data integrity."""
    raw = np.asarray(raw, dtype=np.float64)
    ts = np.asarray(ts, dtype=np.float64)
    n = raw.shape[0]
    C = min(int(n_used or raw.shape[1]), raw.shape[1])
    labels = list(labels or [])[:C] or [f"Ch {i + 1}" for i in range(C)]
    win = int(WINDOW_S * srate)
    levels = []                                   # per window: list of C levels
    for start in range(0, max(n - win + 1, 0), win):
        q = channel_quality(raw[start:start + win, :C], srate, line_freq)
        levels.append([c["level"] for c in q])
    per_seg = max(1, int(round(SEGMENT_S / WINDOW_S)))
    segments = []
    for s0 in range(0, len(levels), per_seg):
        chunk = levels[s0:s0 + per_seg]
        row = []
        for c in range(C):
            vals = [w[c] for w in chunk]
            good = sum(v == "good" for v in vals) / len(vals)
            bad = sum(v in ("bad", "flat") for v in vals) / len(vals)
            row.append("good" if good >= 0.8 else ("bad" if bad >= 0.5 else "warn"))
        segments.append(row)
    if levels:
        arr = np.array(levels)
        good_by_ch = [(arr[:, c] == "good").mean() for c in range(C)]
        good_pct = float(np.mean(good_by_ch))
        not_good_s = [float((arr[:, c] != "good").sum() * WINDOW_S) for c in range(C)]
    else:
        good_by_ch, good_pct, not_good_s = [None] * C, None, [0.0] * C

    ta = {"frontal": None, "parietal": None}
    if metrics is not None and len(metrics):
        m = np.asarray(metrics, dtype=np.float64)
        ta = {"frontal": float(np.nanmean(m[:, 0])), "parietal": float(np.nanmean(m[:, 1]))}
    elif n >= 4 * srate:
        vals = {"frontal": [], "parietal": []}
        w4 = int(4 * srate)
        for start in range(0, n - w4 + 1, w4):
            r = live_metrics(raw[start:start + w4], srate, frontal, parietal)
            for k in vals:
                if r and k in r:
                    vals[k].append(r[k]["theta_alpha"])
        ta = {k: (float(np.mean(v)) if v else None) for k, v in vals.items()}

    gaps = int((np.diff(ts) > 3.0 / srate).sum()) if len(ts) > 1 else 0
    notes = {}
    for label in note_labels or []:
        notes[label] = notes.get(label, 0) + 1
    return {
        "channels": labels,
        "good_pct": None if good_pct is None else round(good_pct, 3),
        "good_by_channel": [None if g is None else round(float(g), 3) for g in good_by_ch],
        "not_good_s": [round(x, 1) for x in not_good_s],
        "segments": segments,
        "segment_s": SEGMENT_S,
        "theta_alpha": {k: (None if v is None else round(v, 3)) for k, v in ta.items()},
        "frontal_label": None,
        "parietal_label": None,
        "samples": int(n),
        "duration_s": round(float(ts[-1] - ts[0]), 2) if len(ts) > 1 else 0.0,
        "gaps": gaps,
        "markers": int(n_markers),
        "notes": notes,
    }
