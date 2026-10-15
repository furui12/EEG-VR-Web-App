"""LSL acquisition, real-time pre-processing and in-memory trial recording."""
import os
import subprocess
import threading
from collections import deque

import numpy as np
import pywt
from pylsl import (
    StreamInfo, StreamInlet, StreamOutlet, cf_float32, proc_clocksync,
    resolve_byprop, resolve_streams,
)
from scipy.signal import butter, iirnotch, lfilter, lfilter_zi, sosfilt, sosfilt_zi

RESOLVE_TIMEOUT = 2.0        # seconds to wait when looking up a stream
LIVE_BUFFER_SECONDS = 10     # history kept for the live plot
MAX_MARKERS_LIVE = 500

CHANNEL_FORMATS = {
    1: "float32", 2: "double64", 3: "string", 4: "int32",
    5: "int16", 6: "int8", 7: "int64",
}


class StreamNotFoundError(RuntimeError):
    pass


def resolve_stream(prop, value, timeout=RESOLVE_TIMEOUT):
    """Return the first StreamInfo matching prop=value, or None (never blocks forever)."""
    if not prop or not value:
        return None
    try:
        found = resolve_byprop(prop, value, 1, timeout)
    except Exception:
        found = []
    return found[0] if found else None


def channel_labels(info):
    labels = []
    try:
        ch = info.desc().child("channels").child("channel")
        while not ch.empty() and len(labels) < info.channel_count():
            labels.append(ch.child_value("label") or f"Ch {len(labels) + 1}")
            ch = ch.next_sibling()
    except Exception:
        labels = []
    if len(labels) != info.channel_count():
        labels = [f"Ch {i + 1}" for i in range(info.channel_count())]
    return labels


class StreamProcessor:
    """Causal, chunk-by-chunk pre-processing applied to the live EEG.

    Filters (applied in this order, each optional):
      notch -- IIR notch at `notch_freq` (Q=30)
      bp    -- 4th-order Butterworth band-pass [locut, hicut]
      bl    -- running-mean baseline removal (EMA, alpha=0.01 per sample)
      asr   -- robust amplitude clipping at median +/- 5 robust SD computed
               over a sliding window. ASR-*inspired* only: it does not do the
               calibration/PCA subspace reconstruction of real ASR.
      wvlt  -- wavelet denoising (db4, universal soft threshold on detail
               coefficients) over a sliding window; the newest samples are
               returned, so the edge of the window adds some distortion.
    Window-based steps keep their own history so the result does not depend on
    how many samples LSL happens to deliver per chunk.
    """

    def __init__(self, srate, n_ch, filts=(), notch_freq=60.0, locut=0.5, hicut=80.0):
        self.srate = float(srate) if srate and srate > 0 else 250.0
        self.n_ch = n_ch
        self.filts = set(filts or [])
        self.notch_freq = notch_freq
        self.locut = locut
        self.hicut = min(hicut, 0.5 * self.srate - 1.0)
        self.window = max(int(2 * self.srate), 64)        # 2 s statistics window
        self.wvlt_window = 1 << int(np.ceil(np.log2(max(self.srate, 64))))  # ~1 s, power of 2
        self._init_filters()

    def _init_filters(self):
        if "notch" in self.filts and self.notch_freq < 0.5 * self.srate:
            self.b_notch, self.a_notch = iirnotch(self.notch_freq, 30.0, fs=self.srate)
            zi = lfilter_zi(self.b_notch, self.a_notch)
            self.notch_states = np.repeat(zi[:, np.newaxis], self.n_ch, axis=1)
        else:
            self.filts.discard("notch")

        if "bp" in self.filts:
            self.bp_sos = butter(4, [self.locut, self.hicut], btype="band", fs=self.srate, output="sos")
            zi = sosfilt_zi(self.bp_sos)
            self.bp_states = np.repeat(zi[:, :, np.newaxis], self.n_ch, axis=2)

        if "bl" in self.filts:
            self.baseline_alpha = 0.01
            self.running_baseline = None

        self.clip_hist = np.empty((0, self.n_ch))
        self.wvlt_hist = np.empty((0, self.n_ch))

    def process_chunk(self, chunk):
        x = np.asarray(chunk, dtype=np.float64).copy()
        n = x.shape[0]
        if n == 0:
            return x

        if "notch" in self.filts:
            x, self.notch_states = lfilter(self.b_notch, self.a_notch, x, axis=0, zi=self.notch_states)

        if "bp" in self.filts:
            x, self.bp_states = sosfilt(self.bp_sos, x, axis=0, zi=self.bp_states)

        if "bl" in self.filts:
            if self.running_baseline is None:
                self.running_baseline = x[0].copy()
            a = self.baseline_alpha
            for i in range(n):
                self.running_baseline = a * x[i] + (1 - a) * self.running_baseline
                x[i] -= self.running_baseline

        if "asr" in self.filts:
            self.clip_hist = np.vstack([self.clip_hist, x])[-self.window:]
            if self.clip_hist.shape[0] >= self.srate / 2:
                med = np.median(self.clip_hist, axis=0)
                rsd = 1.4826 * np.median(np.abs(self.clip_hist - med), axis=0) + 1e-9
                x = np.clip(x, med - 5 * rsd, med + 5 * rsd)

        if "wvlt" in self.filts:
            self.wvlt_hist = np.vstack([self.wvlt_hist, x])[-max(self.wvlt_window, n):]
            seg = self.wvlt_hist
            if seg.shape[0] >= 32:
                wavelet = pywt.Wavelet("db4")
                level = min(4, pywt.dwt_max_level(seg.shape[0], wavelet.dec_len))
                if level >= 1:
                    coeffs = pywt.wavedec(seg, wavelet, level=level, axis=0)
                    sigma = np.median(np.abs(coeffs[-1]), axis=0) / 0.6745
                    thr = sigma * np.sqrt(2 * np.log(seg.shape[0]))
                    coeffs = [coeffs[0]] + [pywt.threshold(c, thr, mode="soft") for c in coeffs[1:]]
                    rec = pywt.waverec(coeffs, wavelet, axis=0)[: seg.shape[0]]
                    x = rec[-n:]

        return x


