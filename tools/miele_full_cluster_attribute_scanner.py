#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Miele XKM3000Z Full Cluster/Attribute Scanner v1
=================================================

Ziel:
  - READ-ONLY Scanner für einen kompletten Waschgang
  - Pollt bekannte + verdächtige Endpoints/Cluster/Attribute
  - Lauscht parallel auf Reports
  - Dekodiert ZCL-Werte generisch
  - Erkennt Miele-spezifische Werte:
      * status_code
      * operation_flags inkl. motor_speed_above_threshold
      * phase_raw / phase_b
      * remaining_time_min
      * program_id
      * FD02 parameter_block inkl. B5 rpm/10
      * FD01 capability counts
  - Keine Writes, keine Start/Stop-Kommandos

Wichtig:
  Vorher alle anderen Python-Scanner/Bridge-Container stoppen, sonst gibt es Socket-/ZNP-Konflikte.

Start:
  docker run --rm -it --network host \
    -v /volume2/docker/zigbee2mqtt:/work \
    python:3.12-alpine \
    python /work/miele_full_cluster_attribute_scanner_v1.py

Optional ENV:
  ZNP_HOST=192.168.178.101
  ZNP_PORT=6638
  TARGET_NWK=0x537D
  POLL_DELAY=2.5
  CYCLE_PAUSE=10
