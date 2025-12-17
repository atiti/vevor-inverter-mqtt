#!/usr/bin/env python3
import time
import json
import logging
import argparse
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

import paho.mqtt.client as mqtt
from pymodbus.client.serial import ModbusSerialClient
from pymodbus.exceptions import ModbusIOException

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("vevor-bridge")

# ----------------------------
# Helpers
# ----------------------------
def s16(x: int) -> int:
    """Convert 16-bit register to signed int."""
    return x if x < 0x8000 else x - 0x10000

def u16(x: int) -> int:
    return x & 0xFFFF

def clamp_bad_power(pin: int) -> int:
    # Some firmwares occasionally return garbage spikes for PV power
    if pin >= 65500 or pin > 10000:
        return 0
    return pin

def bits_to_names(value: int, bit_names: Dict[int, str]) -> List[str]:
    return [name for bit, name in bit_names.items() if value & (1 << bit)]

def flow_bits_list(flow_status: int) -> List[int]:
    return [bit for bit in range(16) if (flow_status & (1 << bit))]

def flow_bits_text(flow_status: int) -> str:
    bits = flow_bits_list(flow_status)
    return ", ".join([f"b{b}" for b in bits])

# ----------------------------
# New: Derived operation mode you can trust
# ----------------------------
def derived_operation_mode(mains_w: int, bat_w: int, pv_w: int, out_w: int) -> str:
    # Your captured transition proves bypass/grid shows up as mains_power > 0
    if mains_w > 50:
        return "BYPASS_GRID"

    # Battery power sign: negative == supplying (discharge), positive == charging
    if bat_w < -30:
        return "INVERTER_BATTERY_DISCHARGE"
    if bat_w > 30:
        return "PV_SURPLUS_CHARGING"

    if pv_w > 30 and out_w > 30:
        return "PV_SUPPLYING_NEAR_BALANCED"

    return "IDLE_OR_UNKNOWN"

# ----------------------------
# Protocol assumptions (validated part)
# ----------------------------
# Block 200.. mapping used here:
# 201 work_mode, 202 mains_v, 203 mains_hz, 204 mains_power,
# 210..213 output, 214 batt charge current, 215..217 battery,
# 219 pv_v, 220 pv_a,
# 223 pv avg power, 224 pv avg charge power,
# 225 load %, 226 chg temp, 227 inv temp, 228 mppt temp,
# 229 SOC,
# 231 flow status,
# 232 batt avg I, 233 inv avg I, 234 pv avg I
#
# "100.." varies by firmware, keep raw visibility.

# UPDATED per your observed transition:
# - WORKMODE=3 while MAINS=0 and BAT negative => inverter supplying load
# - WORKMODE=2 while MAINS>0 and BAT positive => bypass/grid supplying load
WORK_MODE_MAP = {
    0: "Standby",
    1: "Line/AC",
    2: "Bypass/Grid Supply",
    3: "Inverter Supply",
    4: "Fault",
    5: "Shutdown",
    6: "Bypass (alt)",
}

GENERIC_STATUS_BITS = {
    0: "Inverter On",
    1: "Output Active",
    2: "Charging",
    3: "Discharging",
    4: "Grid Present",
    5: "PV Present",
    6: "Battery Low",
    7: "Battery Full",
    8: "Fault",
    9: "Line Mode",
    10: "Bypass Mode",
    11: "Overload",
    12: "Over Temp",
}

# ----------------------------
# MQTT Home Assistant Discovery helpers
# ----------------------------
def ha_device() -> Dict[str, Any]:
    return {
        "identifiers": ["vevor_inverter"],
        "name": "Vevor Inverter",
        "manufacturer": "Vevor",
        "model": "3500W Inverter",
    }

def publish_discovery(mq: mqtt.Client, component: str, unique_id: str, payload: Dict[str, Any]) -> None:
    topic = f"homeassistant/{component}/{unique_id}/config"
    mq.publish(topic, json.dumps(payload), retain=True)

def publish_state(mq: mqtt.Client, component: str, unique_id: str, value: Any) -> None:
    topic = f"homeassistant/{component}/{unique_id}/state"
    mq.publish(topic, value, retain=True)

