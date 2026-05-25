# Miele XKM 3000 Z – Vollständige Protokoll-Dokumentation v3

**Gerät:** Miele WPS 820 Waschmaschine  
**Modul:** XKM 3000 Z (Zigbee, Firmware 00.51)  
**Zigbee NWK:** `0x53xx` | **IEEE:** `0x001dxxxxxxxxxxxx`  
**Profile:** `0xC51E` | **Device:** `0x0052`  
**Koordinator:** SLZB-06U via TCP `192.168.xxx.xxx:xxxx`

---

## 1. Architektur & Grundprinzip

Das XKM 3000 Z verwendet proprietäre Miele-Cluster auf Basis eines Zigbee-ähnlichen Profils (`0xC51E`).

Wichtige Erkenntnisse:

- Viele Daten sind nicht direkt lesbar.
- Das Gerät arbeitet stark event- und reportbasiert.
- Standard-Reads erfolgen über Alias-Cluster (`0x001B`).
- Einige Cluster dienen nur als Capability-Descriptor oder Event-Bus.
- Das Gerät besitzt intern ein deutlich umfangreicheres Diagnose- und Servicemodell als über Zigbee exportiert wird.
- Produktiv nutzbar sind vor allem Status, Programm, Phase, Restzeit, Startzeitvorwahl, Parameterblock, Tür offen/geschlossen und generische Fehlerzustände.

Produktiv nutzbare Daten kommen primär aus:

| Endpoint | Cluster | Funktion |
|---|---|---|
| EP210 | `0x001B` | Appliance Control Alias – Hauptdaten |
| EP210 | `0x7D00` | Laufzeit-Reports unsolicited |
| EP210 | `0xFD02` | Programm- und Parameterdaten |
| EP213 | `0xFD01` | Gerätemetadaten |
| EP214 | `0xFD00` | Capability-Descriptor |

---

## 2. Endpoint- & Cluster-Übersicht

| Endpoint | Hex | Cluster | Funktion |
|---|---|---|---|
| 210 | `0xD2` | `0x001B` | Hauptdaten, READ möglich |
| 210 | `0xD2` | `0x7D00` | Laufzeit-Reports unsolicited |
| 210 | `0xD2` | `0x7DFD` | Programm-Reports unsolicited |
| 210 | `0xD2` | `0x0B02` | Tür-/Event-Bus |
| 210 | `0xD2` | `0xFD02` | Programm-/Parameterblock |
| 212 | `0xD4` | `0x000A` | Zigbee Time Cluster |
| 213 | `0xD5` | `0xFD01` | Gerätemetadaten |
| 214 | `0xD6` | `0xFD00` | Capability-Descriptor |

### Wichtige Korrektur

Frühere Parser interpretierten den Event-Bus fälschlich als `0x7D0B`.

Der korrekte Event-Cluster ist:

```text
0x0B02
```

`0x7D0B` bleibt bei direkten Reads unsupported und ist nicht der bestätigte Tür-/Event-Bus.

---

## 3. Zigbee-Besonderheiten

### `0x001B` → Antworten kommen als `0x7D00`

Reads an EP210/`0x001B` antworten intern als Cluster `0x7D00`.  
Das ist korrektes Geräteverhalten.

### `0x7D00` direkt – nicht lesbar

Direkte Reads auf `0x7D00` liefern:

```text
0x86 UNSUPPORTED_ATTRIBUTE
```

Lesen funktioniert produktiv über den Alias `0x001B`.

### `0x7DFD`

`0x7DFD` liefert vor allem unsolicited Reports für Programm- und Parameterinformationen.  
Direkte Reads sind nicht stabil produktiv nutzbar.

### `0x0B02`

Reiner Event-Bus für:

- Türstatus
- Zustandswechsel
- Power-/Session-Events

Keine lesbaren Attribute.

---

## 4. Lesbare Attribute

### EP210 / Cluster `0x001B`

Antworten kommen intern als `0x7D00`.

