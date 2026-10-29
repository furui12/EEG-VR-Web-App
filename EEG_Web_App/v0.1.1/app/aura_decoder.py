"""Repair the sample stream published by AuraLSL (v1.1).

Problem
-------
AuraLSL does not push one EEG sample at a time. Each time a new value arrives it
pushes its whole internal buffer as a chunk of R rows x C channels (R=9, C=8 for
the AURA). Inside that buffer the values are an *interleaved* scalar stream
(ch0, ch1, ..., ch7, ch0, ...) where scalar number n is stored at
row n % R, column n % C. As a result an LSL client receives ~R times more "samples"
than the nominal 250 Hz, mostly repeated and with the channels scrambled, and LSL
assigns them overlapping timestamps (each chunk starts ~R/srate seconds in the past).

Fix
---
Comparing each pushed buffer with the previous one reveals which buffer cells were
written since the last push. Because gcd(R, C) == 1, every cell maps to a unique
position n mod R*C (Chinese remainder theorem), so the new scalars can be put back in
order and regrouped into real C-channel samples at the nominal rate.

StreamDecoder detects the format automatically during the first second and falls
back to pass-through for any normal LSL stream.
"""
import math
import time

import numpy as np


class StreamDecoder:
    DETECT_SECONDS = 1.0

    def __init__(self, n_channels, srate):
        self.C = int(n_channels)
        self.srate = float(srate) if srate and srate > 0 else 250.0
        self.mode = "detect"            # detect | passthrough | ring
        self.R = None
        self._detect_rows = []
        self._detect_ts = []
        self._detect_started = None
        # ring state
        self._carry_rows = None
        self._carry_ts = []
        self._prev = None
        self._last_n = None
        self._partial = []
        self._next_ch = 0
        self.resyncs = 0
        self._k = 0
        self._t0 = None
        self._last_t = None

    # ------------------------------------------------------------- public
    @property
    def description(self):
        if self.mode == "ring":
            return f"AuraLSL ring buffer {self.R}x{self.C} decoded to {self.srate:g} Hz"
        return None

    def feed(self, rows, ts):
        """rows: (n, C) array, ts: list of n timestamps -> (samples (m, C), timestamps list)."""
        rows = np.asarray(rows)
        if rows.size == 0:
            return np.empty((0, self.C)), []
        if self.mode == "passthrough":
            return rows, list(ts)
        if self.mode == "ring":
            out, out_ts = self._ring_feed(rows, ts)
            now = time.time()
            if len(out_ts):
                self._last_output = now
            elif now - getattr(self, "_last_output", now) > 2.0:
                # data keeps arriving but nothing decodes: the source changed, detect again
                self._restart_detection()
            elif not hasattr(self, "_last_output"):
                self._last_output = now
            return out, out_ts

        # detection phase: buffer ~1 s, then decide
        if self._detect_started is None:
            self._detect_started = time.time()
        self._detect_rows.append(rows)
        self._detect_ts.extend(ts)
        if time.time() - self._detect_started < self.DETECT_SECONDS:
            return np.empty((0, self.C)), []
        all_rows = np.vstack(self._detect_rows)
        all_ts = self._detect_ts
        self._detect_rows, self._detect_ts = [], []
        self.mode, self.R = self._detect(all_rows, np.asarray(all_ts))
        if self.mode == "ring":
            return self._ring_feed(all_rows, all_ts)
        return all_rows, list(all_ts)

    def _restart_detection(self):
        for attr in ("_P", "_cell_to_n", "_last_output"):
            self.__dict__.pop(attr, None)
        self.__init__(self.C, self.srate)

    # ---------------------------------------------------------- detection
    def _detect(self, rows, ts):
        if len(ts) < 20:
            return "passthrough", None
        back = np.where(np.diff(ts) < -1e-9)[0] + 1
        span = max(ts.max() - ts.min(), 1e-3)
        if len(back) < 10 or len(back) / span < 5:
            return "passthrough", None
        bounds = np.r_[0, back, len(ts)]
        lengths = np.diff(bounds)[1:-1]          # complete blocks only
        if len(lengths) < 5:
            return "passthrough", None
        values, counts = np.unique(lengths, return_counts=True)
        # blocks can be multiples of R when two pushes are read together
        R = int(values[np.argmax(counts)])
        if R < 2 or math.gcd(R, self.C) != 1 or np.mean(lengths % R == 0) < 0.8:
            return "passthrough", None
        blocks = []
        for a, b in zip(bounds[:-1], bounds[1:]):
            blk = rows[a:b]
            if len(blk) % R == 0 and len(blk) > 0:
                blocks.extend(blk[k:k + R] for k in range(0, len(blk), R))
        changes = [int(np.sum(p != q)) for p, q in zip(blocks[:-1], blocks[1:])]
        if len(changes) < 5:
            return "passthrough", None
        med = float(np.median(changes))
        if 1 <= med <= R + 1:
            return "ring", R
        return "passthrough", None

    # --------------------------------------------------------------- ring
    def _ring_setup(self):
        P = self.R * self.C
        self._P = P
        self._cell_to_n = {(n % self.R, n % self.C): n for n in range(P)}

    def _ring_feed(self, rows, ts):
        if not hasattr(self, "_P"):
            self._ring_setup()
        R = self.R
        if self._carry_rows is not None and len(self._carry_rows):
            rows = np.vstack([self._carry_rows, rows])
            ts = list(self._carry_ts) + list(ts)
        ts_arr = np.asarray(ts)
        back = list(np.where(np.diff(ts_arr) < -1e-9)[0] + 1)
        bounds = [0] + back + [len(ts)]

        out_samples, out_ts = [], []
        consumed = 0
        for i, (a, b) in enumerate(zip(bounds[:-1], bounds[1:])):
            blk_len = b - a
            last_segment = i == len(bounds) - 2
            if last_segment and blk_len % R != 0:
                break                       # incomplete push: keep for next call
            if blk_len % R != 0:
                self._resync()
                consumed = b
                continue
            for k in range(a, b, R):
                block = rows[k:k + R]
                block_ts = ts_arr[k + R - 1]
                samples = self._decode_block(block)
                q = len(samples)
                for j, s in enumerate(samples):
                    out_samples.append(s)
                    out_ts.append(self._smooth_ts(block_ts - (q - 1 - j) / self.srate))
            consumed = b
        self._carry_rows = rows[consumed:]
        self._carry_ts = list(ts[consumed:])
        if out_samples:
            return np.asarray(out_samples), out_ts
        return np.empty((0, self.C)), []

    def _smooth_ts(self, observed):
        """Push times are bursty; give samples regular timestamps (k / srate) whose
        offset slowly follows the observed push times (time constant ~2 s)."""
        k = self._k
        self._k += 1
        if self._t0 is None:
            self._t0 = observed
        else:
            self._t0 += 0.002 * ((observed - k / self.srate) - self._t0)
        t = self._t0 + k / self.srate
        if self._last_t is not None and t <= self._last_t:
            t = self._last_t + 1e-6
        self._last_t = t
        return t

    def _resync(self):
        self._prev = None
        self._last_n = None
        self._partial = []
        self._next_ch = 0
        self.resyncs += 1

    def _decode_block(self, cur):
        prev = self._prev
        self._prev = cur.copy()
        if prev is None:
            return []
        rr, cc = np.where(prev != cur)
        if len(rr) == 0:
            return []
        ns = sorted(self._cell_to_n[(int(r), int(c))] for r, c in zip(rr, cc))
        P = self._P
        S = set(ns)
        ends = [n for n in ns if (n + 1) % P not in S]
        end = ends[0] if len(ends) == 1 else ns[-1]
        if self._last_n is None:
            self._last_n = (end - len(ns)) % P
        k = (end - self._last_n) % P
        if k == 0 or k > P // 2:
            self._last_n = end
            self._partial, self._next_ch = [], 0
            self.resyncs += 1
            return []
        samples = []
        for j in range(1, k + 1):
            n = (self._last_n + j) % P
            ch = n % self.C
            val = cur[n % self.R, ch]
            if ch != self._next_ch:          # lost alignment: start a new sample at ch 0
                self._partial = []
                self._next_ch = 0
                if ch != 0:
                    continue
            self._partial.append(val)
            self._next_ch = (ch + 1) % self.C
            if len(self._partial) == self.C:
                samples.append(np.asarray(self._partial, dtype=np.float64))
                self._partial = []
        self._last_n = end
        return samples


def decode_recording(rows, ts, n_channels, srate):
    """Offline helper: decode a whole recorded stream (used by tools/repair_aura_xdf.py)."""
    dec = StreamDecoder(n_channels, srate)
    dec.DETECT_SECONDS = 0
    mode, R = dec._detect(np.asarray(rows), np.asarray(ts))
    dec.mode, dec.R = mode, R
    if mode != "ring":
        return np.asarray(rows), list(ts), dec
    out_rows, out_ts = [], []
    step = 512
    for i in range(0, len(ts), step):
        s, t = dec.feed(rows[i:i + step], ts[i:i + step])
        if len(t):
            out_rows.append(s)
            out_ts.extend(t)
    return (np.vstack(out_rows) if out_rows else np.empty((0, n_channels))), out_ts, dec