def publish_sensor(
    mq: mqtt.Client,
    unique_id: str,
    name: str,
    value: Any,
    unit: Optional[str] = None,
    device_class: Optional[str] = None,
    state_class: Optional[str] = "measurement",
    icon: Optional[str] = None,
):
    sensor_cfg = {
        "name": name,
        "state_topic": f"homeassistant/sensor/{unique_id}/state",
        "unique_id": unique_id,
        "device": ha_device(),
    }
    if unit is not None:
        sensor_cfg["unit_of_measurement"] = unit
    if device_class is not None:
        sensor_cfg["device_class"] = device_class
    if state_class is not None:
        sensor_cfg["state_class"] = state_class
    if icon:
        sensor_cfg["icon"] = icon

    publish_discovery(mq, "sensor", unique_id, sensor_cfg)
    publish_state(mq, "sensor", unique_id, value)

def publish_binary_sensor(
    mq: mqtt.Client,
    unique_id: str,
    name: str,
    value_bool: bool,
    device_class: Optional[str] = None,
    icon: Optional[str] = None,
):
    cfg = {
        "name": name,
        "state_topic": f"homeassistant/binary_sensor/{unique_id}/state",
        "unique_id": unique_id,
        "device": ha_device(),
        "payload_on": "ON",
        "payload_off": "OFF",
    }
    if device_class is not None:
        cfg["device_class"] = device_class
    if icon:
        cfg["icon"] = icon

    publish_discovery(mq, "binary_sensor", unique_id, cfg)
    publish_state(mq, "binary_sensor", unique_id, "ON" if value_bool else "OFF")

def publish_text_sensor(
    mq: mqtt.Client,
    unique_id: str,
    name: str,
    value: str,
    icon: Optional[str] = None,
):
    cfg = {
        "name": name,
        "state_topic": f"homeassistant/sensor/{unique_id}/state",
        "unique_id": unique_id,
        "device": ha_device(),
    }
    if icon:
        cfg["icon"] = icon
    publish_discovery(mq, "sensor", unique_id, cfg)
    publish_state(mq, "sensor", unique_id, value)

# ----------------------------
# Modbus read
# ----------------------------
def read_block(client: ModbusSerialClient, base_addr: int, count: int, slave: int) -> Optional[List[int]]:
    try:
        rr = client.read_holding_registers(address=base_addr, count=count, slave=slave)
        if rr is None or rr.isError():
            return None
        return rr.registers
    except ModbusIOException as e:
        logger.error(f"Modbus IO error: {e}")
        return None
    except Exception as e:
        logger.error(f"Modbus read failed: {e}")
        return None

@dataclass
class InverterSnapshot:
    ts: float

    work_mode: int
    work_mode_str: str

    mains_v: float
    mains_hz: float
    mains_p: int

    out_v: float
    out_a: float
    out_hz: float
    out_p: int

    batt_charge_current: float  # A (from reg 214)

    bat_v: float
    bat_a: float
    bat_p: int

    pv_v: float
    pv_a: float
    pv_p: int
    pv_avg_p: int
    pv_avg_charge_p: int

    load_pct: int
    chg_temp: int
    inv_temp: int
    mppt_temp: int

    soc: int
    flow_status: int

    batt_avg_i: float
    inv_avg_i: float
    pv_avg_i: float

    status_word: Optional[int] = None
    warn_word: Optional[int] = None
    fault_word: Optional[int] = None
    decoded_status_bits: Optional[List[str]] = None

    fault_active: bool = False