| Attribut | Typ | Inhalt |
|---|---|---|
| `0x0000` | enum8 | status_code |
| `0x0001` | uint8 | operation_flags |
| `0x0002` | uint24 | prog_phase_raw |
| `0x0100` | uint16 | Startzeitvorwahl / Maschinenzeitwert |
| `0x0102` | uint16 | Restlaufzeit / Programmdauer |
| `0x0101`, `0x0103` | – | unsupported |

### EP210 / Cluster `0xFD02`

| Attribut | Typ | Inhalt |
|---|---|---|
| `0x0000` | CharStr | leer bei Gerät aus |
| `0x0001` | uint16 | Gerätetyp |
| `0x0010` | uint24 LE | program_id |
| `0x0020` | OctetStr[8] | parameter_block |

### EP212 / Cluster `0x000A`

| Attribut | Typ | Inhalt |
|---|---|---|
| `0x0000` | uint32 | UTCTime |

### TimeSync

Das XKM3000Z kommuniziert aktiv mit dem Zigbee Time Cluster.

TimeSync ist produktiv sinnvoll, weil er vermutlich folgende Bereiche stabilisiert:

- Startzeitvorwahl
- interne Zeitlogik
- konsistente Delay-Berechnung
- Vermeidung veralteter interner Zeitbasis

Empfehlung für die Bridge:

```text
ENABLE_TIME_SYNC = True
```

aber optional schaltbar lassen, damit Tests mit und ohne TimeSync möglich bleiben.

### EP213 / Cluster `0xFD01`

| Attribut | Typ | Inhalt |
|---|---|---|
| `0x0001` | OctetStr[38] | Geräte-String |
| `0x0002` | OctetStr[27] | Reporting-/Capability-Blob |
| `0x0010` | uint24 | Treiber-Version |
| `0x0011` | uint8 | Gerätetyp-Klasse |
| `0x0012` | uint64 | Reporting-Konfiguration |
| `0x0030` | uint8 | Temperaturstufen = 7 |
| `0x0031` | uint8 | Schleuderstufen = 10 |
| `0x0020` | – | dauerhaft `0x8F` |

#### FD01 / 0x0020

Das Attribut existiert, liefert jedoch dauerhaft:

```text
0x8F
```

Beobachtet unter:

- off
- ready
- running
- finished
- error
- Service-Modus

Keine bestätigten Nutzdaten gefunden.

Wahrscheinlich:

- gesperrter Diagnosecontainer
- EEPROM-/Servicebereich
- interne Diagnoseebene

Nicht bestätigt:

- Live-Temperatur
- Heizstatus
- Verriegelungsstatus
- Fehlerhistorie per Zigbee

### EP214 / Cluster `0xFD00`

Capability-Descriptor.

Enthält:

- Action-IDs
- Capability-Listen
- Descriptor-Informationen

Keine direkt lesbaren Action-Attribute.

---

## 5. Statusmodell

### status_code (`0x001B/0x0000`)

| Wert | Bedeutung |
|---|---|
| `1` | off |
| `3` | ready |
| `4` | delay_start_active |
| `5` | running |
| `7` | finished |
| `8` | error / alarm |
| `9` | cancelled |
| `12` | service_test_mode |

### operation_flags (`0x001B/0x0001`)

| Wert | Bedeutung |
|---|---|
| `0` | Gerät komplett aus |
| `23` | Normalbetrieb |
| `31` | Schleudern oder Fehlerzustand |

Bit `0x08` bedeutet:

```text
Motor-/Trommeldrehzahl über Schwellenwert
```

Nicht korrekt wäre:

- running
- heater active
- wash active

Running wird über `status_code == 5` bestimmt.

---

## 6. prog_phase_raw (`0x001B/0x0002`)

Kodierung:

```text
uint24 LE = B[0], B[1], B[2]
```

| Byte | Bedeutung |
|---|---|
| B[0] | Gerätezustand |
| B[1] | Programmphase |
| B[2] | konstant = 1 |

### B[0] – Gerätezustand

| B[0] | Bedeutung |
|---|---|
| `1` | aus |
| `2` | init / aufwachen |
| `3` | normal-an |
| `4` | ausschalten-Übergang |
| `7` | Sonderzustand / Service (`status=12`) |
| `10` (`0x0A`) | Fehler / Alarm |