class LiveRecorderManager:
    """Owns the LSL inlets/outlet and keeps the samples of the current trial in memory."""

    def __init__(self):
        self.lock = threading.Lock()
        self.is_recording = False
        self.trial_id = None
        self._stop_event = threading.Event()
        self._threads = []
        self.outlet = None
        self.labrecorder = None
        self.labrecorder_file = None
        self.warnings = []
        self._reset()

    # ------------------------------------------------------------------ state
    def _reset(self):
        self.eeg_name = None
        self.eeg_srate = 250.0
        self.eeg_labels = []
        self.eeg_format = "float32"
        self.eeg_header_xml = None
        self.filt_header_xml = None
        self.marker_name = None
        self.marker_format = "string"
        self.marker_header_xml = None
        self.raw_ts, self.raw_data, self.filt_data = [], [], []
        self.marker_ts, self.marker_data = [], []
        self.total_samples = 0
        self.live = deque()          # (index, timestamp, [values])
        self.live_markers = deque(maxlen=MAX_MARKERS_LIVE)  # (timestamp, label)

    def has_data(self, trial_id):
        return self.trial_id == trial_id and self.total_samples > 0

    # -------------------------------------------------------------- discovery
    def discover_streams(self):
        try:
            return [{
                "name": s.name(), "type": s.type(), "channels": s.channel_count(),
                "rate": s.nominal_srate(), "source_id": s.source_id(),
            } for s in resolve_streams(wait_time=1.0)]
        except Exception:
            return []

    # -------------------------------------------------------------- recording
    def start_recording(self, trial_id, hw_prop, hw_value, vr_prop, vr_value, filters,
                        labrecorder_file=None):
        if self.is_recording:
            raise RuntimeError("A recording is already running.")

        eeg_info = resolve_stream(hw_prop, hw_value)
        if eeg_info is None:
            raise StreamNotFoundError(
                f"No LSL stream found with {hw_prop}='{hw_value}'. Is the EEG device streaming?")

        self._reset()
        self.warnings = []
        self.trial_id = trial_id

        eeg_inlet = StreamInlet(eeg_info, max_buflen=360, processing_flags=proc_clocksync)
        full = eeg_inlet.info(timeout=RESOLVE_TIMEOUT)
        self.eeg_name = full.name()
        self.eeg_srate = full.nominal_srate() or 250.0
        self.eeg_labels = channel_labels(full)
        self.eeg_format = CHANNEL_FORMATS.get(full.channel_format(), "float32")
        if self.eeg_format == "string":
            raise RuntimeError(f"Stream '{self.eeg_name}' has string samples; it is not an EEG stream.")
        self.eeg_header_xml = full.as_xml()

        out_info = StreamInfo(f"{self.eeg_name}_filtered", "EEG", full.channel_count(),
                              full.nominal_srate(), cf_float32, f"{full.source_id()}_filtered")
        chans = out_info.desc().append_child("channels")
        for lab in self.eeg_labels:
            chans.append_child("channel").append_child_value("label", lab)
        out_info.desc().append_child_value("filters", ",".join(filters) or "none")
        self.filt_header_xml = out_info.as_xml()
        self.outlet = StreamOutlet(out_info)

        marker_inlet = None
        vr_info = resolve_stream(vr_prop, vr_value)
        if vr_info is not None:
            marker_inlet = StreamInlet(vr_info, max_buflen=360, processing_flags=proc_clocksync)
            minfo = marker_inlet.info(timeout=RESOLVE_TIMEOUT)
            self.marker_name = minfo.name()
            self.marker_format = CHANNEL_FORMATS.get(minfo.channel_format(), "string")
            self.marker_header_xml = minfo.as_xml()
        else:
            self.warnings.append(
                f"VR marker stream {vr_prop}='{vr_value}' not found; recording EEG only.")

        processor = StreamProcessor(self.eeg_srate, full.channel_count(), filters)
        self._stop_event.clear()
        self.is_recording = True

        self._start_labrecorder(labrecorder_file, hw_prop, hw_value, vr_prop, vr_value,
                                marker_inlet is not None)

        self._threads = [threading.Thread(target=self._eeg_loop, args=(eeg_inlet, processor), daemon=True)]
        if marker_inlet is not None:
            self._threads.append(threading.Thread(target=self._marker_loop, args=(marker_inlet,), daemon=True))
        for t in self._threads:
            t.start()

        return {"eeg_stream": self.eeg_name, "marker_stream": self.marker_name,
                "warnings": list(self.warnings)}

    def _eeg_loop(self, inlet, processor):
        max_live = int(LIVE_BUFFER_SECONDS * self.eeg_srate)
        while not self._stop_event.is_set():
            try:
                chunk, ts = inlet.pull_chunk(timeout=0.05, max_samples=512)
            except Exception:
                continue
            if not ts:
                continue
            raw = np.asarray(chunk, dtype=np.float64)
            filt = processor.process_chunk(raw)
            if self.outlet is not None:
                try:
                    self.outlet.push_chunk(filt.astype(np.float32).tolist(), ts)
                except Exception:
                    for i, t in enumerate(ts):
                        self.outlet.push_sample(filt[i].tolist(), t)
            with self.lock:
                self.raw_ts.extend(ts)
                self.raw_data.append(raw)
                self.filt_data.append(filt)
                for i, t in enumerate(ts):
                    self.live.append((self.total_samples + i, t, filt[i].tolist()))
                self.total_samples += len(ts)
                while len(self.live) > max_live:
                    self.live.popleft()
        try:
            inlet.close_stream()
        except Exception:
            pass

    def _marker_loop(self, inlet):
        while not self._stop_event.is_set():
            try:
                sample, ts = inlet.pull_sample(timeout=0.1)
            except Exception:
                continue
            if ts is None:
                continue
            with self.lock:
                self.marker_ts.append(ts)
                self.marker_data.append(list(sample))
                self.live_markers.append((ts, " | ".join(str(v) for v in sample)))
        try:
            inlet.close_stream()
        except Exception:
            pass

    def stop_recording(self):
        if not self.is_recording:
            return {"samples": self.total_samples, "markers": len(self.marker_ts)}
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []
        self._stop_labrecorder()
        self.outlet = None   # destroys the *_filtered outlet
        self.is_recording = False
        return {"samples": self.total_samples, "markers": len(self.marker_ts)}

    # ----------------------------------------------------------- live access
    def get_live(self, since=0):
        with self.lock:
            entries = list(self.live)
            markers = list(self.live_markers)
        start_index = entries[0][0] if entries else self.total_samples
        since = max(int(since), start_index)
        new = [e for e in entries if e[0] >= since]

        marker_out = []
        if entries and markers:
            ts_arr = np.fromiter((e[1] for e in entries), dtype=float, count=len(entries))
            for mts, label in markers:
                if mts < ts_arr[0]:
                    continue
                pos = int(np.searchsorted(ts_arr, mts))
                if pos < len(entries):
                    marker_out.append({"index": entries[pos][0], "time": mts, "label": label})

        return {
            "start_index": new[0][0] if new else since,
            "next_index": (new[-1][0] + 1) if new else since,
            "samples": [e[2] for e in new],
            "markers": marker_out,
            "srate": self.eeg_srate,
            "channel_labels": self.eeg_labels,
            "is_recording": self.is_recording,
            "total_samples": self.total_samples,
            "marker_count": len(self.marker_ts),
            "warnings": list(self.warnings),
        }

    # ---------------------------------------------------------------- export
    def export_xdf(self, trial_id, export_type, full_path):
        from app.xdf_writer import write_xdf

        if self.is_recording:
            raise RuntimeError("Stop the recording before exporting.")
        if not self.has_data(trial_id):
            raise RuntimeError(
                "No recorded data for this trial is in memory (was the server restarted?).")

        with self.lock:
            raw = np.vstack(self.raw_data)
            filt = np.vstack(self.filt_data)
            ts = list(self.raw_ts)
            m_ts, m_data = list(self.marker_ts), list(self.marker_data)

        streams = [{"header_xml": self.eeg_header_xml, "channel_format": self.eeg_format,
                    "timestamps": ts, "samples": raw}]
        if export_type == "all":
            streams.append({"header_xml": self.filt_header_xml, "channel_format": "float32",
                            "timestamps": ts, "samples": filt})
            if self.marker_header_xml is not None:
                streams.append({"header_xml": self.marker_header_xml,
                                "channel_format": self.marker_format,
                                "timestamps": m_ts, "samples": m_data})
        write_xdf(full_path, streams)
        return {"streams": len(streams), "samples": len(ts), "markers": len(m_ts)}

    # ----------------------------------------------------- optional LabRecorder
    def _start_labrecorder(self, out_file, hw_prop, hw_value, vr_prop, vr_value, has_vr):
        """If LABRECORDER_CLI_PATH is set, also record the streams with LabRecorderCLI
        for the whole duration of the trial (independent backup recording)."""
        cli = os.path.normpath(os.path.join(os.getcwd(), "..", "LabRecorder-1.18.0-Win_amd64", "LabRecorderCLI.exe"))

        self.labrecorder, self.labrecorder_file = None, None
        if not cli or not out_file:
            return
        if not os.path.exists(cli):
            self.warnings.append(f"LABRECORDER_CLI_PATH does not exist: {cli}")
            return
        queries = [f"{hw_prop}='{hw_value}'", f"name='{self.eeg_name}_filtered'"]
        if has_vr:
            queries.append(f"{vr_prop}='{vr_value}'")
        try:
            self.labrecorder = subprocess.Popen(
                [cli, out_file, *queries], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
            self.labrecorder_file = out_file
        except Exception as exc:
            self.warnings.append(f"Could not start LabRecorderCLI: {exc}")

    def _stop_labrecorder(self):
        proc = self.labrecorder
        if proc is None:
            return
        try:
            proc.stdin.write("\n")   # LabRecorderCLI stops on Enter
            proc.stdin.flush()
            proc.wait(timeout=10)
        except Exception:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        self.labrecorder = None


manager = LiveRecorderManager()