def parse_snapshot(block200: List[int], block100: Optional[List[int]]) -> InverterSnapshot:
    def r(addr: int) -> int:
        return block200[addr - 200]

    work_mode = u16(r(201))
    work_mode_str = WORK_MODE_MAP.get(work_mode, f"Unknown({work_mode})")

    mains_v = u16(r(202)) / 10.0
    mains_hz = u16(r(203)) / 100.0
    mains_p = s16(r(204))

    out_v = u16(r(210)) / 10.0
    out_a = u16(r(211)) / 10.0
    out_hz = u16(r(212)) / 100.0
    out_p = s16(r(213))

    batt_charge_current = u16(r(214)) / 10.0  # A

    bat_v = u16(r(215)) / 10.0
    bat_a = s16(r(216)) / 10.0
    bat_p = s16(r(217))

    pv_v = u16(r(219)) / 10.0
    pv_a = s16(r(220)) / 10.0

    pv_avg_p = u16(r(223))
    pv_avg_charge_p = u16(r(224))
    pv_p = clamp_bad_power(pv_avg_p)

    load_pct = u16(r(225))

    chg_temp = s16(r(226))
    inv_temp = s16(r(227))
    mppt_temp = s16(r(228))

    soc = u16(r(229))
    flow_status = u16(r(231))

    batt_avg_i = s16(r(232)) / 10.0
    inv_avg_i = s16(r(233)) / 10.0
    pv_avg_i = s16(r(234)) / 10.0

    status_word = warn_word = fault_word = None
    decoded_status_bits = None

    if block100 and len(block100) >= 40:
        def r100(addr: int) -> int:
            return block100[addr - 100]
        status_word = u16(r100(101))  # still a guess; keep raw visibility
        warn_word = u16(r100(102))
        fault_word = u16(r100(103))
        decoded_status_bits = bits_to_names(status_word, GENERIC_STATUS_BITS)

    fault_active = (work_mode_str.lower().startswith("fault"))
    if fault_word is not None and fault_word != 0:
        fault_active = True
    if decoded_status_bits and "Fault" in decoded_status_bits:
        fault_active = True

    return InverterSnapshot(
        ts=time.time(),
        work_mode=work_mode,
        work_mode_str=work_mode_str,
        mains_v=mains_v,
        mains_hz=mains_hz,
        mains_p=mains_p,
        out_v=out_v,
        out_a=out_a,
        out_hz=out_hz,
        out_p=out_p,
        batt_charge_current=batt_charge_current,
        bat_v=bat_v,
        bat_a=bat_a,
        bat_p=bat_p,
        pv_v=pv_v,
        pv_a=pv_a,
        pv_p=pv_p,
        pv_avg_p=pv_avg_p,
        pv_avg_charge_p=pv_avg_charge_p,
        load_pct=load_pct,
        chg_temp=chg_temp,
        inv_temp=inv_temp,
        mppt_temp=mppt_temp,
        soc=soc,
        flow_status=flow_status,
        batt_avg_i=batt_avg_i,
        inv_avg_i=inv_avg_i,
        pv_avg_i=pv_avg_i,
        status_word=status_word,
        warn_word=warn_word,
        fault_word=fault_word,
        decoded_status_bits=decoded_status_bits,
        fault_active=fault_active,
    )