### B[1] – Programmphase

| B[1] | Phase |
|---|---|
| `0` | standby / aus |
| `4` | waschen |
| `5` | spülen |
| `9` | pumpen |
| `10` | schleudern |
| `11` | fertig / knitterschutz |
| `12` | programm_beendet |

### Fehlerzustände

Fehler werden nicht nur indirekt erkannt.

Korrekte Logik:

```text
status_code = 8
UND
B[0] = 10 (0x0A)
```

→ Fehler / Alarm bestätigt.

Beispiel:

```text
66570
```

Dekodierung:

```text
B[0] = 10 → Fehler
B[1] = 4  → Fehler während Waschen
B[2] = 1
```

Damit ist Wasserzulauffehler korrekt ableitbar über:

- status_code
- Fehlerzustand
- Programmphase

Nicht korrekt wäre die alte Formulierung „nur indirekt über Phase B[1]=4“.  
Richtig ist: Wasserzulauf-Fehlerbild wird über `status=8`, `B[0]=0x0A` und Kontext `B[1]=4` erkannt.

---

## 7. Zeitwerte

### `0x0100`

Kontextabhängig:

| Status | Bedeutung |
|---|---|
| `status=4` | Startzeitvorwahl |
| `status=5` | teils Restzeit / Maschinenzeitwert |
| `status=3` | meist 0 |

Kodierung:

```text
raw = (HH << 8) + MM
minutes = HH * 60 + MM
```

Beispiele:

| Raw | Bedeutung |
|---|---|
| `0x000F` | 15 Minuten |
| `0x010F` | 1 h 15 min |
| `0x0400` | 4 h |
| `0x003B` | 59 min |

### `0x0102`

Restlaufzeit / Programmdauer.

Gleiche Kodierung:

```text
raw = (HH << 8) + MM
```

---

## 8. program_id (`0xFD02/0x0010`)

| ID | Programm |
|---|---|
| `0x7A00` | Express 20 |
| `0x9200` | Quick Power Wash |
| `0x0300` | Pflegeleicht |
| `0x0400` | Feinwäsche |
| `0x0100` | Baumwolle |
| `0x0800` | Wolle |
| `0x0900` | Seide |
| `0x7B00` | Dunkles/Jeans |
| `0x2500` | Outdoor |
| `0x1B00` | Imprägnieren |
| `0x1500` | Pumpen/Schleudern |
| `0x1D00` | Sportwäsche |
| `0x8100` | Daunen |
| `0x5B03` | Maschine reinigen |
| `0x3400` | nur Spülen/Stärken |
| `0x1F00` | Automatic Plus |

Wichtig: Werte wie `0x1E0501` sind keine Programm-ID, sondern Gerätemetadaten / Treiber-Version.  
Bridge sollte unbekannte Programmwerte nicht als `unknown_...` publizieren, sondern ignorieren oder als `program_id_candidate_ignored` ablegen.

---

## 9. parameter_block (`0xFD02/0x0020`)

8-Byte Parameterblock.

| Byte | Bedeutung |
|---|---|
| B[0] | konstant |
| B[1] | konstant |
| B[2] | konstant |
| B[3] | Gewebe-/Programmart |
| B[4] | Options-/Capability-Information |
| B[5] | aktuelle Schleuderstufe |
| B[6+7] | maximale Schleuderdrehzahl |

### B[5] – aktuelle Schleuderstufe

Formel:

```text
rpm = B[5] * 10
```

| Wert | RPM |
|---|---|
| `0x00` | 0 |
| `0x50` | 800 |
| `0x78` | 1200 |
| `0x8C` | 1400 |
| `0xA0` | 1600 |

### B[6+7] – maximale Schleuderdrehzahl

Big Endian.

| Hex | RPM |
|---|---|
| `0x0000` | 0 |
| `0x0320` | 800 |
| `0x0384` | 900 |
| `0x03E8` | 1000 |
| `0x044C` | 1100 |
| `0x04B0` | 1200 |
| `0x0578` | 1400 |
| `0x05DC` | 1500 |
| `0x0640` | 1600 |

