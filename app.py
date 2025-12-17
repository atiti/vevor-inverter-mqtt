import time
import json
import logging
import argparse
import paho.mqtt.client as mqtt
from pymodbus.client.serial import ModbusSerialClient
from pymodbus.exceptions import ModbusIOException

# --- Logging Setup ---
logging.basicConfig()
logger = logging.getLogger("vevor-bridge")
logger.setLevel(logging.INFO)

# --- Command Line ---
parser = argparse.ArgumentParser(description="Vevor Inverter → MQTT Bridge for Home Assistant")
parser.add_argument("--modbus-port", default="/dev/ttyUSB1", help="Serial port (default: /dev/ttyUSB0)")
parser.add_argument("--mqtt-host", default="localhost", help="MQTT broker host")
parser.add_argument("--mqtt-port", type=int, default=1883, help="MQTT port (default: 1883)")
parser.add_argument("--mqtt-user", default="", help="MQTT username")
parser.add_argument("--mqtt-pass", default="", help="MQTT password")
parser.add_argument("--poll", type=int, default=30, help="Polling interval in seconds")
args = parser.parse_args()

# --- MQTT Setup ---
mqtt_client = mqtt.Client(protocol=mqtt.MQTTv311)
if args.mqtt_user:
    mqtt_client.username_pw_set(args.mqtt_user, args.mqtt_pass)
try:
    mqtt_client.connect(args.mqtt_host, args.mqtt_port, 60)
    mqtt_client.loop_start()
    logger.info(f"Connected to MQTT at {args.mqtt_host}:{args.mqtt_port}")
except Exception as e:
    logger.error(f"MQTT connection failed: {e}")
    exit(1)

def publish_sensor(name, unit, device_class, state_class, value, unique_id, device_name, icon=None):
    sensor_topic = f"homeassistant/sensor/{unique_id}/state"
    config_topic = f"homeassistant/sensor/{unique_id}/config"
    config_payload = {
        "name": name,
        "state_topic": sensor_topic,
        "unit_of_measurement": unit,
        "device_class": device_class,
        "state_class": state_class,
        "unique_id": unique_id,
        "device": {
            "identifiers": ["vevor_inverter"],
            "name": device_name,
            "manufacturer": "Vevor",
            "model": "3500W Inverter"
        }
    }
    if icon:
        config_payload["icon"] = icon

    mqtt_client.publish(config_topic, json.dumps(config_payload), retain=True)
    mqtt_client.publish(sensor_topic, value, retain=True)

# --- Modbus Client Setup ---
client = ModbusSerialClient(
    port=args.modbus_port,
    baudrate=9600,
    bytesize=8,
    stopbits=1,
    parity="N",
    timeout=1
)

def read_registers():
    try:
        result1 = client.read_holding_registers(address=200, count=35, slave=1)

        if result1.isError():
            logger.warning("Modbus read error in one of the chunks")
            return None

        return result1.registers
    except ModbusIOException as e:
        logger.error(f"Modbus IO error: {e}")
    except Exception as e:
        logger.error(f"Read failed: {e}")
    return None

def map_status(code):
    return {
        0: "Standby",
        1: "Inverter Mode",
        2: "Bypass Mode",
        3: "Charging Mode",
        4: "Fault Mode",
        5: "Line Mode",
        6: "Battery Mode",
        7: "Shutdown",
    }.get(code, f"Unknown Status ({code})")

def map_error(code):
    return {
        0: "No Error",
        1: "Overload",
        2: "Over Temperature",
        3: "Battery Low",
        4: "Fan Failure",
        5: "Grid Fault",
        6: "PV Overvoltage",
        7: "Battery Disconnect",
        8: "Inverter Fault",
    }.get(code, f"Unknown Error ({code})")

def map_operation_mode(mode):
    return {
        0: "Standby",
        1: "Line",
        2: "Battery",
        3: "Charging",
        4: "Fault"
    }.get(mode, f"Unknown Mode ({mode})")

def decode_status_bits(code):
    bits = {
        0: "Inverter On",
        1: "Output Active",
        2: "Charging",
        3: "Discharging",
        4: "Grid Input",
        5: "PV Present",
        6: "Battery Low",
        7: "Battery Full",
        8: "Fault",
        9: "Line Mode",
        10: "Bypass Mode",
        11: "Overload",
        12: "Over Temp",
    }
    return [name for bit, name in bits.items() if code & (1 << bit)]

