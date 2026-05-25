#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Miele XKM3000Z Local Zigbee -> MQTT Bridge
============================================================
Phase 4G clean / documented build

Goals
-----
- Keep all experimental functionality from Phase 4F.
- Make the confirmed values cleaner and less misleading.
- Clearly separate confirmed decoding from experimental writes.
- Add derived sensors:
    * motor_speed_above_threshold from operation_flags bit 0x08
    * current_spin_rpm from parameter block byte B5 * 10
    * max_spin_rpm from parameter block B6/B7 big endian
    * target temperature as explicit estimate only

Important findings baked into this version
-----------------------------------------
- Temperature is NOT exposed as confirmed live value by XKM3000Z.
  It is published only as temperature_target_estimated.
- FD01/0x0020 repeatedly answers 0x8F during real wash cycles.
  It is tracked as diagnostic, not used as a productive sensor.
- FD00 action IDs inside capability blob are descriptors, not directly readable attributes.
- operation_flags bit 0x08 is NOT "running". It means motor/drum speed above threshold.
- Running state comes from status_code == 5.
- status_code == 8 is confirmed fault/alarm. For water inlet faults, the
  reliable derived pattern is status_code=8 plus program_phase_a/B0=0x0A.
- program_phase_a/B0 has its own meaning table; B0=10/0x0A is fault/alarm.
- status_code == 12 plus B0=7 is a service/special test state observed in the
  appliance service menu.
- Zigbee Time Cluster sync is intentionally kept enabled and documented; it is
  useful for delay-start/time-base consistency, but not a live telemetry value.
- No confirmed direct Zigbee exposure has been found for B8 NTC live temp,
  S78 float switch, separate door-lock state, heater relay, valve state, or
  individual service-menu consumer/actuator states.