# ----------------------------
# Main
# ----------------------------
def main():
    parser = argparse.ArgumentParser(description="Vevor Inverter → MQTT Bridge (full telemetry + derived operation mode)")
    parser.add_argument("--modbus-port", default="/dev/ttyMoschip", help="Serial port (use your udev symlink)")
    parser.add_argument("--slave", type=int, default=1, help="Modbus slave id (default: 1)")
    parser.add_argument("--mqtt-host", default="localhost", help="MQTT broker host")
    parser.add_argument("--mqtt-port", type=int, default=1883, help="MQTT port")
    parser.add_argument("--mqtt-user", default="", help="MQTT username")
    parser.add_argument("--mqtt-pass", default="", help="MQTT password")
    parser.add_argument("--poll", type=int, default=10, help="Polling interval seconds (default: 10)")
    args = parser.parse_args()

    mq = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        protocol=mqtt.MQTTv311,
    )
    if args.mqtt_user:
        mq.username_pw_set(args.mqtt_user, args.mqtt_pass)

    try:
        mq.connect(args.mqtt_host, args.mqtt_port, 60)
        mq.loop_start()
        logger.info(f"✅ Connected to MQTT at {args.mqtt_host}:{args.mqtt_port}")
    except Exception as e:
        logger.error(f"MQTT connection failed: {e}")
        raise SystemExit(1)

    client = ModbusSerialClient(
        port=args.modbus_port,
        baudrate=9600,
        bytesize=8,
        stopbits=1,
        parity="N",
        timeout=1,
    )

    logger.info("Starting Modbus polling loop...")

    last_flow_status: Optional[int] = None

    while True:
        try:
            if not client.connect():
                logger.warning("Modbus device not available.")
                time.sleep(args.poll)
                continue

            block200 = read_block(client, base_addr=200, count=60, slave=args.slave)
            block100 = read_block(client, base_addr=100, count=40, slave=args.slave)

            if not block200 or len(block200) < 60:
                logger.warning("Failed to read main block 200..259")
                time.sleep(args.poll)
                continue

            snap = parse_snapshot(block200, block100)

            # ---- Core mode/state ----
            publish_sensor(mq, "vevor_work_mode_raw", "Work Mode (raw)", snap.work_mode, icon="mdi:numeric")
            publish_text_sensor(mq, "vevor_work_mode", "Work Mode", snap.work_mode_str, icon="mdi:menu")

            op_mode = derived_operation_mode(snap.mains_p, snap.bat_p, snap.pv_p, snap.out_p)
            publish_text_sensor(mq, "vevor_operation_mode_derived", "Operation Mode (derived)", op_mode, icon="mdi:state-machine")

            # ---- Mains ----
            publish_sensor(mq, "vevor_mains_voltage", "Mains Voltage", snap.mains_v, unit="V", device_class="voltage")
            publish_sensor(mq, "vevor_mains_frequency", "Mains Frequency", snap.mains_hz, unit="Hz", device_class="frequency")
            publish_sensor(mq, "vevor_mains_power", "Mains Power", snap.mains_p, unit="W", device_class="power")

            # ---- AC Output ----
            publish_sensor(mq, "vevor_ac_output_voltage", "AC Output Voltage", snap.out_v, unit="V", device_class="voltage")
            publish_sensor(mq, "vevor_ac_output_current", "AC Output Current", snap.out_a, unit="A", device_class="current")
            publish_sensor(mq, "vevor_ac_output_frequency", "AC Output Frequency", snap.out_hz, unit="Hz", device_class="frequency")
            publish_sensor(mq, "vevor_ac_output_power", "AC Output Power", snap.out_p, unit="W", device_class="power")

            # ---- Battery ----
            publish_sensor(mq, "vevor_battery_voltage", "Battery Voltage", snap.bat_v, unit="V", device_class="voltage")
            publish_sensor(mq, "vevor_battery_current", "Battery Current", snap.bat_a, unit="A", device_class="current")
            publish_sensor(mq, "vevor_battery_power", "Battery Power", snap.bat_p, unit="W", device_class="power")
            publish_sensor(mq, "vevor_battery_soc", "Battery SOC", snap.soc, unit="%", device_class="battery")
            publish_sensor(mq, "vevor_battery_charge_current", "Battery Charge Current (reg214)", snap.batt_charge_current, unit="A", device_class="current", icon="mdi:battery-charging")

            # ---- PV ----
            publish_sensor(mq, "vevor_pv_voltage", "PV Voltage", snap.pv_v, unit="V", device_class="voltage")
            publish_sensor(mq, "vevor_pv_current", "PV Current", snap.pv_a, unit="A", device_class="current")
            publish_sensor(mq, "vevor_pv_power", "PV Power", snap.pv_p, unit="W", device_class="power")
            publish_sensor(mq, "vevor_pv_avg_power", "PV Avg Power", snap.pv_avg_p, unit="W", device_class="power")
            publish_sensor(mq, "vevor_pv_avg_charge_power", "PV Avg Charge Power", snap.pv_avg_charge_p, unit="W", device_class="power")

            # ---- Extra telemetry you asked for ----
            publish_sensor(mq, "vevor_load_percent", "Load Percent", snap.load_pct, unit="%", state_class="measurement", icon="mdi:gauge")
            publish_sensor(mq, "vevor_charger_temp", "Charger Temp", snap.chg_temp, unit="°C", device_class="temperature")
            publish_sensor(mq, "vevor_inverter_temp", "Inverter Temp", snap.inv_temp, unit="°C", device_class="temperature")
            publish_sensor(mq, "vevor_mppt_temp", "MPPT Temp", snap.mppt_temp, unit="°C", device_class="temperature")

            publish_sensor(mq, "vevor_battery_avg_current", "Battery Avg Current", snap.batt_avg_i, unit="A", device_class="current", icon="mdi:current-dc")
            publish_sensor(mq, "vevor_inverter_avg_current", "Inverter Avg Current", snap.inv_avg_i, unit="A", device_class="current", icon="mdi:current-ac")
            publish_sensor(mq, "vevor_pv_avg_current", "PV Avg Current", snap.pv_avg_i, unit="A", device_class="current", icon="mdi:solar-power")

            # ---- Flow status (raw + bits) ----
            publish_sensor(mq, "vevor_flow_status_raw", "Flow Status (raw)", snap.flow_status, icon="mdi:transit-connection-variant")
            publish_text_sensor(mq, "vevor_flow_bits", "Flow Bits", flow_bits_text(snap.flow_status), icon="mdi:format-list-bulleted")

            # Bit 2 toggle (0x0251 -> 0x0255 diff 0x0004)
            flow_bit2 = bool(snap.flow_status & (1 << 2))
            publish_binary_sensor(mq, "vevor_flow_bit2", "Flow Bit 2 (toggle)", flow_bit2, icon="mdi:toggle-switch")

            # ---- Optional raw status words (still “best guess”) ----
            if snap.status_word is not None:
                publish_sensor(mq, "vevor_status_word_raw", "Status Word (raw)", snap.status_word, icon="mdi:code-braces")
                publish_text_sensor(
                    mq,
                    "vevor_status_word_decoded",
                    "Status Word (decoded)",
                    ", ".join(snap.decoded_status_bits or []) if snap.decoded_status_bits else "",
                    icon="mdi:format-list-bulleted",
                )
            if snap.warn_word is not None:
                publish_sensor(mq, "vevor_warn_word_raw", "Warn Word (raw)", snap.warn_word, icon="mdi:alert-circle-outline")
            if snap.fault_word is not None:
                publish_sensor(mq, "vevor_fault_word_raw", "Fault Word (raw)", snap.fault_word, icon="mdi:alert-octagon-outline")

            # ---- Derived binary sensors for automation ----
            publish_binary_sensor(mq, "vevor_grid_importing", "Grid Importing", snap.mains_p > 50, icon="mdi:transmission-tower")
            publish_binary_sensor(mq, "vevor_battery_discharging", "Battery Discharging", snap.bat_p < -30, device_class="battery", icon="mdi:battery-minus")
            publish_binary_sensor(mq, "vevor_battery_charging", "Battery Charging", snap.bat_p > 30, device_class="battery", icon="mdi:battery-plus")
            publish_binary_sensor(mq, "vevor_pv_present", "PV Present", snap.pv_p > 30, icon="mdi:white-balance-sunny")
            publish_binary_sensor(mq, "vevor_fault_active", "Fault Active", snap.fault_active, device_class="problem", icon="mdi:alert")

            # Log transition on flow change (helps map bits)
            if last_flow_status is None:
                last_flow_status = snap.flow_status
            elif snap.flow_status != last_flow_status:
                prev = last_flow_status
                now = snap.flow_status
                diff = prev ^ now
                logger.info(f"FLOW CHANGED: 0x{prev:04X} -> 0x{now:04X} (xor=0x{diff:04X}) bits={flow_bits_text(diff)}")
                last_flow_status = now

            # Summary log
            logger.info(
                f"PV={snap.pv_p}W | MAINS={snap.mains_p}W | OUT={snap.out_p}W | "
                f"BAT={snap.bat_v:.1f}V {snap.bat_a:.1f}A {snap.bat_p}W | SOC={snap.soc}% | "
                f"WORKMODE={snap.work_mode} ({snap.work_mode_str}) | OP={op_mode} | "
                f"LOAD={snap.load_pct}% | Tchg={snap.chg_temp}C Tinv={snap.inv_temp}C Tmppt={snap.mppt_temp}C | "
                f"FLOW=0x{snap.flow_status:04X} | FAULT={snap.fault_active}"
            )

        except Exception as e:
            logger.exception(f"Loop error: {e}")

        time.sleep(args.poll)


if __name__ == "__main__":
    main()

