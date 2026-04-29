import os
import asyncio
from datetime import datetime, timezone
from typing import Any, List, Dict, Optional
from pydantic import BaseModel, Field
from fastapi import FastAPI, HTTPException, Query, Body, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS
import httpx
import logging

#*======================= Setup and Config =====================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="BOSS FARM Sensors API",
    description="Real-time data management API for IESWIC3A multi-sensor devices",
    version="1.0.0",
    openapi_tags=[
        {"name": "Health",     "description": "Service status"},
        {"name": "Devices",    "description": "Device discovery, readings and IP registry"},
        {"name": "Alarms",     "description": "Active alarms, history and acknowledgement"},
        {"name": "Thresholds", "description": "Read and update alert thresholds per device"},
    ],
)

API_KEY = os.getenv("API_KEY", "bossfarm-secret-2024")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(key: str = Security(api_key_header)):
    if key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

INFLUXDB_URL    = os.getenv("INFLUXDB_URL",    "http://influxdb:8086")
INFLUXDB_TOKEN  = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG    = os.getenv("INFLUXDB_ORG",   "myorg")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "esp32_sensors")

client    = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
write_api = client.write_api(write_options=SYNCHRONOUS)
query_api = client.query_api()

class ActiveAlarm(BaseModel):
    device_id:    str
    alert_type:   str
    value:        float
    threshold:    float
    triggered_at: str
    time_elapsed: str

class ActiveAlarmEntry(BaseModel):
    alert_type: str
    value: float
    threshold: float
    triggered_at: str
    time_elapsed: str

class DeviceActiveAlarms(BaseModel):
    device_id: str
    alarms: List[ActiveAlarmEntry]

class BatchThresholdUpdate(BaseModel):
    device_ids: Optional[List[str]] = None
    thresholds: Dict[str, float] = Field(...)
    class Config:
        json_schema_extra = {
            "example": {
                "device_ids": ["IESWIC3A_XX:XX"],
                "thresholds": {
                    "temp_high": 33, "temp_low": 10,
                    "hum_high":  73, "hum_low":  25,
                    "aqi_high": 3, "co2_high": 903, "tvoc_high": 403
                }
            }
        }

class DeviceConfig(BaseModel):
    device_id:  str
    thresholds: Dict[str, float]

device_ip_registry: Dict[str, str] = {}

DEVICE_SUBNET    = os.getenv("DEVICE_SUBNET",    "192.168.0")
DEVICE_SCAN_FROM = int(os.getenv("DEVICE_SCAN_FROM", "1"))
DEVICE_SCAN_TO   = int(os.getenv("DEVICE_SCAN_TO",   "254"))

API_TO_DEVICE_KEYS = {
    "temp_high": "temp",
    "hum_high":  "hum",
    "tvoc_high": "tvoc",
    "co2_high":  "eco2",
    "temp_low":  "temp_low",
    "hum_low":   "hum_low",
}

DEFAULT_THRESHOLDS: Dict[str, float] = {
    "temp_high": 30.0, "temp_low":  5.0,
    "hum_high":  80.0, "hum_low":  20.0,
    "aqi_high":   3.0,
    "co2_high": 1000.0,
    "tvoc_high": 500.0,
}

ALARM_COOLDOWN_SECONDS = 300
ALARM_QUERY_RANGE = os.getenv("ALARM_QUERY_RANGE", "-30d")
_alarm_last_triggered: Dict[tuple, datetime] = {}

#*======================= Helper Functions =====================

