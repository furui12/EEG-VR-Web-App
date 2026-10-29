"""LSL acquisition, real-time pre-processing, live analysis and trial recording."""
import os
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pywt
from pylsl import (
    StreamInfo, StreamInlet, StreamOutlet, cf_float32, proc_clocksync,
    resolve_byprop, resolve_streams,
)
from pylsl import cf_string, local_clock
from scipy.signal import butter, iirnotch, lfilter, lfilter_zi, sosfilt, sosfilt_zi

from app.analysis import channel_quality, live_metrics
from app.aura_decoder import StreamDecoder
from app.trial_store import TrialWriter, export_trial, sample_count

RESOLVE_TIMEOUT = 2.0        # seconds to wait when looking up a stream
CHECK_TIMEOUT = 1.0          # quicker lookup used for the status indicators
LIVE_BUFFER_SECONDS = 10     # history kept for the live plot
RAW_HISTORY_SECONDS = 4      # raw window used for signal quality and live metrics
QUALITY_EVERY = 0.5          # seconds
METRICS_EVERY = 1.0          # seconds
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
        nyq = 0.5 * self.srate
        self.hicut = float(min(max(hicut, 1.0), nyq - 1.0))
        self.locut = float(min(max(locut, 0.01), self.hicut - 0.5))
        self.window = max(int(2 * self.srate), 64)        # 2 s statistics window
        self.wvlt_window = 1 << int(np.ceil(np.log2(max(self.srate, 64))))  # ~1 s, power of 2
        self._init_filters()

    def _init_filters(self):
        if "notch" in self.filts and self.notch_freq < 0.5 * self.srate:
            self.b_notch, self.a_notch = iirnotch(self.notch_freq, 30.0, fs=self.srate)
            self.notch_zi = lfilter_zi(self.b_notch, self.a_notch)
            self.notch_states = None
        else:
            self.filts.discard("notch")

        if "bp" in self.filts:
            self.bp_sos = butter(4, [self.locut, self.hicut], btype="band", fs=self.srate, output="sos")
            self.bp_zi = sosfilt_zi(self.bp_sos)
            self.bp_states = None

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
            if self.notch_states is None:   # start in steady state for the first sample
                self.notch_states = self.notch_zi[:, np.newaxis] * x[0][np.newaxis, :]
            x, self.notch_states = lfilter(self.b_notch, self.a_notch, x, axis=0, zi=self.notch_states)

        if "bp" in self.filts:
            if self.bp_states is None:
                self.bp_states = self.bp_zi[:, :, np.newaxis] * x[0][np.newaxis, np.newaxis, :]
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


def describe_filters(filters, params):
    """Human-readable summary stored with each trial, e.g. 'notch 60 Hz, band-pass 0.5-40 Hz'."""
    parts = []
    f = set(filters or [])
    if "notch" in f:
        parts.append(f"notch {params.get('notch_freq', 60):g} Hz")
    if "bp" in f:
        parts.append(f"band-pass {params.get('locut', 0.5):g}-{params.get('hicut', 40):g} Hz")
    if "bl" in f:
        parts.append("baseline correction")
    if "asr" in f:
        parts.append("robust clipping (ASR-inspired)")
    if "wvlt" in f:
        parts.append("wavelet denoising")
    return ", ".join(parts) or "none"


def stream_summary(info):
    return {
        "name": info.name(), "type": info.type(), "channels": info.channel_count(),
        "rate": info.nominal_srate(), "source_id": info.source_id(),
    }


def _labelled_header(name, stype, labels, srate, fmt, source_id, extra=None):
    info = StreamInfo(name, stype, len(labels), srate, fmt, source_id)
    chans = info.desc().append_child("channels")
    for lab in labels:
        chans.append_child("channel").append_child_value("label", lab)
    for k, v in (extra or {}).items():
        info.desc().append_child_value(k, str(v))
    return info


METRIC_NAMES = ["theta_alpha_frontal", "theta_alpha_parietal",
                "theta_frontal", "alpha_frontal", "theta_parietal", "alpha_parietal"]