### Wasser Plus

Die Bit-Information bedeutet sehr wahrscheinlich:

```text
Wasser Plus für aktuelles Programm verfügbar
```

Nicht:

```text
Wasser Plus derzeit aktiv
```

Empfohlene Bridge-Bezeichnung:

```text
water_plus_available
```

### Kurz

Für „Kurz“ konnte bisher keine belastbare Statusänderung beobachtet werden.

Bleibt daher:

```text
short_option_candidate
```

Nicht umbenennen zu `short_option_available`, solange keine reproduzierbare Statusänderung vorliegt.

---

## 10. Tür- & Eventsystem (`0x0B02`)

Cluster-Specific Event-Frames.

Beobachtete Werte:

| Wert | Bedeutung |
|---|---|
| `0x0001` | Tür geschlossen |
| `0x0011` | Tür geöffnet |

Zusätzlicher Kontext:

| Feld | Bedeutung |
|---|---|
| `0x00` | Gerät aus |
| `0x02` | Gerät an |

Ein separater Verriegelungsstatus wurde bisher nicht gefunden.

### S24

S24 entspricht intern Türkontakt / Türverriegelung.

Über Zigbee sichtbar ist bisher:

- Tür offen
- Tür geschlossen

Nicht sichtbar:

- separat verriegelt
- Verriegelungsmotor
- Verriegelungs-Endlage

---

## 11. Sensoren & Verbraucher

### Sensoren aus Servicemenü

| Kürzel | Bedeutung |
|---|---|
| B8 | NTC-Temperatursensor, misst Wassertemperatur im Laugenbehälter |
| S24 | Türverriegelungsschalter / Türkontakt |
| S78 | Schwimmerschalter Bodenwanne / Aquastop |

### S78 / Aquastop

S78 meldet Wasser in der Bodenwanne und löst Aquastop aus.

Wichtig:

```text
S78 ≠ Flusensieb offen
```

Ein geöffnetes Flusensieb wird nicht automatisch als Sensorzustand erkannt, solange die Bodenwanne / der Schwimmer nicht anspricht.

Ein echter S78-Alarm wäre wahrscheinlich mit einem Aquastop-/Leckagefehler verbunden, z. B. F0220.

### Verbraucher aus Servicemenü

Beobachtete Verbraucher-/Aktortests:

```text
H24P1 + Y40/14
H24P2 + Y40/14 + 40R
H24P3 + Y40/14 + 60
H24P4 + Y40/14 + 80
H24P5 + Y40/14 + 100
Y12/2Y40 + 160
M6
M5W1
M8
M5 + M8
H3-6
```

Bedeutungslogik:

| Kürzel | Bedeutung |
|---|---|
| Yxx | Magnetventile / Wasserwege |
| Mx | Motoren / Pumpen / Antrieb |
| Hx | Heizung |
| R | Relais / Leistungsstufe |
| Bx | Fühler |
| Sx | Schalter / Sensor |

### Zigbee-Erkenntnis zu Sensoren und Verbrauchern

Intern existieren Sensoren und Verbraucher eindeutig.

Über Zigbee wurden bisher nicht direkt gefunden:

- Temperatur-Istwert B8
- Heizstatus
- Ventilstatus
- Verriegelungsstatus S24 locked
- Aquastop-Status S78
- einzelne Verbraucherzustände
- einzelne Aktortest-Rückmeldungen

Das Gerät exportiert nur reduzierte Runtime-Informationen.

---

## 12. Kundendienstmenü

Aktivierung:

```text
Tür zu
→ Gerät aus
→ Start-Taste drücken und gedrückt halten
→ Gerät einschalten
→ Start-Taste loslassen
→ 3× drücken
→ beim dritten Mal gedrückt halten
→ warten bis das Gerät piept
→ loslassen
```

Im Service-Modus sichtbar:

- Fehlerspeicher
- Softwarestände
- Sensorlisten
- Verbrauchertests
- Diagnoseprogramme

### Programmplatz 10

Im Kundendienstmenü wurde Programmplatz 10 beobachtet.

Zigbee-Muster:

```text
status_code = 12
B[0] = 7
B[1] = 0
```