Safety
------
- Normal operation is mostly passive + polling.
- Start delay write is confirmed useful.
- Program selection, pause/cancel, options and spin writes remain experimental.
"""

import datetime
import json
import os
import queue
import socket
import struct
import subprocess
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

# ============================================================
# 01 - USER CONFIG
# ============================================================

HOST = os.getenv("MIELE_ZNP_HOST", "192.168.xxx.xxx")
PORT = int(os.getenv("MIELE_ZNP_PORT", "6638"))
TARGET_NWK = int(os.getenv("MIELE_TARGET_NWK", "0x537D"), 0)

MQTT_HOST = os.getenv("MQTT_HOST", "192.168.xxx.xx")
MQTT_USER = os.getenv("MQTT_USER", "Dein User")
MQTT_PASS = os.getenv("MQTT_PASS", "Dein Passwort")
MQTT_BASE = os.getenv("MQTT_BASE", "miele_xkm3000z")

PROFILE_ID = 0xC51E
DEVICE_ID = 0x0052

POLL_INTERVAL_SEC = int(os.getenv("MIELE_POLL_INTERVAL", "30"))
TIME_SYNC_INTERVAL_SEC = 600
ENABLE_TIME_SYNC = os.getenv("MIELE_ENABLE_TIME_SYNC", "true").lower() in ("1", "true", "yes", "on")
ENABLE_TIME_SYNC = True  # Keep enabled: XKM3000Z uses Zigbee Time Cluster 0x000A as time base.

# Optional one-shot write test. Keep disabled unless deliberately testing.
ENABLE_START_DELAY_TEST = False
START_DELAY_TEST_MINUTES = 15

# ============================================================
# 02 - ZIGBEE / MIELE CLUSTERS AND MQTT TOPICS
# ============================================================

# Confirmed / useful clusters
CLUSTER_APPLIANCE_CONTROL_ALIAS = 0x001B  # readable alias; incoming often appears as 0x7D00
CLUSTER_APPLIANCE_CONTROL_DIRECT = 0x7D00
CLUSTER_TIME = 0x000A
# Program data has two cluster IDs:
#   0xFD02 = outbound/read/write target on EP210
#   0x7DFD = incoming/report alias used by the appliance
# Variable names are kept for compatibility with the existing code.
CLUSTER_PROGRAM_ALIAS = 0xFD02          # outbound/read/write target
CLUSTER_PROGRAM_DIRECT = 0x7DFD         # incoming/report alias
# Confirmed by raw APS/ZCL monitor: real door/event cluster is 0x0B02.
# Older scanner builds accidentally interpreted some frames as 0x7D0B because of a parser offset.
CLUSTER_DOOR_EVENT = 0x0B02
CLUSTER_LEGACY_7D0B_UNSUPPORTED = 0x7D0B  # kept registered only for research/backward comparison
CLUSTER_DEVICE_INFO = 0xFD01
CLUSTER_CAPABILITY = 0xFD00

START_DELAY_WRITE_CLUSTER = CLUSTER_APPLIANCE_CONTROL_ALIAS
PARAM_BLOCK_WRITE_CLUSTER = CLUSTER_PROGRAM_ALIAS
PARAM_BLOCK_ATTR = 0x0020

START_DELAY_COMMAND_TOPIC = f"{MQTT_BASE}/set/start_delay_min"
PROGRAM_COMMAND_TOPIC = f"{MQTT_BASE}/set/program"
CONTROL_COMMAND_TOPIC = f"{MQTT_BASE}/set/control"
SHORT_OPTION_TOPIC = f"{MQTT_BASE}/set/option_short"
WATER_PLUS_TOPIC = f"{MQTT_BASE}/set/option_water_plus"
SPIN_RPM_TOPIC = f"{MQTT_BASE}/set/spin_rpm"

# ============================================================
# 03 - CONFIRMED MAPS / LOOKUPS
# ============================================================

status_map = {
    1: "off",
    3: "ready",
    4: "delay_start_active",
    5: "running",
    7: "finished",
    8: "fault",              # confirmed: generic fault/alarm state
    9: "cancelled",
    12: "service_test_mode",  # observed in service menu / program position 10
}

program_map = {
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
reverse_program_map = {name: pid for pid, name in program_map.items()}

# program_phase_raw is a 3-byte value decoded as:
#   B0 / phase_a = value & 0xFF      -> primary machine/service state
#   B1 / phase_b = (value >> 8) & 0xFF  -> program phase, e.g. washing/spin
#   B2 / phase_c = (value >> 16) & 0xFF -> context/program byte
phase_a_map = {
    1: "off",
    2: "init_wakeup",
    3: "normal_on",
    4: "shutdown_transition",
    7: "special_service_state",  # observed together with status_code=12
    10: "fault_alarm",           # B0=0x0A: confirmed fault/alarm marker
}

phase_b_map = {
    0: "standby/aus",
    4: "waschen",
    5: "spuelen",
    9: "pumpen",
    10: "schleudern",
    11: "fertig_knitterschutz",
    12: "programm_beendet_tuer_entriegelt",
}

# phase byte B0 / phase_a from program_phase_raw.
# Confirmed: 0x0A is the internal fault/alarm substate.
phase_a_map = {
    1: "off",
    2: "init_wakeup",
    3: "normal_on",
    4: "shutdown_transition",
    7: "special_service_state",
    10: "fault_alarm",
}

# Phase 4F/4G: estimated only. The XKM3000Z did not expose target temperature as live attr.
PROGRAM_DEFAULT_TEMP = {
    0x0100: 60,   # Baumwolle
    0x0300: 40,   # Pflegeleicht
    0x0400: 30,   # Feinwaesche
    0x7A00: 20,   # Express 20
    0x2500: 20,   # Outdoor
    0x9200: 40,   # Quick Power Wash
}

PROGRAM_ALLOWED_TEMPS = {
    0x0100: [20, 30, 40, 50, 60, 75, 90],
    0x0300: [20, 30, 40, 50, 60],
    0x0400: [20, 30, 40],
    0x7A00: [20, 30, 40],
    0x2500: [20, 30, 40],
    0x9200: [20, 30, 40, 50, 60],
}

PROGRAM_DEFAULT_RPM = {
    0x0100: 1600,
    0x0300: 1200,
    0x0400: 900,
    0x7A00: 1200,
    0x9200: 1600,
}

# ============================================================
# 04 - EXPERIMENTAL WRITE CANDIDATES
# ============================================================

START_DELAY_MIN = 0
START_DELAY_MAX = 24 * 60
START_DELAY_STEP = 15

# Start/Pause/Cancel: leave experimental. Start is implemented as delay=0 because that worked.
CONTROL_WRITE_CLUSTER = CLUSTER_APPLIANCE_CONTROL_ALIAS
CONTROL_COMMAND_EXECUTION_CMD = 0x00
CONTROL_ACTION_MAP = {
    "start": 0x01,
    "pause": 0x03,
    "cancel": 0x02,
}

# Candidate bits in parameter block. Kept deliberately experimental.
SHORT_OPTION_BYTE_INDEX = 4
SHORT_OPTION_BIT = 0x01
WATER_PLUS_BYTE_INDEX = 3
WATER_PLUS_BIT = 0x04

# ============================================================
# 05 - GLOBAL STATE / HA DISCOVERY
# ============================================================

state: Dict[str, Any] = {}
command_queue: "queue.Queue[Tuple[str, str]]" = queue.Queue()

HA_DISCOVERY_PREFIX = "homeassistant"
DEVICE_ID_HA = "miele_xkm3000z"
DEVICE_NAME = "Miele Waschmaschine XKM3000Z"

# ============================================================
# 06 - SMALL HELPERS
# ============================================================

def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def hexs(b: Union[bytes, bytearray]) -> str:
    return " ".join(f"{x:02x}" for x in b)


def fcs(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    return x


def miele_time_to_minutes(value: int) -> int:
    """Miele encodes duration as HH:MM in a uint16: high byte=hours, low byte=minutes."""
    hours = (value >> 8) & 0xFF
    minutes = value & 0xFF
    return hours * 60 + minutes


def minutes_to_miele_time(minutes: int) -> int:
    hours = minutes // 60
    mins = minutes % 60
    return (hours << 8) | mins


def zigbee_time_now() -> int:
    epoch = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return int((now - epoch).total_seconds())


def parse_bool_payload(payload: str) -> bool:
    return str(payload).strip().upper() in ("ON", "1", "TRUE", "YES")


def refresh_fault_derived_state() -> None:
    """Derive fault sensors from confirmed status/phase pattern.

    Confirmed water-inlet fault pattern from tests:
    - status_code == 8
    - program_phase_a/B0 == 0x0A (10) -> fault/alarm

    The exact Miele F-code is not exported over Zigbee in the observed frames.
    Therefore the bridge publishes a conservative generic fault plus a best
    context hint when B1/phase_b indicates the program was in washing/filling.
    """
    status_code = state.get("status_code")
    phase_a = state.get("program_phase_a")
    phase_b = state.get("program_phase_b")

    is_fault = (status_code == 8) or (phase_a == 10)
    update("fault_active", "ON" if is_fault else "OFF")

    if not is_fault:
        update("fault_text", "none")
        return

    if status_code == 8 and phase_a == 10 and phase_b == 4:
        update("fault_text", "wasserzulauf_fehler_oder_zulauf_pruefen")
    elif status_code == 8 and phase_a == 10:
        update("fault_text", "fault_alarm")
    elif status_code == 8:
        update("fault_text", "fault_status")
    else:
        update("fault_text", "fault_phase")

# ============================================================
# 07 - MQTT PUBLISH / SUBSCRIBE
# ============================================================

def mqtt_publish(topic: str, value: Any, retain: bool = True) -> None:
    full_topic = f"{MQTT_BASE}/{topic}"
    payload = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    cmd = [
        "mosquitto_pub", "-h", MQTT_HOST, "-u", MQTT_USER, "-P", MQTT_PASS,
        "-t", full_topic, "-m", payload,
    ]
    if retain:
        cmd.append("-r")
    subprocess.run(cmd, check=False)
    log(f"MQTT {full_topic} = {payload}")


def mqtt_publish_raw(topic: str, payload: str, retain: bool = True) -> None:
    cmd = [
        "mosquitto_pub", "-h", MQTT_HOST, "-u", MQTT_USER, "-P", MQTT_PASS,
        "-t", topic, "-m", payload,
    ]
    if retain:
        cmd.append("-r")
    subprocess.run(cmd, check=False)
    log(f"MQTT {topic} = {payload}")


def publish_state() -> None:
    mqtt_publish("state", state)


def update(key: str, value: Any) -> None:
    """Publish only when changed, then update retained JSON state."""
    if state.get(key) != value:
        state[key] = value
        mqtt_publish(key, value)
        publish_state()


def mqtt_subscribe_commands() -> None:
    topics = [
        START_DELAY_COMMAND_TOPIC,
        PROGRAM_COMMAND_TOPIC,
        CONTROL_COMMAND_TOPIC,
        SHORT_OPTION_TOPIC,
        WATER_PLUS_TOPIC,
        SPIN_RPM_TOPIC,
    ]
    cmd = ["mosquitto_sub", "-h", MQTT_HOST, "-u", MQTT_USER, "-P", MQTT_PASS]
    for t in topics:
        cmd += ["-t", t]
    cmd.append("-v")

    while True:
        try:
            log("MQTT command subscription starting")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                topic, payload = line.split(" ", 1) if " " in line else (START_DELAY_COMMAND_TOPIC, line)
                payload = payload.strip()
                topic_map = {
                    START_DELAY_COMMAND_TOPIC: "start_delay_min",
                    PROGRAM_COMMAND_TOPIC: "program",
                    CONTROL_COMMAND_TOPIC: "control",
                    SHORT_OPTION_TOPIC: "option_short",
                    WATER_PLUS_TOPIC: "option_water_plus",
                    SPIN_RPM_TOPIC: "spin_rpm",
                }
                if topic in topic_map:
                    command_queue.put((topic_map[topic], payload))
                    log(f"MQTT COMMAND {topic} = {payload}")
            log(f"mosquitto_sub exited rc={proc.wait()}; reconnecting in 5s")
            time.sleep(5)
        except Exception as exc:
            log(f"mosquitto_sub error: {exc}; reconnecting in 5s")
            time.sleep(5)


def start_command_listener() -> None:
    threading.Thread(target=mqtt_subscribe_commands, daemon=True).start()

# ============================================================
# 08 - HOME ASSISTANT DISCOVERY
# ============================================================

def publish_ha_discovery() -> None:
    device = {
        "identifiers": [DEVICE_ID_HA],
        "name": DEVICE_NAME,
        "manufacturer": "Miele",
        "model": "XKM3000Z",
    }

    sensors: List[Tuple[str, str, Optional[str], Optional[str], Optional[str]]] = [
        ("status", "Status", None, None, None),
        ("program", "Programm", None, None, None),
        ("program_id", "Programm-ID", None, None, None),
        ("program_hex", "Programm-ID Hex", None, None, None),
        ("program_last_update", "Programm zuletzt aktualisiert", None, "timestamp", None),
        ("remaining_time_min", "Restlaufzeit", "min", "duration", None),
        ("remaining_time_raw", "Restlaufzeit Raw", None, None, None),
        ("start_delay_min", "Startzeitvorwahl", "min", "duration", None),
        ("start_delay_raw", "Startzeitvorwahl Raw", None, None, None),
        ("status_code", "Status Code", None, None, None),
        ("operation_flags", "Operation Flags", None, None, None),
        ("program_phase_raw", "Programmphase Raw", None, None, None),
        ("program_phase_a", "Programmphase A / B0", None, None, None),
        ("program_phase_a_text", "Programmphase A / B0 Text", None, None, None),
        ("program_phase_b", "Programmphase B / B1", None, None, None),
        ("program_phase_c", "Programmphase C", None, None, None),
        ("program_phase_text", "Programmphase", None, None, None),
        ("parameter_block_hex", "Parameterblock Hex", None, None, None),
        ("param_1", "Parameter 1", None, None, None),
        ("param_2", "Parameter 2", None, None, None),
        ("param_3", "Parameter 3", None, None, None),
        ("param_4", "Parameter 4", None, None, None),
        ("param_5", "Parameter 5", None, None, None),
        ("spin_stage_raw", "Schleuderphase Raw B5", None, None, None),
        ("current_spin_rpm", "Aktuelle Trommeldrehzahl", "rpm", None, "mdi:speedometer"),
        ("max_spin_rpm", "Maximale Schleuderdrehzahl", "rpm", None, "mdi:speedometer"),
        ("spin_rpm", "Schleuderdrehzahl Soll/Max", "rpm", None, "mdi:speedometer"),
        ("option_short_candidate", "Kurz Kandidat", None, None, None),
        ("water_plus_available", "Wasser Plus verfügbar", None, None, None),
        ("temperature_target_estimated", "Zieltemperatur geschätzt", "°C", "temperature", "mdi:thermometer"),
        ("temperature_estimation_source", "Temperatur Quelle", None, None, None),
        ("temperature_allowed_values", "Temperaturstufen erlaubt", None, None, None),
        ("fd01_0020_status", "FD01 0x0020 Status", None, None, None),
        ("fault_text", "Fehlertext", None, None, "mdi:alert"),
        ("fault_text", "Fehlertext abgeleitet", None, None, "mdi:alert-circle-outline"),
        ("write_result", "Letzter Schreibbefehl", None, None, None),
        ("event_raw_hex", "Event Raw Hex", None, None, None),
        ("event_class", "Event Class", None, None, None),
        ("event_code", "Event Code", None, None, None),
        ("event_value", "Event Value", None, None, None),
    ]

    for key, name, unit, device_class, icon in sensors:
        cfg: Dict[str, Any] = {
            "name": name,
            "unique_id": f"{DEVICE_ID_HA}_{key}",
            "state_topic": f"{MQTT_BASE}/{key}",
            "availability_topic": f"{MQTT_BASE}/bridge",
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
        }
        if unit:
            cfg["unit_of_measurement"] = unit
        if device_class:
            cfg["device_class"] = device_class
        if icon:
            cfg["icon"] = icon
        mqtt_publish_raw(f"{HA_DISCOVERY_PREFIX}/sensor/{DEVICE_ID_HA}/{key}/config", json.dumps(cfg), retain=True)

    binary_sensors = [
        ("door_open", "Tür geöffnet", "door"),
        ("motor_speed_above_threshold", "Motor dreht schnell", "running"),
        ("running", "Programm läuft", "running"),
        ("fault_active", "Fehler aktiv", "problem"),
    ]
    for key, name, device_class in binary_sensors:
        cfg = {
            "name": name,
            "unique_id": f"{DEVICE_ID_HA}_{key}",
            "state_topic": f"{MQTT_BASE}/{key}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability_topic": f"{MQTT_BASE}/bridge",
            "payload_available": "online",
            "payload_not_available": "offline",
            "device_class": device_class,
            "device": device,
        }
        mqtt_publish_raw(f"{HA_DISCOVERY_PREFIX}/binary_sensor/{DEVICE_ID_HA}/{key}/config", json.dumps(cfg), retain=True)

    number_cfg = {
        "name": "Startzeitvorwahl setzen",
        "unique_id": f"{DEVICE_ID_HA}_set_start_delay_min",
        "command_topic": START_DELAY_COMMAND_TOPIC,
        "state_topic": f"{MQTT_BASE}/start_delay_min",
        "availability_topic": f"{MQTT_BASE}/bridge",
        "payload_available": "online",
        "payload_not_available": "offline",
        "min": START_DELAY_MIN,
        "max": START_DELAY_MAX,
        "step": START_DELAY_STEP,
        "mode": "box",
        "unit_of_measurement": "min",
        "device_class": "duration",
        "device": device,
    }
    mqtt_publish_raw(f"{HA_DISCOVERY_PREFIX}/number/{DEVICE_ID_HA}/set_start_delay_min/config", json.dumps(number_cfg), retain=True)

    select_cfg = {
        "name": "Programm wählen (experimentell)",
        "unique_id": f"{DEVICE_ID_HA}_program_select",
        "command_topic": PROGRAM_COMMAND_TOPIC,
        "state_topic": f"{MQTT_BASE}/program",
        "availability_topic": f"{MQTT_BASE}/bridge",
        "payload_available": "online",
        "payload_not_available": "offline",
        "options": sorted(program_map.values()),
        "device": device,
    }
    mqtt_publish_raw(f"{HA_DISCOVERY_PREFIX}/select/{DEVICE_ID_HA}/program/config", json.dumps(select_cfg), retain=True)

    for key, label, cmd_topic in [
        ("option_short", "Kurz (experimentell)", SHORT_OPTION_TOPIC),
        ("option_water_plus", "Wasser Plus (experimentell)", WATER_PLUS_TOPIC),
    ]:
        cfg = {
            "name": label,
            "unique_id": f"{DEVICE_ID_HA}_{key}",
            "command_topic": cmd_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "optimistic": True,
            "availability_topic": f"{MQTT_BASE}/bridge",
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
        }
        mqtt_publish_raw(f"{HA_DISCOVERY_PREFIX}/switch/{DEVICE_ID_HA}/{key}/config", json.dumps(cfg), retain=True)

    spin_number_cfg = {
        "name": "Schleuderdrehzahl setzen (experimentell)",
        "unique_id": f"{DEVICE_ID_HA}_set_spin_rpm",
        "command_topic": SPIN_RPM_TOPIC,
        "state_topic": f"{MQTT_BASE}/max_spin_rpm",
        "availability_topic": f"{MQTT_BASE}/bridge",
        "payload_available": "online",
        "payload_not_available": "offline",
        "min": 0,
        "max": 1600,
        "step": 100,
        "mode": "box",
        "unit_of_measurement": "rpm",
        "device": device,
    }
    mqtt_publish_raw(f"{HA_DISCOVERY_PREFIX}/number/{DEVICE_ID_HA}/set_spin_rpm/config", json.dumps(spin_number_cfg), retain=True)

    for action, label in [
        ("start", "Start jetzt / Startzeit 0"),
        ("pause", "Pause (experimentell)"),
        ("cancel", "Abbrechen (experimentell)"),
    ]:
        cfg = {
            "name": label,
            "unique_id": f"{DEVICE_ID_HA}_{action}_button",
            "command_topic": CONTROL_COMMAND_TOPIC,
            "payload_press": action,
            "availability_topic": f"{MQTT_BASE}/bridge",
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
        }
        mqtt_publish_raw(f"{HA_DISCOVERY_PREFIX}/button/{DEVICE_ID_HA}/{action}/config", json.dumps(cfg), retain=True)

# ============================================================
# 09 - ZNP SEND / REGISTER / READ / WRITE
# ============================================================

def send_znp(sock: socket.socket, cmd0: int, cmd1: int, payload: bytes = b"") -> None:
    frame = bytes([0xFE, len(payload), cmd0, cmd1]) + payload
    frame += bytes([fcs(frame[1:])])
    sock.sendall(frame)


def af_register(sock: socket.socket, ep: int, in_clusters: Iterable[int], out_clusters: Iterable[int]) -> None:
    in_clusters = list(in_clusters)
    out_clusters = list(out_clusters)
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
    send_znp(sock, 0x24, 0x00, payload)
    log(f"AF_REGISTER ep={ep} clusters={[hex(c) for c in in_clusters]}")


def af_data_request(sock: socket.socket, dst_ep: int, src_ep: int, cluster_id: int, zcl_payload: bytes) -> None:
    trans_id = int(time.time()) & 0xFF
    payload = struct.pack("<H", TARGET_NWK)
    payload += bytes([dst_ep, src_ep])
    payload += struct.pack("<H", cluster_id)
    payload += bytes([trans_id, 0x00, 30, len(zcl_payload)])
    payload += zcl_payload
    send_znp(sock, 0x24, 0x01, payload)


def send_read_attrs(sock: socket.socket, dst_ep: int, src_ep: int, cluster_id: int, attrs: Iterable[int]) -> None:
    zcl = bytes([0x00, int(time.time()) & 0xFF, 0x00])
    attrs = list(attrs)
    for attr in attrs:
        zcl += struct.pack("<H", attr)
    af_data_request(sock, dst_ep, src_ep, cluster_id, zcl)
    log(f"READ dst_ep={dst_ep} src_ep={src_ep} cluster=0x{cluster_id:04X} attrs={[hex(a) for a in attrs]}")


def send_time_sync(sock: socket.socket) -> None:
    """Send Zigbee Time Cluster synchronization.

    Kept intentionally enabled by default. The XKM3000Z interacts with
    endpoint 212 / cluster 0x000A and delay-start behaviour depends on a sane
    internal time base. This is not a live sensor and does not unlock service
    diagnostics.
    """
    ztime = zigbee_time_now()
    zcl = bytes([0x10, 1, 0x05]) + struct.pack("<H", 0x0000) + bytes([0xE2]) + struct.pack("<I", ztime)
    af_data_request(sock, 212, 212, CLUSTER_TIME, zcl)
    zcl2 = bytes([0x10, 2, 0x05]) + struct.pack("<H", 0x0001) + bytes([0x18, 0x03])
    af_data_request(sock, 212, 212, CLUSTER_TIME, zcl2)
    log("Time sync sent")

# ============================================================
# 10 - CONFIRMED AND EXPERIMENTAL WRITES
# ============================================================

def send_start_delay(sock: socket.socket, minutes: int, cluster_id: int = START_DELAY_WRITE_CLUSTER, no_response: bool = True) -> None:
    """Confirmed useful write. Valid values are 15-minute steps; 0 starts immediately from delay-start."""
    minutes = int(minutes)
    if not START_DELAY_MIN <= minutes <= START_DELAY_MAX:
        raise ValueError(f"minutes must be between {START_DELAY_MIN} and {START_DELAY_MAX}")
    if minutes % START_DELAY_STEP != 0:
        raise ValueError(f"minutes must be a multiple of {START_DELAY_STEP}")

    cmd = 0x05 if no_response else 0x02
    zcl = bytes([0x10, int(time.time()) & 0xFF, cmd])
    zcl += struct.pack("<H", 0x0100)
    zcl += bytes([0x21])
    zcl += struct.pack("<H", minutes_to_miele_time(minutes))
    af_data_request(sock, 210, 210, cluster_id, zcl)
    msg = f"sent start_delay_min={minutes} cluster=0x{cluster_id:04X} no_response={no_response}"
    update("write_result", msg)
    log(f"SENT {msg}")


def send_program(sock: socket.socket, program_id: int) -> None:
    """Experimental. Machines with physical rotary knob may ignore or revert this."""
    zcl = bytes([0x10, int(time.time()) & 0xFF, 0x05])
    zcl += struct.pack("<H", 0x0010)
    zcl += bytes([0x22])
    zcl += struct.pack("<I", program_id)[0:3]
    af_data_request(sock, 210, 210, CLUSTER_PROGRAM_ALIAS, zcl)
    msg = f"sent program_id=0x{program_id:04X} cluster=0xFD02 no_response=True experimental"
    update("write_result", msg)
    log(f"SENT {msg}")


def send_control_command(sock: socket.socket, action: str) -> None:
    """Experimental generic Appliance Control command. Start is handled elsewhere via delay=0."""
    action = str(action).strip().lower()
    if action not in CONTROL_ACTION_MAP:
        raise ValueError(f"unknown control action '{action}'. Valid: {', '.join(sorted(CONTROL_ACTION_MAP))}")
    command_id = CONTROL_ACTION_MAP[action]
    zcl = bytes([0x01, int(time.time()) & 0xFF, CONTROL_COMMAND_EXECUTION_CMD, command_id])
    af_data_request(sock, 210, 210, CONTROL_WRITE_CLUSTER, zcl)
    msg = f"sent control={action} command_id=0x{command_id:02X} cluster=0x{CONTROL_WRITE_CLUSTER:04X} experimental"
    update("write_result", msg)
    log(f"SENT {msg}")


def get_current_parameter_block() -> bytearray:
    raw = state.get("parameter_block_hex")
    if not raw:
        raise ValueError("no parameter_block_hex known yet; wait for polling/report first")
    return bytearray(int(x, 16) for x in str(raw).split())


def send_parameter_block(sock: socket.socket, block: Union[bytes, bytearray], reason: str = "parameter_block") -> None:
    """Experimental. Write with response to learn READ_ONLY/INVALID_VALUE/etc."""
    block = bytes(block)
    if len(block) < 2:
        raise ValueError("parameter block too short")
    zcl = bytes([0x10, int(time.time()) & 0xFF, 0x02])
    zcl += struct.pack("<H", PARAM_BLOCK_ATTR)
    zcl += bytes([0x41, len(block)]) + block
    af_data_request(sock, 210, 210, PARAM_BLOCK_WRITE_CLUSTER, zcl)
    msg = f"sent {reason} parameter_block={hexs(block)} cluster=0x{PARAM_BLOCK_WRITE_CLUSTER:04X} attr=0x{PARAM_BLOCK_ATTR:04X} with_response=True experimental"
    update("write_result", msg)
    log(f"SENT {msg}")


def send_option_bit(sock: socket.socket, option_name: str, enabled: bool, byte_index: int, bit_mask: int) -> None:
    block = get_current_parameter_block()
    if byte_index >= len(block):
        raise ValueError(f"parameter block too short for {option_name}: len={len(block)} index={byte_index}")
    if enabled:
        block[byte_index] |= bit_mask
    else:
        block[byte_index] &= (~bit_mask) & 0xFF
    send_parameter_block(sock, block, reason=f"option_{option_name}={'ON' if enabled else 'OFF'}")


def send_spin_rpm(sock: socket.socket, rpm: int) -> None:
    rpm = int(rpm)
    allowed = {0, 400, 600, 800, 900, 1000, 1100, 1200, 1400, 1500, 1600}
    if rpm not in allowed:
        raise ValueError(f"unsupported rpm={rpm}; allowed={sorted(allowed)}")
    block = get_current_parameter_block()
    if len(block) < 8:
        raise ValueError(f"parameter block too short for spin rpm: len={len(block)}")
    block[-2:] = struct.pack(">H", rpm)
    send_parameter_block(sock, block, reason=f"spin_rpm={rpm}")

# ============================================================
# 11 - DECODING: DATA TYPES AND DERIVED VALUES
# ============================================================

def parse_value(dtype: int, data: bytes, pos: int) -> Tuple[Any, int]:
    if dtype == 0x18:  # bitmap8
        return data[pos], pos + 1
    if dtype == 0x20:  # uint8
        return data[pos], pos + 1
    if dtype == 0x21:  # uint16
        return struct.unpack("<H", data[pos:pos + 2])[0], pos + 2
    if dtype == 0x22:  # uint24
        return data[pos] | (data[pos + 1] << 8) | (data[pos + 2] << 16), pos + 3
    if dtype == 0x23:  # uint32
        return struct.unpack("<I", data[pos:pos + 4])[0], pos + 4
    if dtype == 0x30:  # enum8
        return data[pos], pos + 1
    if dtype == 0x41:  # octet string
        length = data[pos]
        pos += 1
        return data[pos:pos + length], pos + length
    return None, len(data)


def estimate_temperature(program_id: Optional[int], remaining_time_min: Optional[int] = None) -> Tuple[Optional[int], str]:
    if program_id is None:
        return None, "unknown"
    if program_id in PROGRAM_DEFAULT_TEMP:
        return PROGRAM_DEFAULT_TEMP[program_id], "program_default_lookup"
    return None, "no_lookup_entry"


def refresh_temperature_estimate() -> None:
    program_id = state.get("program_id")
    remaining_time = state.get("remaining_time_min")
    temp, source = estimate_temperature(program_id, remaining_time)
    if temp is not None:
        update("temperature_target_estimated", temp)
    update("temperature_estimation_source", source)
    if program_id in PROGRAM_ALLOWED_TEMPS:
        update("temperature_allowed_values", ",".join(str(x) for x in PROGRAM_ALLOWED_TEMPS[program_id]))


def decode_operation_flags(flags: int) -> None:
    update("operation_flags", flags)
    # Confirmed correction: bit 0x08 means drum/motor speed over threshold, not generic running.
    update("motor_speed_above_threshold", "ON" if (flags & 0x08) else "OFF")


def decode_parameter_block(value: bytes) -> None:
    update("parameter_block_hex", hexs(value))
    if len(value) >= 8:
        update("param_1", value[0])
        update("param_2", value[1])
        update("param_3", value[2])
        update("param_4", value[3])
        update("param_5", value[4])

        # Confirmed from logs: B5 is current/phase drum speed in rpm/10.
        spin_stage_raw = value[5]
        update("spin_stage_raw", spin_stage_raw)
        update("current_spin_rpm", spin_stage_raw * 10)

        # Confirmed from logs: B6/B7 are max spin rpm, big endian.
        max_spin_rpm = struct.unpack(">H", value[6:8])[0]
        update("max_spin_rpm", max_spin_rpm)
        update("spin_rpm", max_spin_rpm)  # kept for backward compatibility with older dashboard cards

        # Experimental candidate bits, kept visible but clearly named candidate.
        # Water Plus bit appears to mean: option available for current program,
        # not necessarily currently enabled.
        update("water_plus_available", "ON" if (value[WATER_PLUS_BYTE_INDEX] & WATER_PLUS_BIT) else "OFF")

        # Short option remains unresolved: no reliable state change observed yet.
        update("option_short_candidate", "ON" if (value[SHORT_OPTION_BYTE_INDEX] & SHORT_OPTION_BIT) else "OFF")


def decode_main_attr(attr: int, value: Any) -> None:
    if attr == 0x0000:
        if value not in status_map:
            update("status_code_candidate_ignored", value)
            return

        update("status_code", value)
        update("status", status_map[value])
        update("running", "ON" if value == 5 else "OFF")
        update("fault", "ON" if value == 8 else "OFF")
        if value != 8:
            update("fault_text", "")
        refresh_fault_derived_state()

    elif attr == 0x0001:
        decode_operation_flags(int(value))

    elif attr == 0x0002:
        update("program_phase_raw", value)
        phase_a = value & 0xFF
        update("program_phase_a", phase_a)
        update("program_phase_a_text", phase_a_map.get(phase_a, f"unknown_{phase_a}"))
        phase_b = (value >> 8) & 0xFF
        update("program_phase_b", phase_b)
        update("program_phase_c", (value >> 16) & 0xFF)
        update("program_phase_text", phase_b_map.get(phase_b, f"unknown_{phase_b}"))
        refresh_fault_derived_state()

    elif attr == 0x0100:
        update("start_delay_raw", value)
        update("start_delay_min", miele_time_to_minutes(value))

    elif attr == 0x0102:
        update("remaining_time_raw", value)
        update("remaining_time_min", miele_time_to_minutes(value))
        refresh_temperature_estimate()


def decode_program_attr(attr: int, value: Any) -> None:
    """Decode EP210/FD02 or incoming 0x7DFD program attributes only.

    Safety guard:
    FD01/0x0010 has been observed as 0x1E0501, which is device driver
    version metadata, not a program id. If a future routing/cluster quirk sends
    such a value through this function, do NOT overwrite the real program.
    """
    if attr == 0x0010 and isinstance(value, int):
        if value not in program_map:
            update("program_id_candidate_ignored", f"0x{value:06X}")
            log(f"IGNORED non-program attr 0x0010 value=0x{value:06X}; real program remains {state.get('program')}")
            return

        update("program_id", value)
        update("program_hex", f"0x{value:04X}")
        update("program", program_map[value])
        update("program_last_update", datetime.datetime.now(datetime.timezone.utc).isoformat())
        refresh_temperature_estimate()

    elif attr == 0x0020 and isinstance(value, (bytes, bytearray)):
        decode_parameter_block(bytes(value))


def decode_device_info_attr(attr: int, status: int, value: Any = None) -> None:
    if attr == 0x0020 and status != 0x00:
        # Repeatedly observed as 0x8F in real wash/service/fault states.
        # No confirmed evidence yet that this exposes live B8/S78/S24-lock,
        # heater, valve, actuator, or detailed service EEPROM values.
        update("fd01_0020_status", f"ERR_0x{status:02X}")
    elif attr == 0x0030 and status == 0x00:
        update("temperature_stage_count", value)
    elif attr == 0x0031 and status == 0x00:
        update("spin_stage_count", value)

# ============================================================
# 12 - DECODING: ZCL FRAMES
# ============================================================

def decode_read_response(cluster_id: int, payload: bytes) -> None:
    pos = 3
    while pos + 3 <= len(payload):
        attr = struct.unpack("<H", payload[pos:pos + 2])[0]
        pos += 2
        status = payload[pos]
        pos += 1

        if status != 0x00:
            log(f"READ RESP cluster=0x{cluster_id:04X} attr=0x{attr:04X} status=0x{status:02X}")
            if cluster_id == CLUSTER_DEVICE_INFO:
                decode_device_info_attr(attr, status)
            continue

        if pos >= len(payload):
            break
        dtype = payload[pos]
        pos += 1
        value, pos = parse_value(dtype, payload, pos)
        log(f"READ DECODE cluster=0x{cluster_id:04X} attr=0x{attr:04X} dtype=0x{dtype:02X} value={value if not isinstance(value, (bytes, bytearray)) else hexs(value)}")

        if cluster_id in (CLUSTER_APPLIANCE_CONTROL_DIRECT, CLUSTER_APPLIANCE_CONTROL_ALIAS):
            decode_main_attr(attr, value)
        elif cluster_id in (CLUSTER_PROGRAM_DIRECT, CLUSTER_PROGRAM_ALIAS):
            decode_program_attr(attr, value)
        elif cluster_id == CLUSTER_DEVICE_INFO:
            decode_device_info_attr(attr, status, value)


def decode_report(cluster_id: int, payload: bytes) -> None:
    pos = 3
    while pos + 3 <= len(payload):
        attr = struct.unpack("<H", payload[pos:pos + 2])[0]
        pos += 2
        dtype = payload[pos]
        pos += 1
        value, pos = parse_value(dtype, payload, pos)
        log(f"REPORT cluster=0x{cluster_id:04X} attr=0x{attr:04X} dtype=0x{dtype:02X} value={value if not isinstance(value, (bytes, bytearray)) else hexs(value)}")

        if cluster_id == CLUSTER_APPLIANCE_CONTROL_DIRECT:
            decode_main_attr(attr, value)
        elif cluster_id == CLUSTER_PROGRAM_DIRECT:
            decode_program_attr(attr, value)


def decode_door_event(payload: bytes) -> None:
    update("event_raw_hex", hexs(payload))
    if len(payload) >= 7:
        event_class = payload[3]
        event_code = payload[4]
        event_value = payload[5] | (payload[6] << 8)
        update("event_class", event_class)
        update("event_code", event_code)
        update("event_value", event_value)

        # Observed S24 door events. Keep conservative:
        #   0x0001 = door closed
        #   0x0011 = door open
        # A separate "locked"/"verriegelt" bit has not been confirmed yet.
        if event_code == 0x02 and event_value == 0x0001:
            update("door", "closed")
            update("door_open", "OFF")
        elif event_code == 0x02 and event_value == 0x0011:
            update("door", "open")
            update("door_open", "ON")


def decode_zcl(cluster_id: int, payload: bytes) -> None:
    if len(payload) < 3:
        return
    cmd = payload[2]

    # 0x7D0B is handled as manufacturer-specific event stream.
    if cluster_id == CLUSTER_DOOR_EVENT:
        decode_door_event(payload)
        return

    if cmd == 0x01:
        decode_read_response(cluster_id, payload)
    elif cmd == 0x0A:
        decode_report(cluster_id, payload)
    elif cmd == 0x04:
        msg = f"write_response cluster=0x{cluster_id:04X} payload={hexs(payload)}"
        update("write_result", msg)
        log(msg)
    elif cmd == 0x0B:
        msg = f"default_response cluster=0x{cluster_id:04X} payload={hexs(payload)}"
        update("write_result", msg)
        log(msg)
    else:
        log(f"ZCL cmd=0x{cmd:02X} cluster=0x{cluster_id:04X} payload={hexs(payload)}")


def handle_af_incoming(frame: bytes) -> None:
    payload = frame[5:-1]
    if len(payload) < 16:
        return
    cluster_id = struct.unpack("<H", payload[2:4])[0]
    src_addr = struct.unpack("<H", payload[4:6])[0]
    src_ep = payload[6]
    dst_ep = payload[7]
    data_len = payload[15]
    zcl_payload = payload[16:16 + data_len]
    log(f"IN src=0x{src_addr:04X} src_ep={src_ep} dst_ep={dst_ep} cluster=0x{cluster_id:04X} payload={hexs(zcl_payload)}")
    decode_zcl(cluster_id, zcl_payload)

# ============================================================
# 13 - COMMAND PROCESSOR
# ============================================================

def process_pending_commands(sock: socket.socket) -> None:
    while True:
        try:
            command, payload = command_queue.get_nowait()
        except queue.Empty:
            break

        try:
            if command == "start_delay_min":
                minutes = int(float(str(payload).replace(",", ".")))
                send_start_delay(sock, minutes)
                time.sleep(0.2)
                send_read_attrs(sock, 210, 210, CLUSTER_APPLIANCE_CONTROL_ALIAS, [0x0100, 0x0102, 0x0000, 0x0001, 0x0002])

            elif command == "program":
                program_name = str(payload).strip()
                if program_name not in reverse_program_map:
                    raise ValueError(f"unknown program '{program_name}'. Valid: {', '.join(sorted(reverse_program_map))}")
                send_program(sock, reverse_program_map[program_name])
                time.sleep(0.2)
                send_read_attrs(sock, 210, 210, CLUSTER_PROGRAM_ALIAS, [0x0010, 0x0020])

            elif command == "control":
                action = str(payload).strip().lower()
                if action == "start":
                    # Confirmed practical behaviour: start now = start_delay_min 0.
                    send_start_delay(sock, 0)
                else:
                    send_control_command(sock, action)
                time.sleep(0.5)
                send_read_attrs(sock, 210, 210, CLUSTER_APPLIANCE_CONTROL_ALIAS, [0x0000, 0x0001, 0x0002, 0x0100, 0x0102])

            elif command == "option_short":
                send_option_bit(sock, "short", parse_bool_payload(payload), SHORT_OPTION_BYTE_INDEX, SHORT_OPTION_BIT)
                time.sleep(0.5)
                send_read_attrs(sock, 210, 210, CLUSTER_PROGRAM_ALIAS, [0x0020])

            elif command == "option_water_plus":
                send_option_bit(sock, "water_plus", parse_bool_payload(payload), WATER_PLUS_BYTE_INDEX, WATER_PLUS_BIT)
                time.sleep(0.5)
                send_read_attrs(sock, 210, 210, CLUSTER_PROGRAM_ALIAS, [0x0020])

            elif command == "spin_rpm":
                send_spin_rpm(sock, int(float(str(payload).replace(",", "."))))
                time.sleep(0.5)
                send_read_attrs(sock, 210, 210, CLUSTER_PROGRAM_ALIAS, [0x0020])

        except Exception as exc:
            msg = f"rejected {command}={payload}: {exc}"
            log(msg)
            update("write_result", msg)

# ============================================================
# 14 - RECEIVE LOOP / POLLING
# ============================================================

def poll_confirmed_values(sock: socket.socket) -> None:
    """Poll only confirmed productive areas, not the broad scanner matrix."""
    send_read_attrs(sock, 210, 210, CLUSTER_APPLIANCE_CONTROL_ALIAS, [0x0000, 0x0001, 0x0002])
    time.sleep(0.2)
    send_read_attrs(sock, 210, 210, CLUSTER_APPLIANCE_CONTROL_ALIAS, [0x0100, 0x0102])
    time.sleep(0.2)
    send_read_attrs(sock, 210, 210, CLUSTER_PROGRAM_ALIAS, [0x0010, 0x0020])
    time.sleep(0.2)
    # Diagnostic only: keeps tracking the known 0x8F candidate without relying on it.
    send_read_attrs(sock, 213, 210, CLUSTER_DEVICE_INFO, [0x0020, 0x0030, 0x0031])


def recv_loop(sock: socket.socket) -> None:
    sock.settimeout(1)
    last_timesync = time.time()
    last_poll = 0.0
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
                    buf = buf[idx:]
                if len(buf) < 5:
                    break
                total = 4 + buf[1] + 1
                if len(buf) < total:
                    break
                frame = bytes(buf[:total])
                buf = buf[total:]
                if len(frame) > 5 and frame[2] == 0x44 and frame[3] == 0x81:
                    handle_af_incoming(frame)
        except socket.timeout:
            pass

        process_pending_commands(sock)

        if time.time() - last_poll > POLL_INTERVAL_SEC:
            poll_confirmed_values(sock)
            last_poll = time.time()

        if ENABLE_TIME_SYNC and time.time() - last_timesync > TIME_SYNC_INTERVAL_SEC:
            send_time_sync(sock)
            last_timesync = time.time()

# ============================================================
# 15 - MAIN STARTUP
# ============================================================

def register_endpoints(sock: socket.socket) -> None:
    # Conservative registration: do not over-register FD01/FD00 on EP210.
    af_register(sock, 212, [CLUSTER_TIME], [CLUSTER_TIME])
    af_register(
        sock,
        210,
        [
            CLUSTER_APPLIANCE_CONTROL_ALIAS,
            0x0B02,
            0x0A00,
            CLUSTER_PROGRAM_ALIAS,
            CLUSTER_APPLIANCE_CONTROL_DIRECT,
            CLUSTER_DOOR_EVENT,
            CLUSTER_LEGACY_7D0B_UNSUPPORTED,
            CLUSTER_PROGRAM_DIRECT,
        ],
        [
            CLUSTER_APPLIANCE_CONTROL_ALIAS,
            0x0B02,
            0x0A00,
            CLUSTER_PROGRAM_ALIAS,
            CLUSTER_APPLIANCE_CONTROL_DIRECT,
            CLUSTER_DOOR_EVENT,
            CLUSTER_LEGACY_7D0B_UNSUPPORTED,
            CLUSTER_PROGRAM_DIRECT,
        ],
    )
    af_register(sock, 213, [CLUSTER_DEVICE_INFO], [CLUSTER_DEVICE_INFO])
    af_register(sock, 214, [CLUSTER_CAPABILITY], [CLUSTER_CAPABILITY])


def main() -> None:
    log("Miele XKM3000Z MQTT bridge Phase 4G clean starting")
    log(f"ZNP {HOST}:{PORT} target_nwk=0x{TARGET_NWK:04X} MQTT={MQTT_HOST} base={MQTT_BASE}")

    with socket.create_connection((HOST, PORT), timeout=5) as sock:
        log("CONNECTED TO ZNP")
        register_endpoints(sock)
        time.sleep(1)
        if ENABLE_TIME_SYNC:
            send_time_sync(sock)

        mqtt_publish("bridge", "online")
        publish_ha_discovery()
        start_command_listener()

        if ENABLE_START_DELAY_TEST:
            time.sleep(2)
            send_start_delay(sock, START_DELAY_TEST_MINUTES)

        log("Miele XKM3000Z MQTT bridge läuft.")
        recv_loop(sock)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopping by KeyboardInterrupt")
        mqtt_publish("bridge", "offline")
    except Exception as exc:
        log(f"FATAL: {exc}")
        try:
            mqtt_publish("bridge", "offline")
        except Exception:
            pass
        raise