class LiveRecorderManager:
    """Owns the LSL connection, the live analysis and the recording of the current trial.

    Life cycle:
      connect()          -> streams are read and shown live (preview, nothing stored)
      set_filters()      -> change pre-processing while previewing
      set_montage()      -> channel names and the channels used for the live metrics
      begin_recording()  -> samples/markers/notes/metrics are written to disk as they arrive
      end_recording()    -> stop writing; the stream stays connected for the next trial
      export_xdf()       -> write the stored trial to an XDF file
      disconnect()
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.connected = False
        self.is_recording = False
        self.config = None
        self.trial_id = None
        self.trial_folder = None
        self.writer = None
        self.processor = None
        self.decoder = None
        self.filters = []
        self.filter_params = {}
        self.outlet = None
        self.metrics_outlet = None
        self.notes_outlet = None
        self.labrecorder = None
        self.labrecorder_file = None
        self.warnings = []
        self.record_started = None
        self.record_duration = 0.0
        self.auto_stop_at = None
        self.record_counts = {"samples": 0, "markers": 0, "notes": 0}
        self.frontal = 0
        self.parietal = 1
        self._stop_event = threading.Event()
        self._threads = []
        self._reset_stream_info()
        self._reset_live()

    # ------------------------------------------------------------------ state
    def _reset_stream_info(self):
        self.eeg_name = None
        self.eeg_source_id = ""
        self.eeg_channels = 0
        self.eeg_srate = 250.0
        self.eeg_labels = []
        self.device_labels = []
        self.eeg_format = "float32"
        self._eeg_info = None
        self.marker_name = None
        self.marker_format = "string"
        self.marker_header_xml = None

    def _reset_live(self):
        self.live = deque()          # (index, timestamp, [values])
        self.live_markers = deque(maxlen=MAX_MARKERS_LIVE)  # (timestamp, label, kind)
        self.total_samples = 0
        self.last_sample_wall = None
        self.raw_hist = np.empty((0, max(self.eeg_channels, 1)))
        self.quality = []
        self.metrics = None
        self.metrics_history = deque(maxlen=600)   # (elapsed or wall time, ta_frontal, ta_parietal)
        self._last_quality = 0.0
        self._last_metrics = 0.0

    def has_data(self, trial_id):
        return (self.trial_id == trial_id and self.trial_folder is not None
                and sample_count(self.trial_folder) > 0)

    def elapsed(self):
        if self.is_recording and self.record_started:
            return time.time() - self.record_started
        return self.record_duration

    @property
    def line_freq(self):
        return float(self.filter_params.get("notch_freq", 60.0)) if self.filter_params else 60.0

    # -------------------------------------------------------------- discovery
    def discover_streams(self):
        try:
            return [stream_summary(s) for s in resolve_streams(wait_time=1.0)]
        except Exception:
            return []

    def check_streams(self, hw_prop, hw_value, vr_prop, vr_value):
        """Look up the EEG and VR streams in parallel (used by the status indicators)."""
        with ThreadPoolExecutor(max_workers=2) as pool:
            eeg_f = pool.submit(resolve_stream, hw_prop, hw_value, CHECK_TIMEOUT)
            vr_f = pool.submit(resolve_stream, vr_prop, vr_value, CHECK_TIMEOUT)
            eeg, vr = eeg_f.result(), vr_f.result()
        return {
            "eeg": {"found": eeg is not None, **(stream_summary(eeg) if eeg else {})},
            "vr": {"found": vr is not None, **(stream_summary(vr) if vr else {})},
        }

    # ------------------------------------------------------------- connection
    def connect(self, config, filters, params, montage=None, force=False):
        """Open the EEG (required) and VR marker (optional) streams and start the live preview."""
        if self.is_recording:
            raise RuntimeError("Stop the recording first.")
        if self.connected and self.config == config and not force:
            self.set_filters(filters, params)
            return self.connection_info()
        self.disconnect()

        hw_prop, hw_value = config["hardware_prop_name"], config["hardware_prop_value"]
        vr_prop, vr_value = config["vr_prop_name"], config["vr_prop_value"]
        eeg_info = resolve_stream(hw_prop, hw_value)
        if eeg_info is None:
            raise StreamNotFoundError(
                f"No LSL stream found with {hw_prop}='{hw_value}'. Is the EEG device streaming?")

        self._reset_stream_info()
        self.warnings = []

        eeg_inlet = StreamInlet(eeg_info, max_buflen=360, processing_flags=proc_clocksync)
        full = eeg_inlet.info(timeout=RESOLVE_TIMEOUT)
        self.eeg_format = CHANNEL_FORMATS.get(full.channel_format(), "float32")
        if self.eeg_format == "string":
            raise RuntimeError(f"Stream '{full.name()}' has text samples; it is not an EEG stream.")
        self._eeg_info = full
        self.eeg_name = full.name()
        self.eeg_source_id = full.source_id()
        self.eeg_channels = full.channel_count()
        self.eeg_srate = full.nominal_srate() or 250.0
        self.device_labels = channel_labels(full)
        self.eeg_labels = list(self.device_labels)
        self.decoder = StreamDecoder(self.eeg_channels, self.eeg_srate)
        self._reset_live()
        if montage:
            self.set_montage(montage.get("labels"), montage.get("frontal"), montage.get("parietal"))
        else:
            self.set_montage(None, None, None)

        out_info = _labelled_header(f"{self.eeg_name}_filtered", "EEG", self.eeg_labels,
                                    self.eeg_srate, cf_float32, f"{self.eeg_source_id}_filtered")
        self.outlet = StreamOutlet(out_info)
        self.metrics_outlet = StreamOutlet(_labelled_header(
            "VREEG_Metrics", "Metrics", METRIC_NAMES, 1.0 / METRICS_EVERY, cf_float32, "vreeg_metrics"))
        self.notes_outlet = StreamOutlet(StreamInfo(
            "VREEG_Annotations", "Markers", 1, 0, cf_string, "vreeg_annotations"))

        marker_inlet = None
        vr_info = resolve_stream(vr_prop, vr_value)
        if vr_info is not None:
            marker_inlet = StreamInlet(vr_info, max_buflen=360, processing_flags=proc_clocksync)
            minfo = marker_inlet.info(timeout=RESOLVE_TIMEOUT)
            self.marker_name = minfo.name()
            self.marker_format = CHANNEL_FORMATS.get(minfo.channel_format(), "string")
            self.marker_header_xml = minfo.as_xml()
        else:
            self.warnings.append(f"VR marker stream {vr_prop}='{vr_value}' not found; only EEG will be recorded.")

        self.config = dict(config)
        self.set_filters(filters, params)
        self._stop_event.clear()
        self.connected = True

        self._threads = [threading.Thread(target=self._eeg_loop, args=(eeg_inlet,), daemon=True)]
        if marker_inlet is not None:
            self._threads.append(threading.Thread(target=self._marker_loop, args=(marker_inlet,), daemon=True))
        for t in self._threads:
            t.start()
        return self.connection_info()

    def connection_info(self):
        return {
            "connected": self.connected,
            "eeg_stream": self.eeg_name,
            "channels": self.eeg_channels,
            "srate": self.eeg_srate,
            "channel_labels": self.eeg_labels,
            "device_labels": self.device_labels,
            "frontal": self.frontal,
            "parietal": self.parietal,
            "marker_stream": self.marker_name,
            "decoding": self.decoder.description if self.decoder else None,
            "warnings": list(self.warnings),
            "config": self.config,
        }

    def set_filters(self, filters, params):
        if self.is_recording:
            raise RuntimeError("Filters cannot be changed while recording.")
        params = dict(params or {})
        with self.lock:
            self.filters = [f for f in (filters or []) if f in ("notch", "bp", "bl", "asr", "wvlt")]
            self.processor = StreamProcessor(
                self.eeg_srate, max(self.eeg_channels, 1), self.filters,
                notch_freq=float(params.get("notch_freq", 60.0)),
                locut=float(params.get("locut", 0.5)),
                hicut=float(params.get("hicut", 40.0)))
            self.filter_params = {
                "notch_freq": self.processor.notch_freq,
                "locut": self.processor.locut,
                "hicut": self.processor.hicut,
            }
        return {"filters": self.filters, "filter_params": self.filter_params}

    def set_montage(self, labels, frontal, parietal):
        """Channel names and which channels feed the theta/alpha metrics."""
        if self.is_recording:
            raise RuntimeError("Channel names cannot be changed while recording.")
        C = self.eeg_channels
        if labels and len(labels) == C:
            self.eeg_labels = [str(l).strip() or f"Ch {i + 1}" for i, l in enumerate(labels)]
        upper = [l.upper() for l in self.eeg_labels]

        def pick(value, preferred, fallback):
            if value is not None and 0 <= int(value) < C:
                return int(value)
            for name in preferred:
                if name in upper:
                    return upper.index(name)
            return min(fallback, C - 1) if C else 0

        self.frontal = pick(frontal, ["FZ", "F3", "F4", "FPZ"], 0)
        self.parietal = pick(parietal, ["PZ", "P3", "P4", "CPZ"], 1)
        return {"labels": self.eeg_labels, "frontal": self.frontal, "parietal": self.parietal}

    def disconnect(self):
        if self.is_recording:
            self.end_recording()
        if not self.connected and not self._threads:
            return
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []
        self.outlet = None   # destroys the outlets
        self.metrics_outlet = None
        self.notes_outlet = None
        self.connected = False

    # ------------------------------------------------------------- headers
    def _headers(self):
        extra = {}
        if self.decoder and self.decoder.description:
            extra["decoding"] = self.decoder.description
        eeg = _labelled_header(self.eeg_name, self._eeg_info.type() or "EEG", self.eeg_labels,
                               self.eeg_srate, self._eeg_info.channel_format(),
                               self.eeg_source_id, extra)
        filt = _labelled_header(f"{self.eeg_name}_filtered", "EEG", self.eeg_labels, self.eeg_srate,
                                cf_float32, f"{self.eeg_source_id}_filtered",
                                {"filters": describe_filters(self.filters, self.filter_params)})
        notes = StreamInfo("Experimenter_Annotations", "Markers", 1, 0, cf_string, "vreeg_annotations")
        metrics = _labelled_header("VREEG_Metrics", "Metrics", METRIC_NAMES, 1.0 / METRICS_EVERY,
                                   cf_float32, "vreeg_metrics",
                                   {"frontal_channel": self.eeg_labels[self.frontal],
                                    "parietal_channel": self.eeg_labels[self.parietal],
                                    "window_s": RAW_HISTORY_SECONDS,
                                    "note": "theta 4-8 Hz, alpha 8-13 Hz, Welch PSD on raw data"})
        return {
            "eeg_header": eeg.as_xml(), "eeg_format": self.eeg_format,
            "filt_header": filt.as_xml(),
            "marker_header": self.marker_header_xml, "marker_format": self.marker_format,
            "notes_header": notes.as_xml(),
            "metrics_header": metrics.as_xml(), "metric_names": METRIC_NAMES,
            "channels": self.eeg_channels,
        }

    # -------------------------------------------------------------- recording
    def begin_recording(self, trial_id, folder, labrecorder_file=None):
        if not self.connected:
            raise RuntimeError("Connect the streams first.")
        if self.is_recording:
            raise RuntimeError("A recording is already running.")
        if self.decoder and self.decoder.mode == "detect":
            raise RuntimeError("Still analysing the EEG stream, try again in a second.")
        meta = {"trial_id": trial_id, "started": time.time(), **self._headers()}
        with self.lock:
            self.writer = TrialWriter(folder, meta)
            self.trial_folder = folder
            self.trial_id = trial_id
            self.record_counts = {"samples": 0, "markers": 0, "notes": 0}
            self.record_started = time.time()
            self.record_duration = 0.0
            self.metrics_history.clear()
            self.is_recording = True
        self._start_labrecorder(labrecorder_file)

    def end_recording(self):
        if not self.is_recording:
            return self.record_summary()
        with self.lock:
            self.is_recording = False
            self.record_duration = time.time() - self.record_started
            if self.writer:
                self.writer.close()
                self.writer = None
        self._stop_labrecorder()
        return self.record_summary()

    def record_summary(self):
        return {
            "samples": self.record_counts["samples"],
            "markers": self.record_counts["markers"],
            "notes": self.record_counts["notes"],
            "duration_s": round(self.elapsed(), 2),
        }

    def add_note(self, label):
        """Experimenter annotation (button / keyboard). Saved when recording, always shown live."""
        label = str(label).strip()[:200]
        if not label:
            raise ValueError("Empty annotation.")
        ts = local_clock()
        with self.lock:
            self.live_markers.append((ts, label, "note"))
            if self.is_recording and self.writer:
                self.writer.write_event("notes", ts, [label])
                self.record_counts["notes"] += 1
        if self.notes_outlet is not None:
            try:
                self.notes_outlet.push_sample([label], ts)
            except Exception:
                pass
        return {"time": ts, "label": label, "saved": self.is_recording,
                "elapsed": round(self.elapsed(), 1)}

    def _eeg_loop(self, inlet):
        max_live = int(LIVE_BUFFER_SECONDS * self.eeg_srate)
        max_hist = int(RAW_HISTORY_SECONDS * self.eeg_srate)
        last_flush = time.time()
        while not self._stop_event.is_set():
            try:
                chunk, ts = inlet.pull_chunk(timeout=0.05, max_samples=1024)
            except Exception:
                continue
            if not ts:
                continue
            raw, ts = self.decoder.feed(np.asarray(chunk, dtype=np.float64), ts)
            if len(ts) == 0:
                continue
            raw = np.asarray(raw, dtype=np.float64)
            with self.lock:
                processor = self.processor
            filt = processor.process_chunk(raw)
            if self.outlet is not None:
                try:
                    self.outlet.push_chunk(filt.astype(np.float32).tolist(), list(ts))
                except Exception:
                    pass
            now = time.time()
            with self.lock:
                if self.is_recording and self.writer:
                    self.writer.write_eeg(ts, raw, filt)
                    self.record_counts["samples"] += len(ts)
                    if now - last_flush > 1.0:
                        self.writer.flush()
                        last_flush = now
                for i, t in enumerate(ts):
                    self.live.append((self.total_samples + i, t, filt[i].tolist()))
                self.total_samples += len(ts)
                self.last_sample_wall = now
                while len(self.live) > max_live:
                    self.live.popleft()
                if self.raw_hist.shape[1] != raw.shape[1]:
                    self.raw_hist = np.empty((0, raw.shape[1]))
                self.raw_hist = np.vstack([self.raw_hist, raw])[-max_hist:]
                hist = self.raw_hist
            self._analyse(hist, ts[-1], now)
        try:
            inlet.close_stream()
        except Exception:
            pass

    def _analyse(self, hist, last_ts, now):
        if now - self._last_quality >= QUALITY_EVERY:
            self._last_quality = now
            n2 = int(2 * self.eeg_srate)
            try:
                self.quality = channel_quality(hist[-n2:], self.eeg_srate, self.line_freq)
            except Exception:
                self.quality = []
        if now - self._last_metrics >= METRICS_EVERY:
            self._last_metrics = now
            try:
                m = live_metrics(hist, self.eeg_srate, self.frontal, self.parietal)
            except Exception:
                m = None
            self.metrics = m
            if m and "frontal" in m and "parietal" in m:
                row = [m["frontal"]["theta_alpha"], m["parietal"]["theta_alpha"],
                       m["frontal"]["theta"], m["frontal"]["alpha"],
                       m["parietal"]["theta"], m["parietal"]["alpha"]]
                t_axis = self.elapsed() if self.is_recording else None
                with self.lock:
                    self.metrics_history.append((t_axis, row[0], row[1]))
                    if self.is_recording and self.writer:
                        self.writer.write_metrics(last_ts, row)
                if self.metrics_outlet is not None:
                    try:
                        self.metrics_outlet.push_sample([float(v) for v in row], last_ts)
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
                if self.is_recording and self.writer:
                    self.writer.write_event("markers", ts, sample)
                    self.record_counts["markers"] += 1
                self.live_markers.append((ts, " | ".join(str(v) for v in sample), "vr"))
        try:
            inlet.close_stream()
        except Exception:
            pass

    # ----------------------------------------------------------- live access
    def get_live(self, since=0):
        with self.lock:
            entries = list(self.live)
            markers = list(self.live_markers)
            last_wall = self.last_sample_wall
            history = [h for h in self.metrics_history if h[0] is not None] if self.is_recording else []
        start_index = entries[0][0] if entries else self.total_samples
        since = max(int(since), start_index)
        new = [e for e in entries if e[0] >= since]

        marker_out = []
        if entries and markers:
            ts_arr = np.fromiter((e[1] for e in entries), dtype=float, count=len(entries))
            for mts, label, kind in markers:
                if mts < ts_arr[0]:
                    continue
                pos = int(np.searchsorted(ts_arr, mts))
                if pos < len(entries):
                    marker_out.append({"index": entries[pos][0], "time": mts, "label": label, "kind": kind})

        return {
            "start_index": new[0][0] if new else since,
            "next_index": (new[-1][0] + 1) if new else since,
            "samples": [e[2] for e in new],
            "markers": marker_out,
            "srate": self.eeg_srate,
            "channel_labels": self.eeg_labels,
            "connected": self.connected,
            "receiving": bool(last_wall and time.time() - last_wall < 2.0),
            "analysing": bool(self.decoder and self.decoder.mode == "detect"),
            "decoding": self.decoder.description if self.decoder else None,
            "is_recording": self.is_recording,
            "elapsed": round(self.elapsed(), 1),
            "remaining": self._remaining(),
            "trial_id": self.trial_id,
            "record_samples": self.record_counts["samples"],
            "record_markers": self.record_counts["markers"],
            "record_notes": self.record_counts["notes"],
            "quality": self.quality,
            "metrics": self.metrics,
            "metrics_history": history[-300:],
            "frontal": self.frontal,
            "parietal": self.parietal,
            "warnings": list(self.warnings),
        }

    def _remaining(self):
        at = self.auto_stop_at
        if not (self.is_recording and at):
            return None
        return round(max(0.0, at - time.time()), 1)

    # ---------------------------------------------------------------- export
    def export_xdf(self, trial_id, export_type, full_path):
        if self.is_recording and self.trial_id == trial_id:
            raise RuntimeError("Stop the recording before exporting.")
        if not self.has_data(trial_id):
            raise RuntimeError("The data of this trial is no longer available.")
        return export_trial(self.trial_folder, export_type, full_path)

    # ----------------------------------------------------- optional LabRecorder
    def _start_labrecorder(self, out_file):
        """If LABRECORDER_CLI_PATH is set, also record the streams with LabRecorderCLI
        for the duration of the trial (independent backup recording)."""
        cli = os.environ.get("LABRECORDER_CLI_PATH", "")
        self.labrecorder, self.labrecorder_file = None, None
        if not cli or not out_file or not self.config:
            return
        if not os.path.exists(cli):
            self.warnings.append(f"LABRECORDER_CLI_PATH does not exist: {cli}")
            return
        c = self.config
        queries = [f"{c['hardware_prop_name']}='{c['hardware_prop_value']}'",
                   f"name='{self.eeg_name}_filtered'", "name='VREEG_Annotations'", "name='VREEG_Metrics'"]
        if self.marker_name:
            queries.append(f"{c['vr_prop_name']}='{c['vr_prop_value']}'")
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