async def push_thresholds_to_device(device_id: str, thresholds: Dict[str, float]) -> Optional[str]:
    device_ip = device_ip_registry.get(device_id)
    if not device_ip:
        return f"No IP registered for {device_id}"
    params = {dkey: thresholds[akey] for akey, dkey in API_TO_DEVICE_KEYS.items() if akey in thresholds}
    params["from_api"] = "1"
    if not params:
        return "No mappable threshold keys found"
    async def _do_push():
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                resp = await c.get(f"http://{device_ip}/set_thresh", params=params)
                if resp.status_code == 200:
                    logger.info(f"Thresholds pushed to {device_id} at {device_ip}")
                else:
                    logger.warning(f"Device {device_id} returned HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"Failed to push to {device_id}: {e}")
    asyncio.create_task(_do_push())
    return None


def get_latest_device_ids() -> List[str]:
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -6h)
      |> filter(fn: (r) => r._measurement == "IESWIC3A")
      |> keep(columns: ["device_id", "_time"])
      |> group(columns: ["device_id"])
      |> last(column: "_time")
    '''  	
    try:
        result = query_api.query(query=query, org=INFLUXDB_ORG)
        ids = [r.values.get("device_id") for t in result for r in t.records]
        return list(set(i for i in ids if i))
    except Exception as e:
        logger.error(f"Error fetching device_ids: {e}")
        return []


def get_latest_sensor_reading(device_id: str) -> Optional[Dict]:
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -10m)
      |> filter(fn: (r) => r._measurement == "IESWIC3A" and r.device_id == "{device_id}")
      |> group(columns: ["_field"])
      |> last()
    '''
    try:
        result = query_api.query(query=query, org=INFLUXDB_ORG)
        reading = {r.get_field(): r.get_value() for t in result for r in t.records}
        return reading if reading else None
    except Exception as e:
        logger.error(f"Error fetching sensor reading for {device_id}: {e}")
        return None


def get_device_thresholds(device_id: str) -> Dict[str, float]:
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -30d)
      |> filter(fn: (r) => r._measurement == "device_config" and r.device_id == "{device_id}")
      |> last()
    '''
    try:
        result = query_api.query(query=query, org=INFLUXDB_ORG)
        thresholds = DEFAULT_THRESHOLDS.copy()
        for table in result:
            for record in table.records:
                f, v = record.get_field(), record.get_value()
                if f in thresholds and isinstance(v, (int, float)):
                    thresholds[f] = float(v)
        return thresholds
    except Exception as e:
        logger.warning(f"Using default thresholds for {device_id}: {e}")
        return DEFAULT_THRESHOLDS.copy()


def write_alert_event(device_id: str, alert_type: str, value: float, threshold: float):
    """Write an active (unacknowledged, unresolved) alert event."""
    try:
        ts = int(datetime.utcnow().timestamp())
        point = (
            f"alert_events,device_id={device_id},alert_type={alert_type} "
            f"value={float(value):.4f},threshold={float(threshold):.4f},acknowledged=false,resolved=false {ts}"
        )
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, write_precision="s", record=point)
        logger.info(
            f"Alert written: device_id={device_id}, alert_type={alert_type}, "
            f"value={value}, threshold={threshold}, acknowledged=false, resolved=false"
        )
    except Exception as e:
        logger.error(f"Error while writing alert event: {e}")


def get_latest_alarm_state(device_id: str, alert_type: str) -> Optional[Dict[str, Any]]:
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {ALARM_QUERY_RANGE})
      |> filter(fn: (r) => r._measurement == "alert_events")
      |> filter(fn: (r) => r.device_id == "{device_id}" and r.alert_type == "{alert_type}")
      |> pivot(
           rowKey:      ["_time", "device_id", "alert_type"],
           columnKey:   ["_field"],
           valueColumn: "_value"
         )
      |> sort(columns: ["_time"], desc: true)
      |> limit(n: 1)
    '''
    try:
        result = query_api.query(query=query, org=INFLUXDB_ORG)
        records = [record for table in result for record in table.records]
        if not records:
            return None

        record = records[0]
        return {
            "acknowledged": _parse_acknowledged(record.values.get("acknowledged", False)),
            "resolved": _parse_acknowledged(record.values.get("resolved", False)),
            "value": record.values.get("value"),
            "threshold": record.values.get("threshold"),
            "time": record.get_time(),
        }
    except Exception as e:
        logger.error(f"Error reading latest alarm state for {device_id}/{alert_type}: {e}")
        return None


def resolve_alarm_if_active(device_id: str, alert_type: str):
    latest = get_latest_alarm_state(device_id, alert_type)
    if not latest:
        return

    # only resolve if currently active (unacknowledged and unresolved)
    if latest["acknowledged"] or latest["resolved"]:
        return

    try:
        ts = int(datetime.utcnow().timestamp())
        write_api.write(
            bucket=INFLUXDB_BUCKET,
            org=INFLUXDB_ORG,
            write_precision="s",
            record=f"alert_events,device_id={device_id},alert_type={alert_type} resolved=true,acknowledged=false {ts}",
        )
        logger.info(f"Alarm auto-resolved: {device_id} - {alert_type}")
    except Exception as e:
        logger.error(f"Error auto-resolving alarm {device_id}/{alert_type}: {e}")