Bedeutung:

```text
service_test_mode / Sonderzustand
```

---

## 13. Fehlerspeicher

Im Servicemenü beobachtete Fehlercodes:

```text
F0010
F0011
F0015
F0047
F0016
F0020
F0092
F4100
```

Die Fehlerhistorie existiert intern sicher.

Über Zigbee wurde bisher jedoch nur exportiert:

```text
status_code = 8
B[0] = 0x0A
```

Keine vollständigen F-Codes.

### Wasserzulauffehler

Zigbee-seitig beobachtbares Muster:

```text
status_code = 8
B[0] = 0x0A
B[1] = 4
0x0102 = 0
```

Bedeutung:

```text
Fehler / Alarm während Waschen
```

Der konkrete Miele-F-Code wird nicht über die bisher lesbaren Zigbee-Attribute übertragen.

### Fehlerspeicher in der Maschine

Der Fehlerspeicher sitzt sehr wahrscheinlich intern in der Maschine, nicht im XKM3000Z als offen lesbarer Zigbee-Speicher.

Mögliche Speicherorte:

- EEPROM / NVM der Maschinensteuerung
- Service-Menü-Diagnosebereich
- gesperrter Diagnosecontainer

`FD01/0x0020 = 0x8F` bleibt der stärkste Kandidat für einen gesperrten Diagnosecontainer, liefert aber bisher keine Nutzdaten.

---

## 14. Softwarestände

| Modul | Version |
|---|---|
| ELP270 | 3299 |
| EW | 3368 |
| LNG | 5598 |
| EZL270 | 2993 |
| ELP270U | 2926 |

Interpretation:

| Modul | Vermutete Funktion |
|---|---|
| ELP | Haupt-/Leistungssteuerung |
| EW | Waschlogik |
| LNG | Sprache / Lokalisierung |
| EZL | Zusatzlogik / Kommunikation |
| ELP270U | Variante / Update-/Untermodul |

---

## 15. Temperatur

Das XKM3000Z liefert:

- keine echte Waschtemperatur
- keinen Temperatur-Istwert
- keinen Heizstatus

Die Temperatur wird sehr wahrscheinlich in der originalen Qivicon-App nur geschätzt über:

- Programmlogik
- Capability-Tabelle
- Defaultwerte

Empfehlung:

```text
temperature_target_estimated
```

explizit als geschätzter Wert kennzeichnen.

### Temperaturstufen

FD01/`0x0030` liefert:

```text
7
```

Das ist ein statischer Capability-Wert, kein Sensorwert.

Wahrscheinliche Stufen:

```text
20 / 30 / 40 / 50 / 60 / 75 / 90 °C
```

---

## 16. Schreibbare Funktionen

### Bestätigt funktionierend: Startzeitvorwahl

Cluster:

```text
0x001B / attr 0x0100
```

Sofortstart aus aktiver Startzeitvorwahl:

```text
0x0000
```

Die alte Qivicon-App nutzte sehr wahrscheinlich diese Methode.

### Experimentell / ohne bestätigte Wirkung

| Funktion | Ergebnis |
|---|---|
| Programmwahl | springt zurück |
| Pause | keine Wirkung |
| Cancel | keine Wirkung |
| Spin-Write | ignoriert |
| parameter_block write | ACK ohne Effekt |
| Kurz | nicht entschlüsselt |
| Wasser Plus setzen | nicht bestätigt, nur Verfügbarkeit erkannt |

Sehr wahrscheinlich limitiert durch:

- Maschinenfirmware
- physikalischen Drehwahlschalter
- fehlende Remote-Freigabe
- alte XKM3000Z-/WPS820-Architektur

---

## 17. Nicht direkt verfügbar über Zigbee

| Feature | Status |
|---|---|
| Echte Waschtemperatur | nicht gefunden |
| Heizungsstatus | nicht gefunden |
| Wasserzulaufstatus | nicht direkt, aber Fehlerzustand ableitbar |
| Ventilstatus | nicht gefunden |
| Tür verriegelt separat | nicht gefunden |
| Aquastop S78 direkt | nicht gefunden |
| Verbraucherzustände | nicht gefunden |
| F-Codes | nicht über Zigbee sichtbar |
| Fehlerhistorie | nur im Servicemenü sichtbar |
| Remote-Programmwahl | nicht funktionsfähig |
| Echte Pause/Resume | nicht funktionsfähig |
| FD01/0x0020 | dauerhaft `0x8F` |

