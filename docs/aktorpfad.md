# Aktorpfad: Lüfter und Heizung über MQTT

Dieses Dokument beschreibt, wie Aktor-Befehle (`FAN_ON`, `FAN_OFF`, `HEAT_ON`, `HEAT_OFF`) vom Backend bis zum geschalteten Verbraucher laufen, welche Probleme dabei aufgetreten sind und was am Pico-Code geändert wurde, damit der Pfad zuverlässig funktioniert. Geschwister-Dokument zur `inbetriebnahme.md`, die den Erst-Inbetriebnahme-Pfad und die umqtt-Patches beschreibt.

## Pfad einer Aktor-Nachricht

1. Browser ruft `POST /api/v1/actuator-command` auf dem Backend auf.
2. Backend (Zig) schreibt einen Eintrag in `actuator_commands` (Postgres).
3. Controller (Python) liest neue Einträge und publiziert auf `actuator01/data` (MQTT).
4. Mosquitto leitet die Nachricht an alle Subscriber weiter, die `actuator01/data` abonniert haben und per ACL Leseberechtigung haben (`sensor01`).
5. Auf dem Pico liest die umqtt-Library die Nachricht aus dem TLS-Socket, sobald `mqtt.check_msg()` aufgerufen wird, und ruft den registrierten Callback `mqtt_callback(topic, msg)` auf.
6. Der Callback parst das JSON, ermittelt den Befehl und ruft `switch_heat()` oder `switch_fan()` auf.
7. Diese Funktionen setzen den jeweiligen GPIO-Pin auf den richtigen Pegel, das Relais schaltet, der Verbraucher reagiert.

Der entscheidende Punkt für die Reaktionszeit liegt zwischen Schritt 4 und Schritt 5. Mosquitto schiebt die Nachricht in den TCP-Socket des Pico, sobald sie ankommt. Auf dem Pico bleibt sie aber so lange im umqtt-internen Puffer (bzw. im Empfangspuffer des Sockets), bis `check_msg()` das nächste Mal läuft. Wird die Funktion selten aufgerufen, bleibt die Nachricht entsprechend lange unverarbeitet.

## Hardware und Relais-Polarität

Die beiden Relais sind unterschiedlich verdrahtet:

| Pin | Funktion | value=1 (HIGH) | value=0 (LOW) |
|-----|----------|----------------|----------------|
| 16  | Heizung (NO-Kontakt)   | Heizung an | Heizung aus |
| 17  | Lüfter (NC-Kontakt)    | Lüfter aus | Lüfter an  |

Pin 16 ist active-HIGH: HIGH energisiert die Relais-Spule, der NO-Kontakt zieht zu, der Heizungs-Stromkreis schließt sich. Pin 17 ist invertiert: der Lüfter hängt am Öffner (NC), also ist der Stromkreis im Ruhezustand der Spule (Pin LOW) bereits geschlossen. Energisieren der Spule (Pin HIGH) trennt den Stromkreis und stoppt den Lüfter.

Diese Zuordnung wurde empirisch festgestellt, indem die `value=` Parameter in der Pin-Initialisierung direkt variiert wurden, mit angeschlossenen Verbrauchern und Sichtkontrolle.

### Der Polaritäts-Bug

Die ursprüngliche Version von `switch_heat` und `switch_fan` ging davon aus, dass beide Relais gleich verdrahtet sind:

```python
def switch_heat(on):
    relais_heat.value(0 if on else 1)   # falsch
def switch_fan(on):
    relais_cool.value(1 if on else 0)   # falsch
```

Beide Funktionen liefen damit invertiert zur Hardware. `switch_heat(True)` setzte den Pin LOW, was die Heizung ausschaltete. `switch_fan(True)` setzte den Pin HIGH, was den Lüfter ausschaltete.

Der Fix dreht beide Branches:

```python
def switch_heat(on):
    relais_heat.value(1 if on else 0)
def switch_fan(on):
    relais_cool.value(0 if on else 1)
```

## Der Haupt-Loop

Die ursprüngliche Version des Haupt-Loops sah strukturell so aus:

```python
while True:
    try:
        mqtt.check_msg()
        temp, hum = read_sensor()
        # publish temp und hum
    except Exception:
        # reconnect
    sleep(PUBLISH_INTERVAL_S)   # 60 Sekunden blockierend
```

Damit wurde `check_msg()` einmal pro Minute aufgerufen. Konsequenz: ein Aktor-Befehl, der fünf Sekunden nach dem letzten Aufruf eintraf, lag fünfundfünfzig Sekunden im Puffer, bevor der Pico ihn verarbeitete. In der Praxis sahen wir genau dieses Muster: ein `FAN_OFF` wurde verschluckt, ein zweites kam an, ein darauffolgendes `FAN_ON` hing über siebzig Sekunden, bis es feuerte.

Dazu kam, dass der MQTT-Client mit `keepalive=0` konfiguriert war. Damit fragt mosquitto den Client nicht regelmäßig per PINGREQ, und der Pico schickt selbst keine PINGREQ-Pakete. Eine TCP-Verbindung, die durch ein NAT-Timeout, einen kurzen WLAN-Aussetzer oder Routing-Probleme stirbt, fällt damit auf Anwendungsebene nicht auf. Der Pico merkt das erst beim nächsten Publish, der in einen toten Socket schreibt und eine Exception wirft. Bei einem 60-Sekunden-Publish-Intervall kann das eine Minute oder länger dauern.

### Der neue Loop

```python
last_publish_ms = ticks_ms() - PUBLISH_INTERVAL_MS
last_ping_ms    = ticks_ms()

while True:
    try:
        mqtt.check_msg()
        now = ticks_ms()
        if ticks_diff(now, last_publish_ms) >= PUBLISH_INTERVAL_MS:
            last_publish_ms = now
            publish_reading(mqtt)
        if ticks_diff(now, last_ping_ms) >= PING_INTERVAL_MS:
            last_ping_ms = now
            mqtt.ping()
    except Exception as e:
        # reconnect
    sleep(LOOP_TICK_S)   # 0.2 Sekunden
```

`check_msg()` läuft jetzt rund fünfmal pro Sekunde. Aktor-Befehle werden dadurch im Schnitt innerhalb von einigen hundert Millisekunden umgesetzt. Sensor-Publish und MQTT-Ping werden über `ticks_diff()` zeitlich gesteuert, der 60-Sekunden-Takt der Messwerte bleibt erhalten.

Zusätzlich wurde der MQTT-Client auf `keepalive=60` umgestellt und schickt alle 30 Sekunden einen PINGREQ. Bricht die Verbindung weg, bemerkt das entweder der Pico beim nächsten `ping()` (Exception im Loop, Reconnect) oder mosquitto innerhalb von 1,5 mal `keepalive`, also nach spätestens 90 Sekunden.

### Warum 0.2 Sekunden Loop-Tick

Der Tick ist ein Kompromiss zwischen Reaktionszeit und CPU-Last. 0,2 Sekunden bedeutet:

- Latenz für Aktor-Befehle: durchschnittlich 100 ms, maximal 200 ms.
- CPU-Belastung: vernachlässigbar, `check_msg()` ist ein nicht-blockierender Read.
- Energieverbrauch: relevant, falls der Pico jemals batteriebetrieben werden sollte. Im Lab am USB-Netzteil unkritisch.

Schneller (zum Beispiel 50 ms) bringt für einen einzelnen menschlich geschalteten Lüfter nichts. Langsamer fängt an, die Reaktionszeit spürbar zu machen.

## Debug-Sichtbarkeit über Thonny

Eine Lektion aus der Debug-Session, die nichts mit dem Code zu tun hat, sondern mit der Methodik: ohne Thonny über USB gibt es keinen Weg, in Echtzeit zu sehen, was auf dem Pico passiert. Das Skript läuft (oder eben nicht), publiziert (oder eben nicht), schaltet Relais (oder eben nicht), und wenn etwas schiefläuft, bleibt nur der Mosquitto-Log auf dem Backend zur Diagnose. Das ist eine Sicht auf die Netzwerk-Seite der Verbindung, nicht auf den Pico selbst.