def _is_cooldown_active(device_id: str, alert_type: str) -> bool:
    last = _alarm_last_triggered.get((device_id, alert_type))
    return last is not None and (datetime.utcnow() - last).total_seconds() < ALARM_COOLDOWN_SECONDS


def _mark_alarm_triggered(device_id: str, alert_type: str):
    _alarm_last_triggered[(device_id, alert_type)] = datetime.utcnow()


def _parse_acknowledged(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "t", "1", "yes", "y"}:
            return True
        if normalized in {"false", "f", "0", "no", "n", ""}:
            return False
    return False


def _format_elapsed(triggered_at: datetime) -> str:
    if triggered_at.tzinfo is None:
        triggered_at = triggered_at.replace(tzinfo=timezone.utc)
    else:
        triggered_at = triggered_at.astimezone(timezone.utc)

    elapsed_seconds = max(0, int((datetime.now(timezone.utc) - triggered_at).total_seconds()))
    if elapsed_seconds < 3600:
        return f"{elapsed_seconds // 60}m ago"
    if elapsed_seconds < 86400:
        return f"{elapsed_seconds // 3600}h ago"
    return f"{elapsed_seconds // 86400}d ago"


def _fetch_active_alarm_models() -> List[ActiveAlarm]:
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {ALARM_QUERY_RANGE})
      |> filter(fn: (r) => r._measurement == "alert_events")
      |> pivot(
           rowKey:      ["_time", "device_id", "alert_type"],
           columnKey:   ["_field"],
           valueColumn: "_value"
         )
      |> sort(columns: ["_time"], desc: true)
    '''
    result = query_api.query(query=query, org=INFLUXDB_ORG)

    records = [record for table in result for record in table.records]
    records.sort(
        key=lambda record: record.get_time() or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    alarms: List[ActiveAlarm] = []
    seen = set()

    for record in records:
        device_id = record.values.get("device_id", "unknown")
        alert_type = record.values.get("alert_type", "unknown")
        alarm_key = (device_id, alert_type)

        # keep only newest state per (device_id, alert_type)
        if alarm_key in seen:
            continue

        acknowledged = _parse_acknowledged(record.values.get("acknowledged", False))
        resolved = _parse_acknowledged(record.values.get("resolved", False))

        # not active if either acknowledged OR resolved
        if acknowledged or resolved:
            seen.add(alarm_key)
            continue

        triggered_at = record.get_time()
        value = record.values.get("value")
        threshold = record.values.get("threshold")

        if triggered_at is None or value is None or threshold is None:
            # still mark key as consumed, this is latest record for this key
            seen.add(alarm_key)
            continue

        try:
            alarm = ActiveAlarm(
                device_id=device_id,
                alert_type=alert_type,
                value=float(value),
                threshold=float(threshold),
                triggered_at=triggered_at.isoformat(),
                time_elapsed=_format_elapsed(triggered_at),
            )
            alarms.append(alarm)
        except (TypeError, ValueError):
            logger.warning(
                "Skipping malformed active alarm record for %s/%s: value=%r threshold=%r",
                device_id,
                alert_type,
                value,
                threshold,
            )

        seen.add(alarm_key)

    return alarms


def check_thresholds_and_trigger_alarms(device_id: str, reading: Dict):
    temp = reading.get("temperature")
    hum  = reading.get("humidity")

    if temp is not None and hum is not None and temp == 0.0 and hum == 0.0:
        logger.warning(f"[{device_id}] Skipping — temp and hum both 0.0 (sensor offline?)")
        return

    thresholds = get_device_thresholds(device_id)

    def maybe_trigger(alert_type: str, value: float, threshold: float):
        latest = get_latest_alarm_state(device_id, alert_type)

        # already active => do not duplicate
        if latest and (not latest["acknowledged"]) and (not latest["resolved"]):
            return

        # never existed OR resolved OR acknowledged => create fresh active alarm
        write_alert_event(device_id, alert_type, value, threshold)

    # Temperature high/low
    if temp is not None and temp != 0.0:
        if temp > thresholds["temp_high"]:
            maybe_trigger("temp_high", temp, thresholds["temp_high"])
            resolve_alarm_if_active(device_id, "temp_low")
        elif temp < thresholds["temp_low"]:
            maybe_trigger("temp_low", temp, thresholds["temp_low"])
            resolve_alarm_if_active(device_id, "temp_high")
        else:
            resolve_alarm_if_active(device_id, "temp_high")
            resolve_alarm_if_active(device_id, "temp_low")

    # Humidity high/low
    if hum is not None and hum != 0.0:
        if hum > thresholds["hum_high"]:
            maybe_trigger("hum_high", hum, thresholds["hum_high"])
            resolve_alarm_if_active(device_id, "hum_low")
        elif hum < thresholds["hum_low"]:
            maybe_trigger("hum_low", hum, thresholds["hum_low"])
            resolve_alarm_if_active(device_id, "hum_high")
        else:
            resolve_alarm_if_active(device_id, "hum_high")
            resolve_alarm_if_active(device_id, "hum_low")

    # AQI high
    aqi = reading.get("aqi")
    if aqi is not None and aqi != 0:
        if reading.get("air_quality_status") != "Error" and reading.get("aqi_label") != "Unknown":
            if int(aqi) > int(thresholds["aqi_high"]):
                maybe_trigger("aqi_high", float(aqi), thresholds["aqi_high"])
            else:
                resolve_alarm_if_active(device_id, "aqi_high")

    # TVOC high
    tvoc = reading.get("tvoc")
    if tvoc is not None and tvoc > 0:
        if tvoc > thresholds["tvoc_high"]:
            maybe_trigger("tvoc_high", float(tvoc), thresholds["tvoc_high"])
        else:
            resolve_alarm_if_active(device_id, "tvoc_high")

    # CO2 high
    eco2 = reading.get("eco2")
    if eco2 is not None and eco2 > 0:
        if eco2 > thresholds["co2_high"]:
            maybe_trigger("co2_high", float(eco2), thresholds["co2_high"])
        else:
            resolve_alarm_if_active(device_id, "co2_high")


async def discover_devices():
    logger.info(f"Scanning {DEVICE_SUBNET}.{DEVICE_SCAN_FROM}-{DEVICE_SCAN_TO}")
    found = 0
    ips = [f"{DEVICE_SUBNET}.{i}" for i in range(DEVICE_SCAN_FROM, DEVICE_SCAN_TO + 1)]

    async def probe(ip, http_client):
        nonlocal found
        try:
            resp = await http_client.get(f"http://{ip}/device_info")
            if resp.status_code == 200:
                info = resp.json()
                if info.get("device_name", "").startswith("ESP32"):
                    mac = info.get("device_id", "")
                    if mac:
                        did = "IESWIC3A_" + mac[-5:]
                        device_ip_registry[did] = ip
                        found += 1
                        logger.info(f"Discovered {did} at {ip}")
        except Exception:
            pass

    async with httpx.AsyncClient(timeout=3.0) as c:
        for b in range(0, len(ips), 30):
            await asyncio.gather(*(probe(ip, c) for ip in ips[b:b+30]))
    logger.info(f"Discovery done: {found} device(s)")


async def device_discovery_task():
    await asyncio.sleep(15)
    while True:
        try:
            await discover_devices()
        except Exception as e:
            logger.error(f"Discovery error: {e}")
        await asyncio.sleep(300)


async def alarm_processor_task():
    await asyncio.sleep(10)
    while True:
        try:
            device_ids = get_latest_device_ids()
            if device_ids:
                logger.info(f"Checking {len(device_ids)} devices for threshold breaches")
                for device_id in device_ids:
                    reading = get_latest_sensor_reading(device_id)
                    if reading:
                        check_thresholds_and_trigger_alarms(device_id, reading)
            await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"Alarm processor error: {e}")
            await asyncio.sleep(30)


def has_unacknowledged_alarm(device_id: str, alert_type: str) -> bool:
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {ALARM_QUERY_RANGE})
      |> filter(fn: (r) => r._measurement == "alert_events")
      |> filter(fn: (r) => r.device_id == "{device_id}" and r.alert_type == "{alert_type}")
      |> pivot(
           rowKey:      ["_time", "device_id", "alert_type"],
           columnKey:   ["_field"],
           valueColumn: "_value"
         )
      |> sort(columns: ["_time"], desc: true)
      |> limit(n: 1)
    '''
    try:
        result = query_api.query(query=query, org=INFLUXDB_ORG)
        records = [rec for tbl in result for rec in tbl.records]
        if not records:
            return False  # never triggered before
        latest = records[0]
        return _parse_acknowledged(latest.values.get("acknowledged", False)) is False
    except Exception as e:
        logger.error(f"Error checking alarm state for {device_id}/{alert_type}: {e}")
        return False

