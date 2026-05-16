# Sensoranfrage: READ_NOW über MQTT

Dieses Dokument beschreibt, wie das Backend den Pico aktiv auffordern kann, sofort einen Messwert zu publizieren, anstatt auf den nächsten 60-Sekunden-Takt zu warten. Der Pfad ist das Pendant zu `aktorpfad.md`: derselbe MQTT-Mechanismus, aber gerichtet auf den Sensor statt auf einen Aktor. Anwendungsfall ist eine Notabschaltungs-Logik im Backend, die bei ausbleibenden Messwerten zuerst einen READ_NOW schickt und erst dann, wenn auch der nicht beantwortet wird, Heizung und Lüfter abschaltet.

## Pfad einer READ_NOW-Anfrage

1. Aufrufer (Dashboard, Watchdog, Skript) ruft `POST /api/v1/sensor-request` auf dem Backend auf, mit `{"sensor_id":"sensor01","command":"READ_NOW"}`.
2. Backend (Zig) schreibt einen Eintrag in `sensor_requests` (Postgres).
3. Controller (Python) liest neue Einträge alle zwei Sekunden und publiziert `{"command":"READ_NOW"}` auf `sensor01/request` (MQTT, QoS 1).
4. Mosquitto leitet die Nachricht an alle Subscriber weiter, die `sensor01/request` abonniert haben und per ACL Leseberechtigung haben (`sensor01`).
5. Auf dem Pico liest die umqtt-Library die Nachricht aus dem TLS-Socket, sobald `mqtt.check_msg()` aufgerufen wird, und ruft den registrierten Callback `mqtt_callback(topic, msg)` auf.
6. Der Callback erkennt `READ_NOW` und setzt das Modul-Flag `read_now_requested = True`. Der Callback selbst publiziert nichts.
7. Der Haupt-Loop sieht beim nächsten Durchlauf, dass das Flag gesetzt ist, ruft `publish_reading(mqtt)` auf und setzt das Flag zurück.
8. `publish_reading` liest den DHT22 und publiziert Temperatur und Luftfeuchtigkeit auf `sensor01/data`, also auf demselben Topic wie die regulären 60-Sekunden-Werte.
9. Der weitere Pfad ist identisch zum normalen Sensorpfad: Controller liest die Werte, prüft Bereich und Frische, leitet sie an den Webserver weiter, der schreibt nach `sensor_data`.

Der zweite Schritt mit dem Flag (statt direkt im Callback zu publizieren) ist bewusst gewählt. Der Callback läuft synchron innerhalb von `check_msg()`, also mitten im Lese-Pfad des MQTT-Sockets. Direkt aus dem Callback heraus zu publizieren würde bedeuten, die MQTT-Library zu betreten, während sie noch im Empfangs-Modus ist. umqtt.simple kommt damit zwar in den meisten Fällen klar, aber das Risiko von Re-Entry-Problemen oder von durch Lese-Operationen abgeschnittenen Schreib-Operationen ist real genug, dass es sich nicht lohnt. Das Flag entkoppelt Empfang und Antwort sauber.

## Änderungen am Pico-Code

### Neue Konstante und neues Flag

```python
MQTT_REQUEST_TOPIC = b"sensor01/request"

# Set by mqtt_callback when a READ_NOW command arrives; the main loop drains it
# so we don't call publish from inside check_msg's callback path.
read_now_requested = False
```

`read_now_requested` ist absichtlich ein Modul-Global. Der Callback ist eine freie Funktion ohne Zugriff auf den `mqtt`-Client und ohne Möglichkeit, einen Rückgabewert in die Schleife zu reichen. Ein einzelnes Bool genügt: zwei dicht aufeinanderfolgende READ_NOW-Befehle führen zu einem einzigen Publish, was für den Anwendungsfall (Notabschaltungs-Logik fragt einmal nach) genau richtig ist.

### Erweiterung des Callbacks

```python
def mqtt_callback(topic, msg):
    global read_now_requested
    ...
    elif cmd == "READ_NOW":
        read_now_requested = True
```

Die bestehenden Aktor-Befehle bleiben unverändert. Der `READ_NOW`-Zweig setzt nur das Flag und kehrt sofort zurück.

### Zweites Abonnement im Setup

```python
client.subscribe(MQTT_SUB_TOPIC)        # actuator01/data
client.subscribe(MQTT_REQUEST_TOPIC)    # sensor01/request
```

