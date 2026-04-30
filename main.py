from umqtt.simple import MQTTClient
from machine import Pin, Timer
from utime import sleep
import json
import network
import ssl
import dht

# ── Hardware ──────────────────────────────────────────────────────────────────
DATA_PIN    = 15
led_pin     = Pin("LED", Pin.OUT)
data_pin    = Pin(DATA_PIN, Pin.IN, Pin.PULL_UP)
relais_heat = Pin(16, Pin.OUT, value=1)  # normally open: 1 = off
relais_cool = Pin(17, Pin.OUT, value=1)  # normally closed: 1 = off

sensor = dht.DHT22(data_pin)

# ── Network ───────────────────────────────────────────────────────────────────
WLAN_SSID   = "Production"
WLAN_PASSWD = "Production-01"

# ── MQTT ──────────────────────────────────────────────────────────────────────
# Broker runs on the backend Pi; cert SAN includes backend-server.lab.local
MQTT_BROKER    = "backend-server.lab.local"
MQTT_PORT      = 8883
MQTT_CLIENT_ID = "sensor01"
MQTT_USER      = "sensor01"
MQTT_PW        = "CHANGE_ME"          # paste the value from mqtt_sensor01_password.txt here
MQTT_PUB_TOPIC = "sensor01/data"
MQTT_SUB_TOPIC = b"actuator01/data"

# CA cert must be copied to the Pico at /ca.crt via Thonny before running
CA_CERT_PATH   = "/ca.crt"

PUBLISH_INTERVAL_S = 60


# ── Actuator helpers ──────────────────────────────────────────────────────────
def switch_heat(on):
    relais_heat.value(0 if on else 1)
    print("Heizelement", "an" if on else "aus")

def switch_fan(on):
    relais_cool.value(1 if on else 0)
    print("Lüfter", "an" if on else "aus")


# ── MQTT callback (actuator commands from controller) ─────────────────────────
def mqtt_callback(topic, msg):
    print("MQTT recv:", topic, msg)
    try:
        payload = json.loads(msg.decode())
        cmd = payload.get("command", "")
    except Exception:
        cmd = msg.decode()

    if cmd == "HEAT_ON":
        switch_heat(True)
    elif cmd == "HEAT_OFF":
        switch_heat(False)
    elif cmd == "FAN_ON":
        switch_fan(True)
    elif cmd == "FAN_OFF":
        switch_fan(False)


# ── WLAN ──────────────────────────────────────────────────────────────────────
def connect_to_network():
    network.country("DE")
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(WLAN_SSID, WLAN_PASSWD)

    print("Verbinde mit WLAN", end="")
    for _ in range(20):
        if wlan.status() == 3:
            break
        print(".", end="")
        sleep(1)
    print()

    if wlan.status() != 3:
        raise RuntimeError("WLAN-Verbindung fehlgeschlagen (status={})".format(wlan.status()))

    print("WLAN verbunden:", wlan.ifconfig()[0])


# ── MQTT setup ────────────────────────────────────────────────────────────────
def setup_mqtt():
    client = MQTTClient(
        client_id=MQTT_CLIENT_ID,
        server=MQTT_BROKER,
        port=MQTT_PORT,
        user=MQTT_USER,
        password=MQTT_PW,
        ssl=True,
        ssl_params={
            "server_hostname": MQTT_BROKER,
            "ca_certs": CA_CERT_PATH,
        },
    )
    client.set_callback(mqtt_callback)
    client.connect()
    client.subscribe(MQTT_SUB_TOPIC)
    print("MQTT verbunden, abonniert:", MQTT_SUB_TOPIC)
    return client


# ── Sensor ────────────────────────────────────────────────────────────────────
def read_sensor():
    try:
        sensor.measure()
        return sensor.temperature(), sensor.humidity()
    except OSError as e:
        print("Sensor-Fehler:", e)
        return None, None


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Startup...")
    connect_to_network()

    mqtt = setup_mqtt()

    while True:
        try:
            mqtt.check_msg()

            temp, hum = read_sensor()
            if temp is not None and hum is not None:
                temp_payload = json.dumps({"value": temp, "unit": "C"})
                hum_payload  = json.dumps({"value": hum,  "unit": "%"})

                mqtt.publish(MQTT_PUB_TOPIC, temp_payload)
                print("Published temp:", temp_payload)

                mqtt.publish(MQTT_PUB_TOPIC, hum_payload)
                print("Published hum: ", hum_payload)

                led_pin.toggle()
            else:
                print("Kein Messwert — Notabschaltung")
                switch_heat(False)
                switch_fan(False)

        except Exception as e:
            print("Fehler im Hauptloop:", e)
            try:
                mqtt = setup_mqtt()
            except Exception as re:
                print("Reconnect fehlgeschlagen:", re)

        sleep(PUBLISH_INTERVAL_S)