#*======================= All API Endpoints =======================

#-------------------------------- Startup & Health -------------------------------------------------
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(alarm_processor_task())
    asyncio.create_task(device_discovery_task())
    logger.info("Alarm processor and device discovery tasks started")

@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "ok", "service": "IoT Alarm API", "timestamp": datetime.utcnow().isoformat()}

#-------------------------------------- Devices & Readings ------------------------------------------
@app.get("/api/devices", response_model=List[str], tags=["Devices"])
async def get_devices():
    return get_latest_device_ids()

@app.get("/api/devices/{device_id}/latest", tags=["Devices"])
async def get_device_latest_reading(device_id: str):
    reading = get_latest_sensor_reading(device_id)
    if not reading:
        raise HTTPException(status_code=404, detail=f"No recent data for device {device_id}")
    return {"device_id": device_id, "reading": reading, "timestamp": datetime.utcnow().isoformat()}


#-------------------------------------- Alarms ------------------------------------------------------
@app.get("/api/alarms/active", response_model=List[ActiveAlarm], tags=["Alarms"])
async def get_active_alarms():
    try:
        alarms = _fetch_active_alarm_models()
        logger.info(f"Fetched {len(alarms)} active alarms.")
        return alarms
    except Exception as e:
        logger.error(f"Error fetching active alarms: {e}", exc_info=True)
        return []