Beide Subscriptions laufen über denselben Callback, der Callback unterscheidet über die `command`-Werte im Payload. Der `topic`-Parameter wird im Callback nur geloggt, nicht zur Dispatch-Entscheidung verwendet. Damit bleibt die Logik symmetrisch zu den Aktor-Befehlen.

### Flag-Auswertung im Haupt-Loop

```python
while True:
    try:
        mqtt.check_msg()
        now = ticks_ms()

        if read_now_requested:
            read_now_requested = False
            publish_reading(mqtt)

        if ticks_diff(now, last_publish_ms) >= PUBLISH_INTERVAL_MS:
            last_publish_ms = now
            publish_reading(mqtt)
        ...
```

Reihenfolge ist wichtig: erst Flag prüfen, dann den periodischen 60-Sekunden-Block. Würden beide Bedingungen im selben Tick zutreffen (READ_NOW kommt genau zum Zeitpunkt des regulären Publishs), publiziert der Pico zweimal. Das ist akzeptiert: jeder Messwert ist unabhängig, das Backend speichert beide Zeilen. Der reguläre Takt wird durch READ_NOW nicht zurückgesetzt, das wäre eine zusätzliche Komplexität ohne Mehrwert.

## Erwartete Latenz

Im typischen Betrieb sieht das Backend einen neuen `sensor_data`-Datensatz innerhalb von 3 bis 4 Sekunden nach dem POST:

| Etappe | Typische Dauer |
|--------|----------------|
| Backend-Insert + Antwort | unter 50 ms |
| Controller-Poll-Intervall | bis zu 2 s (Mittel 1 s) |
| MQTT-Roundtrip Controller → Broker → Pico | unter 100 ms |
| Pico-Loop-Tick bis Flag-Auswertung | bis zu 200 ms (Mittel 100 ms) |
| DHT22-Lesung | ca. 50 ms |
| MQTT-Publish Pico → Broker → Controller | unter 100 ms |
| Webserver-Insert | unter 50 ms |

Worst case (Anfrage kommt unmittelbar nach Controller-Tick + Loop-Tick) liegt bei 4 bis 5 Sekunden. Wer striktere Latenz-Anforderungen hat, muss am Controller-Poll-Intervall drehen, nicht am Pico.

## ACL und Topic-Berechtigungen

`docker/mosquitto/acl` wurde im Backend-Repo um zwei Zeilen erweitert: `sensor01` darf `sensor01/request` lesen, `controller` darf es schreiben. Sensor-Daten und Aktor-Topics bleiben unverändert. Vollständige Beschreibung des Backend-Pfades unter `docs/backend/sensor-request-flow.md` im Repository `API-Rpi16GB`.

## Test vom Backend aus

```sh
# 1. Manuell publizieren wie der Controller es täte
docker compose exec mosquitto mosquitto_pub \
  -h localhost -p 8883 \
  --cafile /mosquitto/ssl/ca.crt --insecure \
  -u controller -P "$PW" \
  -t sensor01/request -m '{"command":"READ_NOW"}'

# 2. Über die API
curl -sk -X POST https://backend-server.local/api/v1/sensor-request \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"sensor_id":"sensor01","command":"READ_NOW"}'
```

Erwartet im Pico-REPL (sofern Thonny verbunden ist):

```
MQTT recv: b'sensor01/request' b'{"command":"READ_NOW"}'
Published temp: {"value": 22.7, "unit": "C"}
Published hum:  {"value": 41, "unit": "%"}
```

## Offene Punkte

* **Backend-Watchdog ist noch nicht implementiert.** Der hier dokumentierte Pfad stellt nur das Primitiv bereit. Eine zyklische Prüfung "letzter Messwert älter als 90 s → READ_NOW → Frist abwarten → ggf. HEAT_OFF/FAN_OFF" lebt im Backend (Zig oder Webserver) und steht aus.
* **Verhalten bei wiederholten READ_NOW.** Das Flag ist single-bit. Kommen während eines laufenden `publish_reading`-Aufrufs weitere READ_NOWs an, werden sie zu einem einzigen zusätzlichen Publish zusammengefasst. Für eine Notabschaltung ist das richtig. Für eine zukünftige Burst-Sampling-Funktion müsste das Flag durch einen Zähler ersetzt werden.
* **Cold-Boot-Verhalten.** Wie in `aktorpfad.md` notiert, ist nicht abschließend verifiziert, dass `main.py` ohne USB-Stromversorgung automatisch durchläuft. Das gilt für den Sensoranfragen-Pfad genauso wie für den Aktorpfad.
