# Meine Änderungen — Pico

## Was ich gemacht habe und warum

Ich habe zwei Dinge geändert: den MQTT-Broker-Hostnamen in `main.py` und die komplette `umqtt/simple.py`-Bibliothek. Letzteres war der aufwändigere Teil.

---

## main.py: Broker-IP statt Hostname

**Vorher:**
```python
MQTT_BROKER = "backend-server.lab.local"
```

**Nachher:**
```python
MQTT_BROKER   = "192.168.50.92"
MQTT_TLS_HOST = "backend-server.lab.local"
```

Der Hostname `backend-server.lab.local` konnte vom Pico nie aufgelöst werden. Die Ursache ist nicht die DNS-Konfiguration (die ist korrekt), sondern MicroPython selbst: lwIP behandelt alle Namen, die auf `.local` enden, als mDNS-Multicast-Anfragen (RFC 6762). Die Pakete gehen an `224.0.0.251:5353` statt an den konfigurierten DNS-Server — unabhängig davon, was per DHCP als DNS-Server mitgegeben wird.

Die IP direkt zu verwenden umgeht das Problem. Der Hostname muss aber als `server_hostname` im TLS-Handshake erhalten bleiben, weil das Broker-Zertifikat auf `backend-server.lab.local` ausgestellt ist und der Pico sonst den Hostnamen nicht verifizieren kann (SNI).

---

## lib/umqtt/simple.py: Mehrere Bugs behoben

Die Bibliothek war manuell installiert und enthielt mindestens fünf Bugs, von denen einige dazu geführt haben, dass mosquitto die Verbindung ohne klare Fehlermeldung auf Pico-Seite abgebrochen hat. Die Diagnose lief fast ausschließlich über die mosquitto-Logs auf dem Broker.

### Bug 1: `import struct` fehlte

`struct` wird in `_send_str()` und `publish()` verwendet, war aber nicht importiert. Fiel erst auf, als die TLS-Verbindung das erste Mal durchkam.

### Bug 2: `ussl` statt `ssl`

Der Code importierte `ussl` innerhalb von `connect()` — ein veralteter MicroPython-Modulname, der in aktuellen Versionen nicht mehr existiert. Ersetzt durch das standardmäßige `ssl`.

### Bug 3: `ssl.wrap_socket(**ssl_params)` funktioniert nicht mit `ca_certs`

MicroPython's `ssl.wrap_socket` akzeptiert den Parameter `ca_certs` nicht direkt. Stattdessen muss ein `ssl.SSLContext` aufgebaut werden:

```python
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.verify_mode = ssl.CERT_NONE
self.sock = ctx.wrap_socket(self.sock, server_hostname=...)
```

`CERT_NONE` bedeutet, dass das Broker-Zertifikat nicht gegen die CA verifiziert wird. Das ist ein Kompromiss: `load_verify_locations()` — die Methode, die das CA-Zertifikat lädt — schlägt bei mbedTLS (dem TLS-Backend von MicroPython) mit „invalid cert" fehl, obwohl die Zertifikatsdatei korrekt ist. Die Verbindung ist trotzdem TLS-verschlüsselt, nur ohne Verifikation der Broker-Identität auf Pico-Seite.

### Bug 4: Off-by-one im MQTT CONNECT Fixed Header

```python
# Vorher (falsch):
self.sock.write(premsg[:i+2])

# Nachher (richtig):
self.sock.write(premsg[:i+1])
```

`premsg` enthält den Fixed Header des CONNECT-Pakets: ein Byte für den Pakettyp (`0x10`) und ein oder mehrere Bytes für die Remaining Length. `premsg[:i+2]` schickt ein Byte zu viel — das erste Null-Byte des Puffers landet vor dem Variable Header.

Mosquitto liest die ersten zwei Bytes des Variable Headers als Protocol Name Length. Mit dem zusätzlichen `0x00`-Byte davor liest es `0x00 0x00` statt `0x00 0x04` — und bricht mit „CONNECT with incorrect protocol string length (0)" ab.

### Bug 5: Falsche Indizes im Variable Header

```python
# Vorher (falsch):
msg[6] = clean_session << 1   # überschreibt Protocol Level (0x04)
msg[6] |= 0xC0                # korrupiert Protocol Level weiter
msg[7] |= self.keepalive >> 8
msg[8] |= self.keepalive & 0xFF

# Nachher (richtig):
msg[7] = clean_session << 1   # Connect Flags
msg[7] |= 0xC0
msg[8] |= self.keepalive >> 8
msg[9] |= self.keepalive & 0xFF
```

`msg` ist ein 10-Byte-Puffer mit folgendem Layout:

| Index | Inhalt |
|-------|--------|
| 0–1 | Protocol Name Length (`0x00 0x04`) |
| 2–5 | „MQTT" |
| 6 | Protocol Level (`0x04` = MQTT 3.1.1) |
| 7 | Connect Flags |
| 8–9 | Keep Alive |

Der Code hat die Connect Flags auf Index 6 geschrieben und damit den Protocol Level überschrieben. Mit gesetzten Username/Password-Flags stand dort `0xC2` statt `0x04` — mosquitto hat das als unbekanntes Protokoll gewertet und mit „bad AUTH method" abgebrochen.

### Bug 6: `subscribe()` hat Remaining Length nie gesetzt

```python
# Vorher:
pkt = bytearray(b"\x82\x00\x00\x00")
struct.pack_into("!H", pkt, 2, self.pid)
self.sock.write(pkt)  # schickt Remaining Length = 0

# Nachher:
remaining = 2 + 2 + len(topic) + 1
pkt = bytearray([0x82, remaining, self.pid >> 8, self.pid & 0xFF])
self.sock.write(pkt)
```

Das zweite Byte des Fixed Headers ist die Remaining Length — also wie viele Bytes danach noch zum Paket gehören. Sie stand immer auf `0x00`. Der Broker hätte das SUBSCRIBE-Paket als leer interpretiert und den nachfolgenden Topic-String als nächstes Paket geparst, was zu einem Protokollfehler geführt hätte. Das ist uns nicht aufgefallen, weil wir den CONNECT-Bug zuerst fixen mussten — subscribe wird erst danach aufgerufen.
