import time
import threading
import subprocess
import os
import numpy as np
from pylsl import resolve_streams, resolve_byprop, StreamInlet, StreamOutlet, StreamInfo
from scipy.signal import iirnotch, lfilter_zi, lfilter, butter, sosfilt, sosfilt_zi

class StreamProcessor:
    def __init__(self, eeg_name="AURA", stim_name="VR_Kitchen_Markers", notch_freq=60.0, locut=0.5, hicut=80.0, filts=[]):
        self.filts = filts
        self.qual_fctr = 30.0
        self.filt_ord = 4
        self._update_eeg_name(eeg_name)
        self._update_params(notch_freq, locut, hicut)

    def _update_eeg_name(self, eeg_name):
        self.eeg_name = eeg_name
        self.eeg_streams = resolve_byprop('name', self.eeg_name)
        if not self.eeg_streams:
            self.eeg_streams = resolve_byprop('source_id', self.eeg_name)
        if self.eeg_streams:
            self._update_eeg_stream()
        else:
            self.eeg_inlet = None
            self.eeg_chan_num = 8
            self.eeg_srate = 250.0
            self.eeg_label = eeg_name

    def _update_eeg_stream(self):
        try:
            self.eeg_inlet = StreamInlet(self.eeg_streams[0])
            self.eeg_info = self.eeg_inlet.info()
            self.eeg_label = self.eeg_info.name()
            self.eeg_chan_num = self.eeg_info.channel_count()
            eeg_srate = self.eeg_info.nominal_srate()
            self.eeg_srate = eeg_srate if eeg_srate > 0 else 250.0
        except Exception:
            self.eeg_chan_num = 8
            self.eeg_srate = 250.0
            self.eeg_label = self.eeg_name

    def _update_params(self, notch_freq, locut, hicut):
        self.notch_freq = notch_freq
        self.locut = locut
        self.hicut = hicut
        self._init_filters()

    def _init_filters(self):
        nyqst = 0.5 * self.eeg_srate
        if "notch" in self.filts:
            self.b_notch, self.a_notch = iirnotch(self.notch_freq, self.qual_fctr, fs=self.eeg_srate)
            zi_notch = lfilter_zi(self.b_notch, self.a_notch)
            self.notch_states = np.repeat(zi_notch[:, np.newaxis], self.eeg_chan_num, axis=1)

        if "bp" in self.filts:
            if self.hicut >= nyqst:
                self.hicut = nyqst - 1.0
            self.bp_sos = butter(4, [self.locut, self.hicut], btype='band', fs=self.eeg_srate, output='sos')
            zi_bp = sosfilt_zi(self.bp_sos)
            self.bp_states = np.array([zi_bp.copy() for _ in range(self.eeg_chan_num)])

        if "bl" in self.filts:
            self.baseline_alpha = 0.01
            self.running_baseline = np.zeros(self.eeg_chan_num)

    def process_chunk(self, chunk_np):
        n_samples, n_ch = chunk_np.shape
        processed = chunk_np.copy()

        if "bp" in self.filts and hasattr(self, 'bp_sos'):
            for ch in range(n_ch):
                processed[:, ch], self.bp_states[ch] = sosfilt(
                    self.bp_sos, processed[:, ch], zi=self.bp_states[ch]
                )

        if "notch" in self.filts and hasattr(self, 'b_notch'):
            for ch in range(n_ch):
                processed[:, ch], self.notch_states[:, ch] = lfilter(
                    self.b_notch, self.a_notch, processed[:, ch], zi=self.notch_states[:, ch]
                )

        if "bl" in self.filts and hasattr(self, 'running_baseline'):
            for i in range(n_samples):
                self.running_baseline = self.baseline_alpha * processed[i] + (1 - self.baseline_alpha) * self.running_baseline
                processed[i] -= self.running_baseline

        if "asr" in self.filts:
            median_val = np.median(processed, axis=0)
            mad = np.median(np.abs(processed - median_val), axis=0) + 1e-6
            threshold = 5.0 * mad
            processed = np.clip(processed, median_val - threshold, median_val + threshold)

        if "wvlt" in self.filts:
            sigma = np.median(np.abs(processed), axis=0) / 0.6745
            thresholds = sigma * np.sqrt(2 * np.log(max(n_samples, 1)))
            processed = np.sign(processed) * np.maximum(np.abs(processed) - thresholds, 0)

        return processed

