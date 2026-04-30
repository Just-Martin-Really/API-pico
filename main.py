from umqtt.simple import MQTTClient
from machine import Pin, Timer
from utime import sleep, sleep_ms
import json
import network
import urequests
import binascii
import ssl
import dht

# Hardware Setup
DATA_PIN = 15
led_pin = Pin("LED", Pin.OUT)
data_pin = Pin(DATA_PIN, Pin.IN, Pin.PULL_UP)
relais_heat = Pin(16, Pin.OUT, value = 1)
relais_cool = Pin(17, Pin.OUT, value = 1)   
blink_timer = Timer(-1)

sensor = dht.DHT22(data_pin)

#Network config
WLAN_SSID = "Production"
WLAN_PASSWORT = "Production-01"
API_URL = "https://api.open-meteo.com/v1/forecast?latitude=47.66&longitude=9.48&current_weather=true"
AP_IP = "192.168.50.1"

#MQTT (to be refined)
MQTT_BROKER = "192.168.1.100"  
MQTT_PORT = 8883               
MQTT_CLIENT_ID = "pico-1"
MQTT_USER = "dein_mosquitto_user"
MQTT_PW = "dein_mosquitto_passwort"
MQTT_TOPIC = "labor/klima/pico1"
MQTT_CMD_TOPIC = b"labor/klima/pico1/cmd"


def parse_sensor_data():
    try:
        sensor.measure()
        return sensor.humidity(), sensor.temperature()
    except OSError as e:
        print(e)
        return None, None 

def write_json(value, unit):
    data = {
        "sensor_id" : "1",
        "value" : value,
        "unit" : unit
    }
    return json.dumps(data) 

# normally open configuration
def switch_heat(is_active):
    if(is_active):
        relais_heat.value(0)
        print("Heizelement an")
    else:
        relais_heat.value(1)
        print("Heizelement aus")

# Normally closed configuration
def switch_fan(is_active):
    if(is_active):
        relais_cool.value(1)
        print("Lüfter an")
    else:
        relais_cool.value(0)
        print("Lüfter aus")


def mqtt_callback(topic, msg):
    print(f"\nMessage received: {topic}: {msg}")
    
    if msg == b"HEAT_ON":
        switch_heat(True)
    elif msg == b"HEAT_OFF":
        switch_heat(False)
    elif msg == b"FAN_ON":
        switch_fan(True)
    elif msg == b"FAN_OFF":
        switch_fan(False)
    else:
        pass


def setup_mqtt():
    client = MQTTClient(
        client_id=MQTT_CLIENT_ID,
        server=MQTT_BROKER,
        port=MQTT_PORT,
        user=MQTT_USER,
        password=MQTT_PW,
        ssl=True,
        ssl_params={'server_hostname': MQTT_BROKER}
    )
    client.set_callback(mqtt_callback)
    client.connect()
    client.subscribe(MQTT_CMD_TOPIC)
    print(f"Erfolgreich abonniert: {MQTT_CMD_TOPIC}")
    return client


def connect_to_network():
    network.country('DE')

    mac_bytes = binascii.unhexlify(AP_IP.replace(':', ''))

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(WLAN_SSID, WLAN_PASSWORT)

    print("Verbinde mit WLAN\n", end="")

    sleep(15)
    if wlan.status() != 3:
        raise RuntimeError("WLAN-Verbindung fehlgeschlagen!")
    else:
        print("WLAN verbunden!")


def get_api_weather_data():
    print("\nFrage API ab...")

    try:
        response = urequests.get(API_URL)
        
        if response.status_code == 200:
            weather_data = response.json()
            temperature = weather_data["current_weather"]["temperature"]
            print("INFO: ", temperature, "°C")
  
            if(temperature < 10.0):
                sleep(2)
                blink_timer.init(period=2000, mode=Timer.PERIODIC, callback=lambda t: led_pin.toggle())
            elif(temperature <= 25.0):
                sleep(1)
                blink_timer.init(period=1000, mode=Timer.PERIODIC, callback=lambda t: led_pin.toggle())
            else:
                sleep_ms(300)
                blink_timer.init(period=300, mode=Timer.PERIODIC, callback=lambda t: led_pin.toggle())
        else:
            print("ERROR: ", response.status_code)
            response.close()

    except Exception as e:
        print(e)

if __name__ == "__main__":
    print("Startup...")
    #connect_to_network()

    try:
        mqtt = setup_mqtt()
        while True:
            #get_api_weather_data()
            mqtt.check_msg()

            temp, hum = parse_sensor_data()
            if temp is not None and hum is not None: 
                temp_payload = write_json(temp, "°C")
                hum_payload = write_json(hum, "%")

                print(temp_payload)
                print(hum_payload)

                switch_heat(True)
                sleep(2)
                switch_heat(False)
                sleep(5)
                switch_fan(True)
                sleep(2)
                switch_fan(False)
                
                sleep(2)
            else:
                print("Perform preventive shutdown")

            sleep(600)
    except KeyboardInterrupt:
        print("Skript manuell beendet")
        blink_timer.deinit()
        mqtt.disconnect()
        led_pin.off()