"""

import os
import sys
import time
import socket
import struct
from datetime import datetime

ZNP_HOST = os.getenv("ZNP_HOST", "192.168.178.101")
ZNP_PORT = int(os.getenv("ZNP_PORT", "6638"))
TARGET_NWK = int(os.getenv("TARGET_NWK", "0x537D"), 0)

PROFILE_ID = 0xC51E
DEVICE_ID = 0x0052

POLL_DELAY = float(os.getenv("POLL_DELAY", "2.5"))
CYCLE_PAUSE = float(os.getenv("CYCLE_PAUSE", "10"))
READ_TIMEOUT = float(os.getenv("READ_TIMEOUT", "1.0"))

# Kandidaten bewusst breit, aber nicht 0x0000..0xFFFF blind.
# Cluster-ID im Log kann wegen proprietärer Remaps teilweise als 0x7DFD erscheinen.
ENDPOINT_REGISTRY = {
    210: [0x001B, 0x0B02, 0x0A00, 0x7D00, 0x7D0B, 0x7DFD, 0xFD02],
    212: [0x000A],
    213: [0xFD01, 0x7DFD],
    214: [0xFD00, 0x7DFD],
}

# Poll-Plan: dst_ep, src_ep, cluster, attributes, label
# Achtung: src_ep 210 ist lokal stabil; dst_ep ist das Zielgerät/Endpoint.
POLL_PLAN = [
    # Standard-/Statuscluster, bereits sicher nützlich
    (210, 210, 0x001B, [0x0000, 0x0001, 0x0002, 0x0100, 0x0101, 0x0102, 0x0103], "EP210 cluster 0x001B/alias 0x7D00 status/time"),
    (210, 210, 0x7D00, [0x0000, 0x0001, 0x0002, 0x0100, 0x0101, 0x0102, 0x0103], "EP210 cluster 0x7D00 status/time direct"),

    # Miele Parameter / Programm / Parameterblock
    (210, 210, 0xFD02, [0x0000, 0x0001, 0x0002, 0x0010, 0x0011, 0x0012, 0x0018, 0x0019, 0x0020, 0x0021, 0x0030, 0x0031, 0x0032], "EP210 FD02 program/parameter"),

    # FD01 Geräte-/Capability-Infos und Kandidat 0x0020
    (213, 210, 0xFD01, [0x0000, 0x0001, 0x0002, 0x000F, 0x0010, 0x0011, 0x0012, 0x0018, 0x0019, 0x0020, 0x0021, 0x0030, 0x0031, 0x0032], "EP213 FD01 device/capability"),
    (213, 210, 0xFD01, [0x0020], "EP213 FD01/0x0020 focused"),

    # FD00 Capability-Blob + mögliche Action/Descriptor IDs aus Blob
    (214, 210, 0xFD00, [0x0000, 0x0001, 0x0010, 0x0011, 0x0018, 0x0019, 0x0020, 0x0021, 0x0030, 0x0031, 0x0032], "EP214 FD00 base/capability"),
    (214, 210, 0xFD00, list(range(0x8100, 0x8109)), "EP214 FD00 0x8100-0x8108"),
    (214, 210, 0xFD00, [0x1100, 0x1101, 0x1102, 0x1103, 0x1104, 0x1105, 0x1106, 0x1107, 0x11FF, 0x2015, 0x2016], "EP214 FD00 action/descriptor candidates"),

    # Zeitcluster nur Kontext
    (212, 212, 0x000A, [0x0000, 0x0001, 0x0002, 0x0007], "EP212 Time cluster"),

    # 7D0B war in Logs mit Status 0x01/0x11 sichtbar, weiter passiv lesen
    (210, 210, 0x7D0B, [0x0000, 0x0001, 0x0002, 0x0100, 0x0101, 0x0200, 0x0201, 0x0202], "EP210 0x7D0B unknown"),
]

STATUS_MAP = {
    1: "off",
    3: "ready",
    4: "delay_start_active",
    5: "running",
    7: "finished",
    9: "cancelled",
}

PHASE_B_MAP = {
    0: "standby/aus",
    4: "waschen",
    5: "spuelen",
    9: "pumpen",
    10: "schleudern",
    11: "fertig_knitterschutz",
    12: "programm_beendet_tuer_entriegelt",
}

PROGRAM_NAMES = {
    0x7A00: "Express 20",
    0x9200: "Quick Power Wash",
    0x0300: "Pflegeleicht",
    0x0400: "Feinwaesche",
    0x0100: "Baumwolle",
    0x0800: "Wolle",
    0x0900: "Seide",
    0x7B00: "Dunkles/Jeans",
    0x2500: "Outdoor",
    0x1B00: "Impraegnieren",
    0x1500: "Pumpen/Schleudern",
    0x1D00: "Sportwaesche",
    0x8100: "Daunen",
    0x5B03: "Maschine reinigen",
    0x3400: "nur Spuelen/Staerken",
    0x1F00: "Automatic Plus",
}

# Dedupe: gleiche attr/value-Kombinationen nicht jedes Mal voll ausgeben.
last_values = {}
state = {}


def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(msg=""):
    print(f"[{ts()}] {msg}", flush=True)


def hexs(b: bytes) -> str:
    return " ".join(f"{x:02x}" for x in b)


def fcs(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    return x


def send_znp(sock, cmd0, cmd1, payload=b""):
    frame = bytes([0xFE, len(payload), cmd0, cmd1]) + payload
    frame += bytes([fcs(frame[1:])])
    sock.sendall(frame)
    log(f"SEND {hexs(frame)}")


def af_register(sock, ep, clusters):
    # Register same clusters as input and output for passive/active read compatibility.
    in_clusters = clusters
    out_clusters = clusters
    payload = bytes([ep])
    payload += struct.pack("<H", PROFILE_ID)
    payload += struct.pack("<H", DEVICE_ID)
    payload += bytes([0x00, 0x00])
    payload += bytes([len(in_clusters)])
    for c in in_clusters:
        payload += struct.pack("<H", c)
    payload += bytes([len(out_clusters)])
    for c in out_clusters:
        payload += struct.pack("<H", c)
    log(f"AF_REGISTER ep={ep} clusters={[hex(c) for c in clusters]}")
    send_znp(sock, 0x24, 0x00, payload)


def af_data_request(sock, dst_ep, src_ep, cluster_id, zcl_payload):
    trans_id = int(time.time() * 1000) & 0xFF
    payload = struct.pack("<H", TARGET_NWK)
    payload += bytes([dst_ep, src_ep])
    payload += struct.pack("<H", cluster_id)
    payload += bytes([trans_id, 0x00, 30, len(zcl_payload)])
    payload += zcl_payload
    send_znp(sock, 0x24, 0x01, payload)


def read_attrs(sock, dst_ep, src_ep, cluster_id, attrs, label=""):
    # ZCL Read Attributes: fc=0x00, tsn, cmd=0x00
    zcl = bytes([0x00, int(time.time() * 1000) & 0xFF, 0x00])
    for attr in attrs:
        zcl += struct.pack("<H", attr)
    log(f"READ {label} dst_ep={dst_ep} src_ep={src_ep} cluster=0x{cluster_id:04X} attrs={[hex(a) for a in attrs]}")
    af_data_request(sock, dst_ep, src_ep, cluster_id, zcl)


def parse_miele_time(raw_value):
    # Bestätigt: bis 60 direkt, >60 Miele kann je nach dtype/encoding abweichen; hier read value bereits int.
    return raw_value


def decode_phase(raw):
    # raw 0x010403 => low=0x03, phase_b=0x04, high=0x01
    low = raw & 0xFF
    phase_b = (raw >> 8) & 0xFF
    high = (raw >> 16) & 0xFF
    return low, phase_b, high, PHASE_B_MAP.get(phase_b, f"unknown_{phase_b}")


def decode_parameter_block(raw: bytes):
    info = {}
    if len(raw) >= 8:
        # B5 = rpm/10 confirmed; B6/B7 = max rpm uint16 big? Known examples 0x0640=1600.
        b = list(raw[:8])
        info["param_b0"] = b[0]
        info["param_b1"] = b[1]
        info["param_b2"] = b[2]
        info["param_b3"] = b[3]
        info["param_b4"] = b[4]
        info["rpm_current_x10"] = b[5]
        info["rpm_current"] = b[5] * 10
        info["max_rpm"] = (b[6] << 8) | b[7]
    return info


def decode_zcl_value(dtype, payload, pos):
    start = pos
    try:
        if dtype == 0x10:  # bool
            return bool(payload[pos]), pos + 1, payload[start:pos+1]
        if dtype in (0x18, 0x20, 0x30):  # bitmap8/uint8/enum8
            return payload[pos], pos + 1, payload[start:pos+1]
        if dtype in (0x19, 0x21, 0x31):  # bitmap16/uint16/enum16
            val = struct.unpack("<H", payload[pos:pos+2])[0]
            return val, pos + 2, payload[start:pos+2]
        if dtype == 0x22:  # uint24
            val = payload[pos] | (payload[pos+1] << 8) | (payload[pos+2] << 16)
            return val, pos + 3, payload[start:pos+3]
        if dtype == 0x23:  # uint32
            val = struct.unpack("<I", payload[pos:pos+4])[0]
            return val, pos + 4, payload[start:pos+4]
        if dtype == 0x27:  # uint64
            raw = payload[pos:pos+8]
            val = int.from_bytes(raw, "little")
            return val, pos + 8, raw
        if dtype == 0x29:  # int16
            val = struct.unpack("<h", payload[pos:pos+2])[0]
            return val, pos + 2, payload[start:pos+2]
        if dtype == 0x2B:  # int32
            val = struct.unpack("<i", payload[pos:pos+4])[0]
            return val, pos + 4, payload[start:pos+4]
        if dtype in (0x41, 0x42):  # octet/string
            ln = payload[pos]
            raw = payload[pos+1:pos+1+ln]
            if dtype == 0x42:
                try:
                    val = raw.decode("utf-8", errors="replace")
                except Exception:
                    val = hexs(raw)
            else:
                val = raw
            return val, pos + 1 + ln, payload[start:pos+1+ln]
        # Unknown dtype: consume nothing more to avoid misalignment
        return f"UNSUPPORTED_DTYPE_0x{dtype:02X}", pos, b""
    except Exception as e:
        return f"DECODE_ERROR_{e}", len(payload), payload[start:]


def interesting_decode(src_ep, cluster_id, attr, dtype, value, raw):
    # Cluster often appears as 0x7D00/0x7DFD due proprietary remap; attr drives most meaning.
    notes = []

    if attr == 0x0000 and dtype == 0x30 and isinstance(value, int):
        state["status_code"] = value
        notes.append(f"status={STATUS_MAP.get(value, 'unknown')} ({value})")

    if attr == 0x0001 and dtype == 0x20 and isinstance(value, int):
        state["operation_flags"] = value
        notes.append(f"operation_flags={value} bin={value:08b}")
        notes.append(f"motor_speed_above_threshold={bool(value & 0x08)}")

    if attr == 0x0002 and dtype == 0x22 and isinstance(value, int):
        low, phase_b, high, name = decode_phase(value)
        state["phase_raw"] = value
        state["phase_b"] = phase_b
        notes.append(f"phase_raw={value} hex=0x{value:06X} low=0x{low:02X} phase_b={phase_b} high=0x{high:02X} phase={name}")

    if attr == 0x0100 and isinstance(value, int):
        state["start_delay_raw"] = value
        notes.append(f"start_delay_candidate={value}")

    if attr == 0x0102 and isinstance(value, int):
        mins = parse_miele_time(value)
        state["remaining_time_min"] = mins
        notes.append(f"remaining_time_min={mins}")

    if attr == 0x0010 and dtype == 0x22 and isinstance(value, int):
        state["program_id"] = value
        notes.append(f"program_id=0x{value:04X} {PROGRAM_NAMES.get(value, 'unknown_program')}")

    if attr == 0x0020 and dtype == 0x41 and isinstance(value, (bytes, bytearray)):
        pb = bytes(value)
        notes.append(f"parameter_block_len={len(pb)} hex={hexs(pb)}")
        info = decode_parameter_block(pb)
        if info:
            for k, v in info.items():
                state[k] = v
            notes.append("param_decode=" + ", ".join(f"{k}={v}" for k, v in info.items()))

    if attr == 0x0030 and dtype == 0x20 and isinstance(value, int):
        notes.append(f"temperature_level_count={value}")

    if attr == 0x0031 and dtype == 0x20 and isinstance(value, int):
        notes.append(f"spin_level_count={value}")

    if attr == 0x0012 and dtype == 0x27 and isinstance(raw, (bytes, bytearray)):
        # Known bytes: 02 00 58 02 03 00 58 02
        notes.append(f"descriptor_0012_raw={hexs(raw)} maybe two uint32/packed limits")

    return notes


def handle_zcl(src_addr, src_ep, dst_ep, cluster_id, zcl_payload):
    if len(zcl_payload) < 3:
        return
    fc, tsn_, cmd = zcl_payload[0], zcl_payload[1], zcl_payload[2]
    log(f"IN src=0x{src_addr:04X} src_ep={src_ep} dst_ep={dst_ep} cluster=0x{cluster_id:04X} payload={hexs(zcl_payload)}")
    log(f"ZCL fc=0x{fc:02X} tsn=0x{tsn_:02X} cmd=0x{cmd:02X}")

    if cmd == 0x01:  # Read Attributes Response
        pos = 3
        while pos + 3 <= len(zcl_payload):
            attr = struct.unpack("<H", zcl_payload[pos:pos+2])[0]
            pos += 2
            status = zcl_payload[pos]
            pos += 1
            if status != 0x00:
                status_name = {
                    0x86: "UNSUPPORTED_ATTRIBUTE",
                    0x8F: "NOT_AUTHORIZED_OR_STATE_DEPENDENT",
                }.get(status, f"ERR_0x{status:02X}")
                log(f"READ_RESP src_ep={src_ep} cluster=0x{cluster_id:04X} attr=0x{attr:04X} status=0x{status:02X} {status_name}")
                continue
            if pos >= len(zcl_payload):
                break
            dtype = zcl_payload[pos]
            pos += 1
            value, pos, raw = decode_zcl_value(dtype, zcl_payload, pos)
            key = (src_ep, cluster_id, attr)
            value_repr = value.hex(" ") if isinstance(value, (bytes, bytearray)) else repr(value)
            old = last_values.get(key)
            changed = old != (dtype, value_repr)
            last_values[key] = (dtype, value_repr)
            prefix = "READ_VALUE" if changed else "READ_SAME"
            log(f"{prefix} src_ep={src_ep} cluster=0x{cluster_id:04X} attr=0x{attr:04X} dtype=0x{dtype:02X} raw={hexs(raw)} value={value_repr}")
            for note in interesting_decode(src_ep, cluster_id, attr, dtype, value, raw):
                log(f"  -> {note}")

    elif cmd == 0x0A:  # Report Attributes
        pos = 3
        while pos + 3 <= len(zcl_payload):
            attr = struct.unpack("<H", zcl_payload[pos:pos+2])[0]
            pos += 2
            dtype = zcl_payload[pos]
            pos += 1
            value, pos, raw = decode_zcl_value(dtype, zcl_payload, pos)
            key = (src_ep, cluster_id, attr)
            value_repr = value.hex(" ") if isinstance(value, (bytes, bytearray)) else repr(value)
            old = last_values.get(key)
            changed = old != (dtype, value_repr)
            last_values[key] = (dtype, value_repr)
            prefix = "REPORT" if changed else "REPORT_SAME"
            log(f"{prefix} src_ep={src_ep} cluster=0x{cluster_id:04X} attr=0x{attr:04X} dtype=0x{dtype:02X} raw={hexs(raw)} value={value_repr}")
            for note in interesting_decode(src_ep, cluster_id, attr, dtype, value, raw):
                log(f"  -> {note}")
    else:
        log(f"ZCL_OTHER cmd=0x{cmd:02X} raw={hexs(zcl_payload)}")


def parse_frames_from_buffer(buf):
    frames = []
    while True:
        if 0xFE not in buf:
            buf.clear()
            break
        idx = buf.index(0xFE)
        if idx:
            del buf[:idx]
        if len(buf) < 5:
            break
        total = 4 + buf[1] + 1
        if len(buf) < total:
            break
        frame = bytes(buf[:total])
        del buf[:total]
        frames.append(frame)
    return frames


def handle_frame(frame):
    if len(frame) < 5:
        return
    cmd0, cmd1 = frame[2], frame[3]
    # AF_INCOMING_MSG = 0x4481
    if cmd0 == 0x44 and cmd1 == 0x81:
        payload = frame[4:-1]
        if len(payload) < 16:
            log(f"SHORT AF_INCOMING {hexs(frame)}")
            return
        # This layout matched previous scripts/logs.
        cluster_id = struct.unpack("<H", payload[2:4])[0]
        src_addr = struct.unpack("<H", payload[4:6])[0]
        src_ep = payload[6]
        dst_ep = payload[7]
        data_len = payload[15]
        zcl_payload = payload[16:16 + data_len]
        handle_zcl(src_addr, src_ep, dst_ep, cluster_id, zcl_payload)
    elif cmd0 == 0x44 and cmd1 in (0x80,):
        log(f"AF_DATA_CONFIRM {hexs(frame)}")
    elif cmd0 == 0x45 and cmd1 == 0xC4:
        log(f"ZDO/ROUTE? {hexs(frame)}")
    elif cmd0 == 0x64:
        log(f"SYS/AREQ? {hexs(frame)}")
    else:
        log(f"FRAME cmd=0x{cmd0:02X}{cmd1:02X} {hexs(frame)}")


def drain(sock, seconds):
    deadline = time.time() + seconds
    buf = bytearray()
    while time.time() < deadline:
        try:
            data = sock.recv(4096)
            if data:
                log(f"RECV {hexs(data)}")
                buf.extend(data)
                for frame in parse_frames_from_buffer(buf):
                    handle_frame(frame)
            else:
                time.sleep(0.05)
        except socket.timeout:
            pass
        except Exception as e:
            log(f"DRAIN_ERROR {type(e).__name__}: {e}")
            raise


def print_state_snapshot():
    if not state:
        return
    parts = []
    for k in [
        "status_code", "operation_flags", "phase_raw", "phase_b", "remaining_time_min",
        "program_id", "rpm_current", "max_rpm", "param_b0", "param_b1", "param_b2", "param_b3", "param_b4"
    ]:
        if k in state:
            v = state[k]
            if k == "program_id":
                parts.append(f"{k}=0x{v:04X}/{PROGRAM_NAMES.get(v, 'unknown')}")
            else:
                parts.append(f"{k}={v}")
    log("STATE " + " | ".join(parts))


def main():
    log("Miele XKM3000Z Full Cluster/Attribute Scanner v1 - READ ONLY")
    log(f"Target ZNP {ZNP_HOST}:{ZNP_PORT} nwk=0x{TARGET_NWK:04X}")
    log("Vorher alte Python-Scanner stoppen. Keine Writes in diesem Script.")

    while True:
        try:
            with socket.create_connection((ZNP_HOST, ZNP_PORT), timeout=5) as sock:
                sock.settimeout(READ_TIMEOUT)
                log("CONNECTED")

                for ep, clusters in ENDPOINT_REGISTRY.items():
                    af_register(sock, ep, clusters)
                    drain(sock, 0.25)

                log("Registration done. Jetzt Waschgang starten/weiterlaufen lassen.")
                drain(sock, 2.0)

                cycle = 0
                while True:
                    cycle += 1
                    log("=" * 72)
                    log(f"POLL_CYCLE {cycle}")
                    print_state_snapshot()

                    for dst_ep, src_ep, cluster, attrs, label in POLL_PLAN:
                        # Kleiner Batch, damit Antworten sauber bleiben.
                        read_attrs(sock, dst_ep, src_ep, cluster, attrs, label)
                        drain(sock, POLL_DELAY)

                    print_state_snapshot()
                    log(f"CYCLE_PAUSE {CYCLE_PAUSE}s - passive listening")
                    drain(sock, CYCLE_PAUSE)

        except KeyboardInterrupt:
            log("STOP by user")
            sys.exit(0)
        except Exception as e:
            log(f"CONNECTION/SCAN ERROR {type(e).__name__}: {e}")
            log("Reconnect in 5s...")
            time.sleep(5)


if __name__ == "__main__":
    main()