@app.get("/api/alarms/active/grouped", response_model=List[DeviceActiveAlarms], tags=["Alarms"])
async def get_active_alarms_grouped():
    alarms = _fetch_active_alarm_models()
    grouped: Dict[str, List[ActiveAlarmEntry]] = {}

    for a in alarms:
        grouped.setdefault(a.device_id, []).append(
            ActiveAlarmEntry(
                alert_type=a.alert_type,
                value=a.value,
                threshold=a.threshold,
                triggered_at=a.triggered_at,
                time_elapsed=a.time_elapsed,
            )
        )

    return [
        DeviceActiveAlarms(device_id=device_id, alarms=entries)
        for device_id, entries in grouped.items()
    ]


@app.get("/api/alarms/history", tags=["Alarms"])
async def get_alarm_history(hours: int = Query(24, ge=1, le=720)):
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -{hours}h)
      |> filter(fn: (r) => r._measurement == "alert_events")
      |> pivot(
           rowKey:      ["_time", "device_id", "alert_type"],
           columnKey:   ["_field"],
           valueColumn: "_value"
         )
      |> sort(columns: ["_time"], desc: true)
    '''
    try:
        result = query_api.query(query=query, org=INFLUXDB_ORG)
        alarms = []
        for table in result:
            for record in table.records:
                acknowledged = _parse_acknowledged(record.values.get("acknowledged", False))
                alarms.append({
                    "device_id":    record.values.get("device_id",  "unknown"),     
                    "alert_type":   record.values.get("alert_type", "unknown"),
                    "triggered_at": record.get_time().isoformat(),
                    "value":        float(record.values["value"])     if record.values.get("value")     is not None else None,
                    "threshold":    float(record.values["threshold"]) if record.values.get("threshold") is not None else None,
                    "acknowledged": acknowledged,
                })
        return alarms
    except Exception as e:
        logger.error(f"Error fetching alarm history: {e}")
        return []


@app.get("/api/devices/{device_id}/history", tags=["Devices"])
async def get_device_history(
    device_id: str,
    hours: int = Query(24, ge=1, le=720)
):
    """Get sensor reading history for a specific device over the last N hours."""
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -{hours}h)
      |> filter(fn: (r) => r._measurement == "IESWIC3A" and r.device_id == "{device_id}")
      |> pivot(
           rowKey:      ["_time", "device_id"],
           columnKey:   ["_field"],
           valueColumn: "_value"
         )
      |> sort(columns: ["_time"], desc: true)
    '''
    try:
        result = query_api.query(query=query, org=INFLUXDB_ORG)
        readings = []
        for table in result:
            for record in table.records:
                readings.append({
                    "timestamp":          record.get_time().isoformat(),
                    "device_id":          record.values.get("device_id",          device_id),
                    "temperature":        record.values.get("temperature"),
                    "humidity":           record.values.get("humidity"),
                    "aqi":                record.values.get("aqi"),
                    "aqi_label":          record.values.get("aqi_label"),
                    "tvoc":               record.values.get("tvoc"),
                    "eco2":               record.values.get("eco2"),
                    "air_quality_status": record.values.get("air_quality_status"),
                    "light_on":           record.values.get("light_on"),
                    "alert_temp":         record.values.get("alert_temp"),
                    "alert_hum":          record.values.get("alert_hum"),
                    "rssi":               record.values.get("rssi"),
                    "firmware":           record.values.get("firmware"),
                })
        return {
            "device_id": device_id,
            "hours":     hours,
            "count":     len(readings),
            "readings":  readings,
        }
    except Exception as e:
        logger.error(f"Error fetching device history for {device_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/devices/{device_id}/history/range", tags=["Devices"])
