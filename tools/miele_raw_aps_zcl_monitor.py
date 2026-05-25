#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Miele XKM3000Z Raw APS/ZCL Monitor v2
====================================

Ziel:
  Reiner Rohmonitor für eingehende ZNP/APS/ZCL Frames der Miele XKM3000Z.
  Dieser Monitor soll nichts interpretativ kaputt-dekodieren, sondern alles
  speichern, was über die registrierten Miele-Cluster hereinkommt.

Eigenschaften:
  - READ-ONLY / NO POLLING / NO WRITES
  - registriert lokale Endpoints/Cluster nur zum Empfangen
  - schreibt Text-Log und CSV ins gemountete /work-Verzeichnis
  - dekodiert nur ZCL-Header-Metadaten:
      * frame_control
      * frame_type
      * manufacturer_specific
      * direction
      * disable_default_response
      * manufacturer_code falls vorhanden
      * transaction sequence number
      * command id
      * raw payload
  - markiert bekannte Cluster nur als Label, ohne Sensorlogik

Start im Container:
  docker run --rm -it --network host \
    -v /volume2/docker/zigbee2mqtt:/work \
    python:3.12-alpine \
    python /work/miele_raw_aps_zcl_monitor_v1.py

Logdateien:
  /volume2/docker/zigbee2mqtt/miele_raw_aps_zcl_monitor_v2.log
  /volume2/docker/zigbee2mqtt/miele_raw_aps_zcl_monitor_v2.csv