---

## 18. Home Assistant Bridge – empfohlene Sensoren

### Produktiv

```text
sensor.status
sensor.status_code
binary_sensor.running
sensor.program
sensor.program_id
sensor.remaining_time_min
sensor.start_delay_min
sensor.program_phase_raw
sensor.program_phase_a
sensor.program_phase_b
sensor.program_phase_text
binary_sensor.door_open
sensor.current_spin_rpm
sensor.max_spin_rpm
binary_sensor.motor_speed_above_threshold
sensor.water_plus_available
```

### Diagnostisch

```text
sensor.fd01_0020_status
sensor.operation_flags
sensor.parameter_block_hex
sensor.event_raw_hex
sensor.program_id_candidate_ignored
sensor.short_option_candidate
```

### Geschätzt

```text
sensor.temperature_target_estimated
```

### Fehler

```text
binary_sensor.fault
sensor.fault_text
sensor.fault_context
```

Empfohlene Ableitung:

```python
fault_active = status_code == 8 and phase_a == 10
```

Wasserzulauf-Kontext:

```python
if status_code == 8 and phase_a == 10 and phase_b == 4:
    fault_text = "Fehler/Alarm während Waschen – möglicher Wasserzulauf"
```
## 19. Geräteidentifikation (EP213/FD01)

```
0x0001 Geräte-String:  E0001xxxxxxxx  WMF820   095xxxxxx
                └─ Serien-Nr. ──┘ └─ Typ ─┘ └─ Art.Nr ─┘

Firmware-String: 00.51  E0001xxxxxxxx 00  XKM3000Z  09731580
                 └─ FW ─┘ └─ XKM-Seriennummer ────┘  └─ Art.Nr ─┘
0x0012 Reporting-Konfiguration:
  Enthält:
    > min reporting interval
    > max reporting interval
0x0010 Treiber-Version: 0x1E0501 (1.5.30 oder 30.5.1)
0x0011 Gerätetyp:       3 (Waschmaschine)
0x0030 Temperaturstufen: 7 → 20/30/40/50/60/75/90°C (statischer Capability-Wert, KEIN Livetwert)
0x0031 Schleuderstufen: 10 → 0/400/600/800/900/1000/1100/1200/1400/1500/1600 rpm (Statischer Capability-Wert)
0x0020 Antwortet dauerhaft:
  > 0x8F
  > Bedeutung wahrscheinlich:
        - Attribut existiert, aber nicht lesbar/freigegeben Keine nutzbaren Live-Daten gefunden.
```

---

## 20. Technisches Fazit

Die WMF 820 WPS implementiert intern ein deutlich umfangreicheres Diagnose- und Servicemodell als über Zigbee - des XKM 3000 Z - offen exportiert wird.

Die Zigbee-Schnittstelle liefert:

- Runtime-Status
- Programmstatus
- Phasenmodell
- Restzeiten
- Startzeitvorwahl
- Parameterblock
- Tür offen/geschlossen
- generische Fehlerzustände
- Service-Test-Modus-Erkennung

Nicht exportiert werden:

- echte Temperaturwerte
- Heizstatus
- detaillierte Fehlercodes
- Verbraucherzustände
- interne Sensorwerte
- EEPROM-Diagnosedaten
- vollständiger Fehlerspeicher

Die Architektur ist technisch leistungsfähig sowie deutlich leistungsfähiger als viele moderne Cloud-APIs.,
aber bewusst auf reduzierte Remote-Diagnose und begrenzte Steuerbarkeit limitiert.

Die eigentliche Einschränkung liegt nicht im Zigbee-Transport des XKM 3000 Z, sondern:

- in der Maschinenfirmware
- den freigegebenen Schreibfunktionen
- den von Miele bewusst nicht exportierten Parametern