In der konkreten Debug-Session führte das zu einer reihe von Fehlannahmen: wir haben `FAN_ON` publiziert, gesehen dass die Nachricht beim Broker ankommt, gesehen dass ein parallel laufender Subscriber als `sensor01` die Nachricht bekommt, und daraus geschlossen, dass der Pico sie auch bekommt und verarbeitet. Beide Schritte waren nicht überprüft. Der Pico selbst schweigt im Normalbetrieb, und die einzigen Hinweise auf seine Lebenszeichen waren die Connect-Meldungen in den Broker-Logs.

Sobald Thonny verbunden war, ließen sich die Dinge in der REPL ablesen: WLAN-Verbindung, MQTT-Abo, Callback-Aufrufe, Relais-Schaltvorgänge. Das hat den irreführenden Effekt erzeugt, das System sei "durch Thonny" plötzlich funktionsfähig geworden. In Wirklichkeit war nur Sichtbarkeit hinzugekommen.

### Diagnose-Reihenfolge

Wenn der Aktor-Pfad scheinbar nicht funktioniert, in dieser Reihenfolge prüfen:

1. **Mosquitto-Log auf dem Backend prüfen.** Existiert eine aktive Session von `192.168.50.21` als `sensor01`? Kommando:
   ```
   docker logs $(docker ps -qf name=mosquitto) --tail 50 | grep 192.168.50.21
   ```
   Erwartung: eine `New client connected ... as sensor01` Zeile, ohne unmittelbar darauf folgenden `disconnected`. Ein Reconnect-Sturm (mehrere Verbindungen pro Sekunde) deutet auf einen Crash-Loop im Pico-Skript hin.
2. **Pico-Erreichbarkeit prüfen.** `ping 192.168.50.21` vom Backend. Antwortet der Pico nicht, liegt das Problem auf der WLAN- oder Hardware-Ebene.
3. **Thonny anschließen** und in der REPL prüfen, ob `main.py` startet und durchläuft, oder ob ein Fehler die Schleife beendet hat. Wichtig: Thonny stoppt das laufende Skript, sobald die REPL aktiv wird. Nach dem Anschluss explizit `main.py` per Run-Button oder soft reboot starten.
4. **Aktor-Befehl publizieren** und in der REPL beobachten, ob `MQTT recv:` und der Schalt-Print erscheinen. Beispiel-Kommando vom Backend:
   ```
   mosquitto_pub -h localhost -p 8883 --cafile /pfad/zu/ca.crt --insecure -u controller -P "$PW" -t actuator01/data -m '{"command":"FAN_ON"}'
   ```
5. **Wenn der Callback feuert, aber das Relais nicht klickt:** GPIO-Pegel mit Multimeter prüfen, oder die `value=` Aufrufe in `switch_*` testweise variieren.
6. **Wenn das Relais klickt, aber der Verbraucher nicht reagiert:** physische Verkabelung des Verbrauchers prüfen. In der hier dokumentierten Session war der Lüfter zeitweise schlicht ausgesteckt, was den Eindruck erweckte, das Relais würde nicht schalten. Ein Multimeter an den Lastkontakten klärt das in zehn Sekunden.

## Offene Punkte

- **Cold-Boot-Verhalten.** Es ist nicht abschließend verifiziert, ob `main.py` bei einem Kalt-Start ohne USB-Verbindung tatsächlich automatisch durchläuft. Alle bisherigen Beobachtungen liefen mit Thonny über USB. Ein gezielter Test (Thonny zu, USB ab, Stromversorgung neu, Mosquitto-Log beobachten) steht aus.
- **`pico_test` Reconnect-Schleife.** In den Broker-Logs taucht zeitweise ein Client `pico_test` auf, der mit hoher Frequenz von der Docker-Bridge-IP `172.19.0.1` reconnectet. Das ist nicht der echte Pico (der kommt aus `192.168.50.21`). Vermutlich ein liegen gebliebenes Test-Skript oder ein Container auf dem Backend-Pi. Quelle ist nicht identifiziert.
- **TLS-Verifikation auf Pico-Seite.** Unverändert `CERT_NONE`, siehe `inbetriebnahme.md`. Die Verbindung bleibt verschlüsselt, aber das Broker-Zertifikat wird Pico-seitig nicht gegen die CA verifiziert. Das Problem liegt im mbedTLS-Backend von MicroPython, nicht im Code.