"""

import csv
import datetime as _dt
import os
import socket
import struct
import sys
import time
from typing import Optional, Tuple

# ============================================================
# 01 - CONNECTION / DEVICE SETTINGS
# ============================================================

HOST = "192.168.xxx.xxx"
PORT = xxxx
TARGET_NWK = 0x537D

PROFILE_ID = 0xC51E
DEVICE_ID = 0x0052

WORKDIR = "/work"
LOG_TXT = os.path.join(WORKDIR, "miele_raw_aps_zcl_monitor_v2.log")
LOG_CSV = os.path.join(WORKDIR, "miele_raw_aps_zcl_monitor_v2.csv")

# ============================================================
# 02 - CLUSTERS / ENDPOINTS
# ============================================================

CLUSTERS_EP210 = [
    0x001B,  # Appliance Control alias
    0xFD02,  # Program / parameter block direct
    0x7D00,  # Runtime reports
    0x0B02,  # Actual incoming event/door cluster observed in AF_INCOMING
    0x7D0B,  # Historical/shifted parser candidate; kept for compatibility
    0x7DFD,  # Program reports
]

# Conservative endpoint registrations. Keep 0x7DFD only on EP210.
REGISTER_ENDPOINTS = [
    (210, CLUSTERS_EP210, CLUSTERS_EP210),
    (212, [0x000A], [0x000A]),
    (213, [0xFD01], [0xFD01]),
    (214, [0xFD00], [0xFD00]),
]

CLUSTER_LABELS = {
    0x001B: "appliance_control_alias",
    0x000A: "time",
    0x7D00: "runtime_report",
    0x0B02: "event_door_bus_actual",
    0x7D0B: "event_bus_legacy_shifted_candidate",
    0x7DFD: "program_report_alias",
    0xFD00: "capability_blob",
    0xFD01: "device_info_service_candidate",
    0xFD02: "program_parameter_direct",
}

ZCL_GLOBAL_COMMANDS = {
    0x00: "read_attributes",
    0x01: "read_attributes_response",
    0x02: "write_attributes",
    0x04: "write_attributes_response",
    0x05: "write_attributes_no_response",
    0x07: "configure_reporting",
    0x09: "read_reporting_configuration_response",
    0x0A: "report_attributes",
    0x0B: "default_response",
}

# ============================================================
# 03 - UTILS
# ============================================================

def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="milliseconds")


def hexs(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data)


def fcs(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    return x


def log(line: str) -> None:
    text = f"[{now_iso()}] {line}"
    print(text, flush=True)
    with open(LOG_TXT, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def init_logs() -> None:
    os.makedirs(WORKDIR, exist_ok=True)
    with open(LOG_TXT, "a", encoding="utf-8") as fh:
        fh.write("\n" + "=" * 100 + "\n")
        fh.write(f"[{now_iso()}] Miele XKM3000Z Raw APS/ZCL Monitor v2 started\n")
        fh.write("=" * 100 + "\n")

    if not os.path.exists(LOG_CSV):
        with open(LOG_CSV, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow([
                "timestamp",
                "src_addr",
                "src_ep",
                "dst_ep",
                "cluster_id",
                "cluster_label",
                "lqi",
                "zcl_fc",
                "frame_type",
                "manufacturer_specific",
                "manufacturer_code",
                "direction",
                "disable_default_response",
                "tsn",
                "cmd_id",
                "cmd_label",
                "zcl_payload_hex",
                "zcl_body_hex",
                "aps_payload_hex",
                "znp_frame_hex",
            ])


def append_csv(row: list) -> None:
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(row)

# ============================================================
# 04 - ZNP SEND / REGISTER ONLY
# ============================================================

def send_znp(sock: socket.socket, cmd0: int, cmd1: int, payload: bytes = b"") -> None:
    frame = bytes([0xFE, len(payload), cmd0, cmd1]) + payload
    frame += bytes([fcs(frame[1:])])
    sock.sendall(frame)
    log(f"SEND_ZNP cmd=0x{cmd0:02X}/0x{cmd1:02X} len={len(payload)} frame={hexs(frame)}")


def af_register(sock: socket.socket, ep: int, in_clusters: list[int], out_clusters: list[int]) -> None:
    payload = bytes([ep])
    payload += struct.pack("<H", PROFILE_ID)
    payload += struct.pack("<H", DEVICE_ID)
    payload += bytes([0x00, 0x00])  # device version / latency

    payload += bytes([len(in_clusters)])
    for c in in_clusters:
        payload += struct.pack("<H", c)

    payload += bytes([len(out_clusters)])
    for c in out_clusters:
        payload += struct.pack("<H", c)

    log(
        f"AF_REGISTER ep={ep} "
        f"in={[hex(c) for c in in_clusters]} "
        f"out={[hex(c) for c in out_clusters]}"
    )
    send_znp(sock, 0x24, 0x00, payload)

# ============================================================
# 05 - ZCL HEADER DECODER ONLY
# ============================================================

def decode_zcl_header(zcl: bytes) -> dict:
    """Decode only the ZCL header, never attribute contents."""
    result = {
        "fc": None,
        "frame_type": "missing",
        "manufacturer_specific": False,
        "manufacturer_code": "",
        "direction": "missing",
        "disable_default_response": False,
        "tsn": "",
        "cmd_id": "",
        "cmd_label": "",
        "body": b"",
    }

    if not zcl:
        return result

    fc = zcl[0]
    result["fc"] = fc

    frame_type = fc & 0x03
    manufacturer_specific = bool(fc & 0x04)
    direction_bit = bool(fc & 0x08)
    disable_default_response = bool(fc & 0x10)

    result["frame_type"] = {
        0: "global",
        1: "cluster_specific",
        2: "reserved_2",
        3: "reserved_3",
    }.get(frame_type, f"unknown_{frame_type}")
    result["manufacturer_specific"] = manufacturer_specific
    result["direction"] = "server_to_client" if direction_bit else "client_to_server"
    result["disable_default_response"] = disable_default_response

    pos = 1
    if manufacturer_specific:
        if len(zcl) < pos + 2:
            result["body"] = zcl[pos:]
            return result
        mfg = struct.unpack("<H", zcl[pos:pos + 2])[0]
        result["manufacturer_code"] = f"0x{mfg:04X}"
        pos += 2

    if len(zcl) <= pos:
        result["body"] = b""
        return result

    tsn = zcl[pos]
    pos += 1
    result["tsn"] = f"0x{tsn:02X}"

    if len(zcl) <= pos:
        result["body"] = b""
        return result

    cmd_id = zcl[pos]
    pos += 1
    result["cmd_id"] = f"0x{cmd_id:02X}"

    if frame_type == 0:
        result["cmd_label"] = ZCL_GLOBAL_COMMANDS.get(cmd_id, f"global_unknown_0x{cmd_id:02X}")
    else:
        result["cmd_label"] = f"cluster_cmd_0x{cmd_id:02X}"

    result["body"] = zcl[pos:]
    return result

# ============================================================
# 06 - ZNP FRAME PARSING
# ============================================================

def parse_af_incoming(frame: bytes) -> Optional[dict]:
    """Parse ZNP AF_INCOMING_MSG (0x44/0x81)."""
    if len(frame) < 5:
        return None

    length = frame[1]
    cmd0 = frame[2]
    cmd1 = frame[3]

    if cmd0 != 0x44 or cmd1 != 0x81:
        return None

    data = frame[4:4 + length]
    if len(data) < 17:
        return None

    group_id = struct.unpack("<H", data[0:2])[0]
    cluster_id = struct.unpack("<H", data[2:4])[0]
    src_addr = struct.unpack("<H", data[4:6])[0]
    src_ep = data[6]
    dst_ep = data[7]
    was_broadcast = data[8]
    lqi = data[9]
    security_use = data[10]
    timestamp = struct.unpack("<I", data[11:15])[0]
    # TI ZNP AF_INCOMING_MSG as observed here contains one byte between
    # timestamp and data_len. Older scripts treated data[15] as data_len,
    # which shifted cluster/event decoding and made 0x0B02 look like 0x7D0B.
    trans_seq_or_reserved = data[15]
    data_len = data[16]
    aps_payload = data[17:17 + data_len]

    return {
        "group_id": group_id,
        "cluster_id": cluster_id,
        "src_addr": src_addr,
        "src_ep": src_ep,
        "dst_ep": dst_ep,
        "was_broadcast": was_broadcast,
        "lqi": lqi,
        "security_use": security_use,
        "timestamp": timestamp,
        "trans_seq_or_reserved": trans_seq_or_reserved,
        "data_len": data_len,
        "aps_payload": aps_payload,
    }


def handle_frame(frame: bytes) -> None:
    parsed = parse_af_incoming(frame)
    if parsed is None:
        # Keep non-AF events visible, but don't spam too much.
        if len(frame) >= 4:
            cmd0, cmd1 = frame[2], frame[3]
            if (cmd0, cmd1) not in [(0x64, 0x00), (0x44, 0x80), (0x45, 0xC4)]:
                log(f"ZNP_OTHER cmd=0x{cmd0:02X}/0x{cmd1:02X} frame={hexs(frame)}")
        return

    cluster_id = parsed["cluster_id"]
    zcl = parsed["aps_payload"]
    zclh = decode_zcl_header(zcl)
    label = CLUSTER_LABELS.get(cluster_id, "unknown")

    body = zclh["body"] if isinstance(zclh.get("body"), bytes) else b""

    # Console text optimized for forensic reading.
    log(
        "APS_IN "
        f"src=0x{parsed['src_addr']:04X} src_ep={parsed['src_ep']} dst_ep={parsed['dst_ep']} "
        f"cluster=0x{cluster_id:04X}({label}) lqi={parsed['lqi']} trans=0x{parsed['trans_seq_or_reserved']:02X} "
        f"zcl_fc=0x{zclh['fc']:02X} " if zclh["fc"] is not None else "zcl_fc=NA "
    )
    log(
        "ZCL_HDR "
        f"cluster=0x{cluster_id:04X} "
        f"type={zclh['frame_type']} "
        f"mfg={zclh['manufacturer_specific']} "
        f"mfg_code={zclh['manufacturer_code']} "
        f"dir={zclh['direction']} "
        f"ddr={zclh['disable_default_response']} "
        f"tsn={zclh['tsn']} "
        f"cmd={zclh['cmd_id']}({zclh['cmd_label']})"
    )
    log(f"ZCL_RAW cluster=0x{cluster_id:04X} payload={hexs(zcl)} body={hexs(body)}")

    append_csv([
        now_iso(),
        f"0x{parsed['src_addr']:04X}",
        parsed["src_ep"],
        parsed["dst_ep"],
        f"0x{cluster_id:04X}",
        label,
        parsed["lqi"],
        f"0x{zclh['fc']:02X}" if zclh["fc"] is not None else "",
        zclh["frame_type"],
        int(bool(zclh["manufacturer_specific"])),
        zclh["manufacturer_code"],
        zclh["direction"],
        int(bool(zclh["disable_default_response"])),
        zclh["tsn"],
        zclh["cmd_id"],
        zclh["cmd_label"],
        hexs(zcl),
        hexs(body),
        hexs(parsed["aps_payload"]),
        hexs(frame),
    ])

# ============================================================
# 07 - RECEIVE LOOP
# ============================================================

def recv_loop(sock: socket.socket) -> None:
    sock.settimeout(1.0)
    buf = bytearray()

    while True:
        try:
            data = sock.recv(4096)
            if data:
                buf.extend(data)

            while True:
                if 0xFE not in buf:
                    buf.clear()
                    break

                idx = buf.index(0xFE)
                if idx:
                    del buf[:idx]

                if len(buf) < 5:
                    break

                length = buf[1]
                total = 4 + length + 1
                if len(buf) < total:
                    break

                frame = bytes(buf[:total])
                del buf[:total]

                # Optional FCS check for diagnostics.
                expected = fcs(frame[1:-1])
                actual = frame[-1]
                if expected != actual:
                    log(f"WARN_BAD_FCS expected=0x{expected:02X} actual=0x{actual:02X} frame={hexs(frame)}")
                    continue

                handle_frame(frame)

        except socket.timeout:
            continue
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log(f"ERROR recv_loop: {type(exc).__name__}: {exc}")
            time.sleep(1)

# ============================================================
# 08 - MAIN
# ============================================================

def main() -> int:
    print("=" * 60)
    print("Miele XKM3000Z Raw APS/ZCL Monitor v2")
    print("READ-ONLY / NO POLLING / NO ATTRIBUTE WRITES")
    print("=" * 60)

    init_logs()
    log(f"Target ZNP={HOST}:{PORT} target_nwk=0x{TARGET_NWK:04X}")
    log(f"Text Log: {LOG_TXT}")
    log(f"CSV  Log: {LOG_CSV}")

    while True:
        try:
            with socket.create_connection((HOST, PORT), timeout=5) as sock:
                log(f"CONNECTED {HOST}:{PORT}")
                for ep, in_clusters, out_clusters in REGISTER_ENDPOINTS:
                    af_register(sock, ep, in_clusters, out_clusters)
                    time.sleep(0.2)

                log("Monitoring all incoming APS/ZCL frames for registered Miele clusters...")
                recv_loop(sock)

        except KeyboardInterrupt:
            log("Stopped by user.")
            return 0
        except Exception as exc:
            log(f"CONNECTION_ERROR {type(exc).__name__}: {exc}; reconnect in 5s")
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