async def get_device_history_range(
    device_id: str,
    start: str = Query(..., description="Start: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ"),
    end: str   = Query(..., description="End: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ"),
):
    """Get sensor reading history for a specific device between two ISO timestamps."""
    def parse_dt(s: str) -> str:
        s = s.strip()
        formats = [
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot parse datetime: '{s}'. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ"
            )

    start_norm = parse_dt(start)
    end_norm   = parse_dt(end)

    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {start_norm}, stop: {end_norm})
      |> filter(fn: (r) => r._measurement == "IESWIC3A" and r.device_id == "{device_id}")
      |> pivot(
           rowKey:      ["_time", "device_id"],
           columnKey:   ["_field"],
           valueColumn: "_value"
         )
      |> sort(columns: ["_time"], desc: true)
      |> limit(n: 1000)
    '''
    try:
        result = query_api.query(query=query, org=INFLUXDB_ORG)
        readings = []
        for table in result:
            for record in table.records:
                readings.append({
                    "timestamp":          record.get_time().isoformat(),
                    "device_id":          record.values.get("device_id",          device_id),
                    "temperature":        record.values.get("temperature"),
                    "humidity":           record.values.get("humidity"),
                    "aqi":                record.values.get("aqi"),
                    "aqi_label":          record.values.get("aqi_label"),
                    "tvoc":               record.values.get("tvoc"),
                    "eco2":               record.values.get("eco2"),
                    "air_quality_status": record.values.get("air_quality_status"),
                    "light_on":           record.values.get("light_on"),
                    "alert_temp":         record.values.get("alert_temp"),
                    "alert_hum":          record.values.get("alert_hum"),
                    "rssi":               record.values.get("rssi"),
                    "firmware":           record.values.get("firmware"),
                })
        return {
            "device_id": device_id,
            "start":     start_norm,
            "end":       end_norm,
            "count":     len(readings),
            "readings":  readings,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching device history range for {device_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/alarms/{device_id}/{alert_type}/acknowledge", tags=["Alarms"])
async def acknowledge_alarm(device_id: str, alert_type: str):
    try:
        ts = int(datetime.utcnow().timestamp())
        write_api.write(
            bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, write_precision="s",
            record=f"alert_events,device_id={device_id},alert_type={alert_type} acknowledged=true {ts}"
        )
        logger.info(f"Alarm acknowledged: {device_id} - {alert_type}")
        return {"status": "acknowledged", "device_id": device_id, "alert_type": alert_type,
                "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        logger.error(f"Error acknowledging alarm: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/alarms/acknowledge-all", tags=["Alarms"])
async def acknowledge_all_alarms():
    try:
        alarms = _fetch_active_alarm_models()
        if not alarms:
            return {"status": "ok", "acknowledged": 0, "alarms": []}

        ts = int(datetime.utcnow().timestamp())
        records = [
            f"alert_events,device_id={alarm.device_id},alert_type={alarm.alert_type} acknowledged=true {ts}"
            for alarm in alarms
        ]
        write_api.write(
            bucket=INFLUXDB_BUCKET,
            org=INFLUXDB_ORG,
            write_precision="s",
            record=records,
        )
        logger.info("Acknowledged %d active alarms", len(alarms))
        return {
            "status": "ok",
            "acknowledged": len(alarms),
            "alarms": [
                {"device_id": alarm.device_id, "alert_type": alarm.alert_type}
                for alarm in alarms
            ],
        }
    except Exception as e:
        logger.error(f"Error acknowledging all alarms: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

#-------------------------------------- Thresholds ----------------------------------------------------
@app.post("/api/thresholds/batch", tags=["Thresholds"])
async def set_thresholds_batch(update: BatchThresholdUpdate):
    device_ids = update.device_ids if update.device_ids else list(device_ip_registry.keys())
    if not device_ids:
        device_ids = get_latest_device_ids()
    numeric = {k: v for k, v in update.thresholds.items() if isinstance(v, (int, float))}
    results = []
    for device_id in device_ids:
        try:
            ts     = int(datetime.utcnow().timestamp())
            fields = ",".join([f"{k}={v}" for k, v in numeric.items()])
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, write_precision="s",
                            record=f"device_config,device_id={device_id} {fields} {ts}")
            entry = {"device_id": device_id, "status": "updated"}
            if device_id in device_ip_registry:
                err = await push_thresholds_to_device(device_id, numeric)
                entry["device_synced"] = err is None
                if err: entry["device_sync_error"] = err
            else:
                entry["device_synced"] = False
                entry["device_sync_error"] = "No IP registered"
            results.append(entry)
        except Exception as e:
            results.append({"device_id": device_id, "status": "error", "error": str(e)})
    return {"updated": len([r for r in results if r["status"] == "updated"]), "results": results}


@app.get("/api/thresholds/{device_id}", response_model=Dict[str, float], tags=["Thresholds"])
async def get_thresholds(device_id: str):
    return get_device_thresholds(device_id)


@app.post("/api/thresholds/{device_id}", tags=["Thresholds"])
async def set_thresholds(device_id: str, config: DeviceConfig):
    try:
        ts      = int(datetime.utcnow().timestamp())
        numeric = {k: v for k, v in config.thresholds.items() if isinstance(v, (int, float))}
        fields  = ",".join([f"{k}={v}" for k, v in numeric.items()])
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, write_precision="s",
                        record=f"device_config,device_id={device_id} {fields} {ts}")
        push_err = None
        if device_id in device_ip_registry:
            push_err = await push_thresholds_to_device(device_id, numeric)
        logger.info(f"Thresholds updated for {device_id}: {config.thresholds}")
        result = {"status": "updated", "device_id": device_id, "thresholds": config.thresholds}
        if push_err is None and device_id in device_ip_registry:
            result["device_synced"] = True
        elif push_err:
            result["device_synced"] = False
            result["device_sync_error"] = push_err
        return result
    except Exception as e:
        logger.error(f"Error setting thresholds: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/devices/{device_id}/thresholds_from_device", tags=["Devices"])
async def get_thresholds_from_device(device_id: str):
    device_ip = device_ip_registry.get(device_id)
    if not device_ip:
        raise HTTPException(status_code=404, detail=f"No IP registered for {device_id}")
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            resp = await c.get(f"http://{device_ip}/get_thresh")
            if resp.status_code == 200:
                return {"device_id": device_id, "ip": device_ip, "thresholds": resp.json()}
            raise HTTPException(status_code=502, detail=f"Device returned HTTP {resp.status_code}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Cannot reach device at {device_ip}: {e}")


#-------------------------------------- Device IP Registry ------------------------------------------------
@app.get("/api/devices/registry", tags=["Devices"])
async def get_device_registry():
    return device_ip_registry

@app.post("/api/devices/{device_id}/register", tags=["Devices"])
async def register_device_ip(device_id: str, ip: str = Body(..., embed=True)):
    device_ip_registry[device_id] = ip
    logger.info(f"Device IP registered: {device_id} -> {ip}")
    return {"status": "registered", "device_id": device_id, "ip": ip}


#*======================= Run the API =======================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