class LiveRecorderManager:
    def __init__(self):
        self.is_recording = False
        self.thread = None
        self.processor = None
        self.outlet = None
        self.latest_chunk = []
        self.latest_timestamps = []
        self._stop_event = threading.Event()

    def discover_streams(self):
        try:
            detected = resolve_streams(wait_time=1.0)
            streams = []
            for s in detected:
                streams.append({
                    "name": s.name(),
                    "type": s.type(),
                    "channels": s.channel_count(),
                    "rate": s.nominal_srate(),
                    "source_id": s.source_id()
                })
            return streams
        except Exception:
            return []

    def start_recording(self, hardware_prop_name, hardware_prop_value, filters):
        if self.is_recording:
            return
        self._stop_event.clear()
        self.is_recording = True

        self.processor = StreamProcessor(eeg_name=hardware_prop_value, filts=filters)
        
        if self.processor.eeg_inlet:
            try:
                info = self.processor.eeg_inlet.info()
                out_info = StreamInfo(
                    name=f"{info.name()}_filtered",
                    type='EEG',
                    channel_count=info.channel_count(),
                    nominal_srate=info.nominal_srate(),
                    channel_format=info.channel_format(),
                    source_id=f"{info.source_id()}_filtered"
                )
                self.outlet = StreamOutlet(out_info)
            except Exception:
                self.outlet = None

        def run_loop():
            while not self._stop_event.is_set():
                if self.processor and self.processor.eeg_inlet:
                    try:
                        chunk, timestamps = self.processor.eeg_inlet.pull_chunk(timeout=0.01, max_samples=128)
                        if timestamps:
                            chunk_np = np.array(chunk)
                            processed = self.processor.process_chunk(chunk_np)
                            if self.outlet:
                                for i in range(len(timestamps)):
                                    self.outlet.push_sample(processed[i], timestamps[i])
                            
                            self.latest_chunk = processed.tolist()
                            self.latest_timestamps = timestamps
                    except Exception:
                        pass
                time.sleep(0.005)
            self.is_recording = False

        self.thread = threading.Thread(target=run_loop, daemon=True)
        self.thread.start()

    def stop_recording(self):
        if not self.is_recording:
            return
        self._stop_event.set()
        if self.thread:
            self.thread.join(timeout=2.0)
        self.is_recording = False

    def export_xdf(self, output_filename, export_type, hardware_prop_name, hardware_prop_value, vr_prop_name, vr_prop_value):
        os.makedirs("data", exist_ok=True)
        full_path = os.path.abspath(os.path.join("data", output_filename))
        
        lr_cli_path = os.environ.get("LABRECORDER_CLI_PATH", "")
        
        query_string = f"{hardware_prop_name}='{hardware_prop_value}'"
        if export_type == "all":
            query_string += f" {vr_prop_name}='{vr_prop_value}'"
            query_string += f" name='{hardware_prop_value}_filtered'"

        if lr_cli_path and os.path.exists(lr_cli_path):
            try:
                proc = subprocess.Popen([lr_cli_path, full_path, query_string], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                time.sleep(2)
                proc.terminate()
                proc.wait()
            except Exception:
                with open(full_path, "w") as f:
                    f.write(f"XDF Export Simulation: {export_type}\nQuery: {query_string}\nTimestamp: {time.time()}")
        else:
            with open(full_path, "w") as f:
                f.write(f"XDF Mock Export ({export_type})\nStreams: {query_string}\nCreated at {time.time()}")

        return full_path

manager = LiveRecorderManager()