def main_loop():
    logger.info("Starting Modbus polling loop...")
    while True:
        if not client.connect():
            logger.warning("Modbus device not available.")
            time.sleep(args.poll)
            continue

        regs = read_registers()
        if regs:
            if len(regs) < 34:
                logger.error(f"Unexpected register count: got {len(regs)} registers, need at least 34.")
                time.sleep(args.poll)
                continue

            try:
                OpMode = regs[201 - 200]
                StatusCode = regs[203 - 200]
                ErrorCode  = regs[204 - 200]

                Vin   = regs[205 - 200] / 10.0
                Iin   = regs[206 - 200] / 10.0
                Freq  = regs[207 - 200] / 100.0
                Pin   = regs[208 - 200]
                Pout  = regs[213 - 200]

                Vbat  = regs[215 - 200] / 10.0
                Ibat  = regs[216 - 200]
                Pbat_raw = regs[217 - 200]
                Pbat = Pbat_raw if Pbat_raw < 32768 else Pbat_raw - 65536
                SOC   = regs[229 - 200]

                Vout  = regs[210 - 200] / 10.0
                Iout  = regs[211 - 200] / 10.0
                BattChargeCurrent = regs[214 - 200] / 10.0

                PV_avg_power = regs[223 - 200]
                PV_avg_charge_power = regs[224 - 200]

                LoadPct = regs[225 - 200]
                CHG_T = regs[226 - 200]
                INV_T = regs[227 - 200]
                MPPT_T = regs[228 - 200]

                BattAvgI = regs[232 - 200] / 10.0
                InvAvgI = regs[233 - 200] / 10.0
                PVAvgI  = regs[234 - 200] / 10.0
            
                if Pin >= 65500 or Pin > 5000:
                    Pin = 0

                decoded_status = decode_status_bits(StatusCode)
                logger.info(f"Decoded Status: {decoded_status}")

                publish_sensor("Solar PV Power", "W", "power", "measurement", Pin, "solar_pv_power", "Vevor Inverter")
                publish_sensor("PV Input Voltage", "V", "voltage", "measurement", Vin, "pv_input_voltage", "Vevor Inverter")
                publish_sensor("PV Input Current", "A", "current", "measurement", Iin, "pv_input_current", "Vevor Inverter")
                publish_sensor("PV Avg Power", "W", "power", "measurement", PV_avg_power, "pv_avg_power", "Vevor Inverter")
                publish_sensor("PV Avg Charge Power", "W", "power", "measurement", PV_avg_charge_power, "pv_avg_charge_power", "Vevor Inverter")

                publish_sensor("Inverter Output Power", "W", "power", "measurement", Pout, "inverter_output_power", "Vevor Inverter")
                publish_sensor("AC Output Voltage", "V", "voltage", "measurement", Vout, "ac_output_voltage", "Vevor Inverter")
                publish_sensor("AC Output Current", "A", "current", "measurement", Iout, "ac_output_current", "Vevor Inverter")
                publish_sensor("AC Frequency", "Hz", "frequency", "measurement", Freq, "ac_output_frequency", "Vevor Inverter")
                publish_sensor("Load Percent", "%", None, "measurement", LoadPct, "load_percent", "Vevor Inverter")

                publish_sensor("Battery Voltage", "V", "voltage", "measurement", Vbat, "battery_voltage", "Vevor Inverter")
                publish_sensor("Battery Power", "W", "power", "measurement", Pbat, "battery_power", "Vevor Inverter")
                publish_sensor("Battery SOC", "%", "battery", "measurement", SOC, "battery_soc", "Vevor Inverter")
                publish_sensor("Battery Charging Current", "A", "current", "measurement", BattChargeCurrent, "battery_charging_current", "Vevor Inverter")
                publish_sensor("Battery Avg Current", "A", "current", "measurement", BattAvgI, "battery_avg_current", "Vevor Inverter")
                publish_sensor("Inverter Avg Current", "A", "current", "measurement", InvAvgI, "inverter_avg_current", "Vevor Inverter")
                publish_sensor("PV Charge Avg Current", "A", "current", "measurement", PVAvgI, "pv_charge_avg_current", "Vevor Inverter")

                publish_sensor("Charger Temp", "°C", "temperature", "measurement", CHG_T, "charger_temp", "Vevor Inverter")
                publish_sensor("Inverter Temp", "°C", "temperature", "measurement", INV_T, "inverter_temp", "Vevor Inverter")
                publish_sensor("MPPT Temp", "°C", "temperature", "measurement", MPPT_T, "mppt_temp", "Vevor Inverter")

                publish_sensor("Inverter Status", "", None, "measurement", map_status(StatusCode), "inverter_status", "Vevor Inverter", icon="mdi:information")
                publish_sensor("Inverter Error", "", None, "measurement", map_error(ErrorCode), "inverter_error", "Vevor Inverter", icon="mdi:alert")
                publish_sensor("Operation Mode", "", None, "measurement", map_operation_mode(OpMode), "operation_mode", "Vevor Inverter", icon="mdi:menu")

                logger.info(f"PV={Pin}W | INV={Pout}W | BAT={Vbat:.1f}V {Pbat}W | SOC={SOC}% | STAT={StatusCode} | ERR={ErrorCode}")
            except Exception as e:
                logger.error(f"Data parse error: {e}")

        time.sleep(args.poll)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        client.close()
        mqtt_client.disconnect()
