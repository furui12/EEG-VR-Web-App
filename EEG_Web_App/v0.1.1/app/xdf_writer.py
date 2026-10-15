"""Minimal XDF 1.0 writer.

Implements the subset of the XDF specification needed to store the streams
recorded by this app (https://github.com/sccn/xdf/wiki/Specifications):
FileHeader, StreamHeader, Samples, ClockOffset and StreamFooter chunks.
Files written here can be opened with pyxdf, EEGLAB (xdfimport) and MNE.

Timestamps are expected to already be in the local LSL clock (the app opens
its inlets with clock synchronization enabled), so clock offsets are 0.
"""
import struct
import time
from xml.sax.saxutils import escape

import numpy as np

NUMERIC_FORMATS = {
    "float32": "<f4",
    "double64": "<f8",
    "int8": "<i1",
    "int16": "<i2",
    "int32": "<i4",
    "int64": "<i8",
}

SAMPLES_PER_CHUNK = 500


def _varlen(n):
    """Variable-length integer as defined by XDF (1, 4 or 8 bytes)."""
    if n < 256:
        return struct.pack("<BB", 1, n)
    if n < 2 ** 32:
        return struct.pack("<BI", 4, n)
    return struct.pack("<BQ", 8, n)


def _chunk(tag, content):
    return _varlen(len(content) + 2) + struct.pack("<H", tag) + content


def _strip_xml_decl(xml):
    xml = xml.strip()
    if xml.startswith("<?xml"):
        xml = xml[xml.index("?>") + 2:].strip()
    return xml


def write_xdf(path, streams):
    """Write streams to an XDF file.

    Each stream is a dict with:
      header_xml     -- stream <info> XML (e.g. pylsl StreamInfo.as_xml())
      channel_format -- 'float32', 'double64', 'int8/16/32/64' or 'string'
      timestamps     -- 1-D sequence of LSL timestamps (seconds)
      samples        -- 2-D array-like (n_samples x n_channels)
    """
    with open(path, "wb") as f:
        f.write(b"XDF:")
        header = (
            '<?xml version="1.0"?><info><version>1.0</version>'
            f"<datetime>{escape(time.strftime('%Y-%m-%dT%H:%M:%S%z'))}</datetime>"
            "<writer>EEG-VR-Web-App</writer></info>"
        )
        f.write(_chunk(1, header.encode("utf-8")))

        for sid, s in enumerate(streams, start=1):
            xml = '<?xml version="1.0"?>' + _strip_xml_decl(s["header_xml"])
            f.write(_chunk(2, struct.pack("<I", sid) + xml.encode("utf-8")))

        for sid, s in enumerate(streams, start=1):
            fmt = s["channel_format"]
            ts = np.asarray(s["timestamps"], dtype="<f8")
            n = len(ts)
            if fmt != "string":
                data = np.asarray(s["samples"]).astype(NUMERIC_FORMATS[fmt])
                if n:
                    data = data.reshape(n, -1)
            else:
                data = s["samples"]

            first_ts = float(ts[0]) if n else 0.0
            last_ts = float(ts[-1]) if n else 0.0
            f.write(_chunk(4, struct.pack("<Idd", sid, first_ts, 0.0)))

            for start in range(0, n, SAMPLES_PER_CHUNK):
                stop = min(start + SAMPLES_PER_CHUNK, n)
                body = bytearray(struct.pack("<I", sid))
                body += _varlen(stop - start)
                for i in range(start, stop):
                    body += struct.pack("<Bd", 8, ts[i])
                    if fmt == "string":
                        for value in data[i]:
                            raw = str(value).encode("utf-8")
                            body += _varlen(len(raw)) + raw
                    else:
                        body += data[i].tobytes()
                f.write(_chunk(3, bytes(body)))

            f.write(_chunk(4, struct.pack("<Idd", sid, last_ts, 0.0)))

            footer = (
                '<?xml version="1.0"?><info>'
                f"<first_timestamp>{first_ts!r}</first_timestamp>"
                f"<last_timestamp>{last_ts!r}</last_timestamp>"
                f"<sample_count>{n}</sample_count>"
                "</info>"
            )
            f.write(_chunk(6, struct.pack("<I", sid) + footer.encode("utf-8")))
    return path
