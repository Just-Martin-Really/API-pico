import json
import network
import urequests
import binascii
from machine import Pin, Timer
from utime import sleep, sleep_ms

pin = Pin("LED", Pin.OUT)
blink_timer = Timer(-1)


WLAN_SSID = "Production"
WLAN_PASSWORT = "Production-01"
API_URL = "https://api.open-meteo.com/v1/forecast?latitude=47.66&longitude=9.48&current_weather=true"
AP_MAC = "88:a2:9e:46:eb:3a"


def connect_to_network():
    network.country('DE')

    mac_bytes = binascii.unhexlify(AP_MAC.replace(':', ''))

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
                blink_timer.init(period=2000, mode=Timer.PERIODIC, callback=lambda t: pin.toggle())
            elif(temperature <= 25.0):
                sleep(1)
                blink_timer.init(period=1000, mode=Timer.PERIODIC, callback=lambda t: pin.toggle())
            else:
                sleep_ms(300)
                blink_timer.init(period=300, mode=Timer.PERIODIC, callback=lambda t: pin.toggle())
        else:
            print("ERROR: ", response.status_code)
            response.close()

    except Exception as e:
        print(e)

if __name__ == "__main__":
    print("Startup...")
    connect_to_network()

    try:
        while True:
            get_api_weather_data()
            sleep(600)
    except KeyboardInterrupt:
        print("Skript manuell beendet")
        blink_timer.deinit()
        pin.off()
