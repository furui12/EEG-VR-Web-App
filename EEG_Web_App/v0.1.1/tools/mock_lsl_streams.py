"""Publish fake LSL streams to test the app without hardware.

  EEG     : name='AURA_SIM', type='EEG', 8 channels (Fz, Cz, Pz, ...), 250 Hz, float32
            theta (6 Hz) + alpha (10 Hz) + pink-ish noise + 60 Hz line noise
            + strong 60 Hz noise on one channel + an occasional large artifact (to see notch / clipping in action)
  Markers : name='VR_SIM_Markers', type='Markers', string events every few seconds

The default names are deliberately different from the real AURA / VR game streams so a
simulated stream can never be recorded by mistake. In step 2 of the app pick AURA_SIM and
VR_SIM_Markers from the list of streams found on the network.

Usage (from the v0.x folder):
    python tools/mock_lsl_streams.py [--eeg-name AURA_SIM] [--marker-name VR_SIM_Markers]
Stop with Ctrl+C.
"""
import argparse
import random
import time

import numpy as np
from pylsl import StreamInfo, StreamOutlet, cf_float32, cf_string, local_clock

LABELS = ["Fz", "Cz", "Pz", "C3", "C4", "P3", "P4", "Oz"]
EVENTS = ["task_start", "pick_item", "place_item", "error", "task_end"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eeg-name", default="AURA_SIM")
    ap.add_argument("--marker-name", default="VR_SIM_Markers")
    ap.add_argument("--srate", type=float, default=250.0)
    ap.add_argument("--channels", type=int, default=8)
    ap.add_argument("--marker-interval", type=float, default=3.0, help="mean seconds between markers")
    ap.add_argument("--aura-ring", action="store_true",
                    help="emulate the AuraLSL v1.1 output (whole 9x8 ring buffer pushed on every update)")
    args = ap.parse_args()

    n_ch = args.channels
    labels = (LABELS + [f"Ch{i + 1}" for i in range(len(LABELS), n_ch)])[:n_ch]

    eeg_info = StreamInfo(args.eeg_name, "EEG", n_ch, args.srate, cf_float32, f"{args.eeg_name}_mock")
    chans = eeg_info.desc().append_child("channels")
    for lab in labels:
        ch = chans.append_child("channel")
        ch.append_child_value("label", lab)
        ch.append_child_value("unit", "microvolts")
        ch.append_child_value("type", "EEG")
    eeg_out = StreamOutlet(eeg_info, chunk_size=0 if args.aura_ring else 10)

    mk_info = StreamInfo(args.marker_name, "Markers", 1, 0, cf_string, f"{args.marker_name}_mock")
    mk_out = StreamOutlet(mk_info)

    print(f"Streaming EEG '{args.eeg_name}' ({n_ch} ch @ {args.srate:g} Hz) and markers "
          f"'{args.marker_name}'. Ctrl+C to stop.")

    rng = np.random.default_rng()
    phase = rng.uniform(0, 2 * np.pi, size=(3, n_ch))
    t0 = local_clock()
    sent = 0
    next_marker = t0 + rng.exponential(args.marker_interval)
    brown = np.zeros(n_ch)
    # small power-line noise everywhere, strong on one channel (to see the quality indicator)
    line_amp = np.full(n_ch, 2.0)
    line_amp[min(6, n_ch - 1)] = 25.0
    ring = np.zeros((9, n_ch), dtype=np.float32)
    scalar_n = 0
    try:
        while True:
            now = local_clock()
            due = int((now - t0) * args.srate) - sent
            if due > 0:
                t = (sent + np.arange(due)) / args.srate
                theta = 8 * np.sin(2 * np.pi * 6 * t[:, None] + phase[0])
                alpha = 12 * np.sin(2 * np.pi * 10 * t[:, None] + phase[1])
                line = line_amp * np.sin(2 * np.pi * 60 * t[:, None] + phase[2])
                white = rng.normal(0, 4, size=(due, n_ch))
                drift = np.empty((due, n_ch))
                for i in range(due):
                    brown = 0.995 * brown + rng.normal(0, 0.5, n_ch)
                    drift[i] = brown
                data = theta + alpha + line + white + drift
                if rng.random() < 0.01:  # blink / movement artifact
                    data[:, :2] += 150 * np.hanning(due)[:, None]
                stamps = t0 + t
                if args.aura_ring:
                    # AuraLSL behaviour: interleaved scalars written into a 9 x n_ch buffer,
                    # the whole buffer pushed every 9 scalars, timestamps assigned by LSL
                    for v in data.astype(np.float32).reshape(-1):
                        ring[scalar_n % 9, scalar_n % n_ch] = v
                        scalar_n += 1
                        if scalar_n % 9 == 0:
                            eeg_out.push_chunk(ring.tolist())
                else:
                    eeg_out.push_chunk(data.astype(np.float32).tolist(), stamps.tolist())
                sent += due

            if now >= next_marker:
                ev = random.choice(EVENTS)
                mk_out.push_sample([ev], now)
                print(f"marker: {ev}")
                next_marker = now + rng.exponential(args.marker_interval)
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
