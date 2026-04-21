import os
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from pydantic import BaseModel, Field
from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS
import httpx
import logging


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="BOSS FARM Sensors API",
    description="Real-time alarm management for IESWIC3A multi-sensor devices",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# InfluxDB Configuration
INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://influxdb:8086")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "myorg")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "esp32_sensors")

client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
write_api = client.write_api(write_options=SYNCHRONOUS)
query_api = client.query_api()

# Pydantic Models
class ActiveAlarm(BaseModel):
    device_id: str
    alert_type: str
    value: float
    threshold: float
    triggered_at: str
    time_elapsed: str

class AlarmHistory(BaseModel):
    device_id: str
    alert_type: str
    value: float
    threshold: float
    triggered_at: str
    acknowledged: bool
    acknowledged_at: Optional[str] = None

class BatchThresholdUpdate(BaseModel):
    device_ids: Optional[List[str]] = None  # If empty/null, applies to all registered devices
    thresholds: Dict[str, float] = Field(
        ...,
        description="Thresholds to set, e.g. {'temp_high': 33, 'hum_high': 73, 'tvoc_high': 403, 'co2_high': 903}"
    )

    class Config:
        schema_extra = {
            "example": {
                "device_ids": [
                    "IESWIC3A_XX:XX",
                    "IESWIC3A_YY:YY"
                ],
                "thresholds": {
                    "temp_high": 33,
                    "hum_high": 73,
                    "tvoc_high": 403,
                    "co2_high": 903
                }
            }
        }

class DeviceConfig(BaseModel):
    device_id: str
    thresholds: Dict[str, float]

# Device IP Registry: maps device_id -> LAN IP
# Auto-populated by periodic discovery scan, or manually via /api/devices/{id}/register
device_ip_registry: Dict[str, str] = {}

# Subnet to scan for device discovery (matches LOCAL_MQTT_SERVER in config.h)
DEVICE_SUBNET = os.getenv("DEVICE_SUBNET", "192.168.0")
DEVICE_SCAN_FROM = int(os.getenv("DEVICE_SCAN_FROM", "1"))
DEVICE_SCAN_TO = int(os.getenv("DEVICE_SCAN_TO", "254"))

# Mapping from API threshold keys to device query param names
API_TO_DEVICE_KEYS = {
    "temp_high": "temp",
    "hum_high": "hum",
    "tvoc_high": "tvoc",
    "co2_high": "eco2",
}

# Default Thresholds
DEFAULT_THRESHOLDS = {
    "temp_high": 30.0,
    "temp_low": 5.0,
    "hum_high": 80.0,
    "hum_low": 20.0,
    "aqi_high": 3,
    "co2_high": 1000,
    "tvoc_high": 500,
}

# ===== HELPER FUNCTIONS =====

async def push_thresholds_to_device(device_id: str, thresholds: Dict[str, float]) -> Optional[str]:
    device_ip = device_ip_registry.get(device_id)
    if not device_ip:
        return f"No IP registered for {device_id}"
    
    # Map API keys to device query params
    params = {}
    for api_key, device_key in API_TO_DEVICE_KEYS.items():
        if api_key in thresholds:
            params[device_key] = thresholds[api_key]
    
    if not params:
        return "No mappable threshold keys found"
    
    url = f"http://{device_ip}/set_thresh"
    
    # Fire-and-forget: the device's /set_thresh handler calls back to the API,
    # which makes a synchronous round-trip. We launch the request as a background
    # task so we don't block the API response or cause deadlocks.
    async def _do_push():
        try:
            async with httpx.AsyncClient(timeout=15.0) as http_client:
                resp = await http_client.get(url, params=params)
                if resp.status_code == 200:
                    logger.info(f"Thresholds pushed to {device_id} at {device_ip}")
                else:
                    logger.warning(f"Device {device_id} returned HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"Failed to push thresholds to {device_id} at {device_ip}: {e}")
    
    asyncio.create_task(_do_push())
    return None  # Optimistically report success

def get_latest_device_ids() -> List[str]:
    """Fetch all unique device_ids from IESWIC3A measurement"""
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -1h)
      |> filter(fn: (r) => r._measurement == "IESWIC3A")
      |> keep(columns: ["device_id", "_time"])
      |> group(columns: ["device_id"])
      |> last(column: "_time")
    '''
    try:
        result = query_api.query(query=query, org=INFLUXDB_ORG)
        device_ids = []
        for table in result:
            for record in table.records:
                device_id = record.values.get("device_id")
                if device_id:
                    device_ids.append(device_id)
        return list(set(device_ids))
    except Exception as e:
        logger.error(f"Error fetching device_ids: {e}")
        return []

def get_latest_sensor_reading(device_id: str) -> Optional[Dict]:
    """Get the most recent sensor reading for a device"""
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -5m)
      |> filter(fn: (r) => r._measurement == "IESWIC3A" and r.device_id == "{device_id}")
      |> group(columns: ["_field"])
      |> last()
    '''
    try:
        result = query_api.query(query=query, org=INFLUXDB_ORG)
        reading = {}
        for table in result:
            for record in table.records:
                field = record.get_field()
                value = record.get_value()
                reading[field] = value
        return reading if reading else None
    except Exception as e:
        logger.error(f"Error fetching sensor reading for {device_id}: {e}")
        return None

def get_device_thresholds(device_id: str) -> Dict[str, float]:
    """Fetch device thresholds from InfluxDB device_config measurement"""
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
                field = record.get_field()
                value = record.get_value()
                # Filter out non-numeric fields (booleans, strings) to avoid aggregation errors
                if field in thresholds and isinstance(value, (int, float)):
                    try:
                        thresholds[field] = float(value)
                    except (ValueError, TypeError):
                        logger.warning(f"Skipping non-numeric threshold {field}={value}")
        return thresholds
    except Exception as e:
        logger.warning(f"Using default thresholds for {device_id}: {e}")
        return DEFAULT_THRESHOLDS.copy()

def write_alert_event(device_id: str, alert_type: str, value: float, threshold: float):
    """Write an alert event to InfluxDB"""
    try:
        timestamp = int(datetime.utcnow().timestamp())
        point = (
            f"alert_events,device_id={device_id},alert_type={alert_type} "
            f"value={value},threshold={threshold},acknowledged=false {timestamp}"        
        )
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, write_precision="s", record=point)
        logger.info(f"Alert written: {device_id} - {alert_type} (value={value}, threshold={threshold})")
    except Exception as e:
        logger.error(f"Error writing alert: {e}")

def check_thresholds_and_trigger_alarms(device_id: str, reading: Dict):
    """Compare sensor reading against thresholds and trigger alarms if breached"""
    thresholds = get_device_thresholds(device_id)
    
    # Temperature checks
    if "temperature" in reading:
        temp = reading["temperature"]
        if temp > thresholds["temp_high"]:
            write_alert_event(device_id, "temp_high", temp, thresholds["temp_high"])
        elif temp < thresholds["temp_low"]:
            write_alert_event(device_id, "temp_low", temp, thresholds["temp_low"])
    
    # Humidity checks
    if "humidity" in reading:
        hum = reading["humidity"]
        if hum > thresholds["hum_high"]:
            write_alert_event(device_id, "hum_high", hum, thresholds["hum_high"])
        elif hum < thresholds["hum_low"]:
            write_alert_event(device_id, "hum_low", hum, thresholds["hum_low"])
    
    # AQI check
    if "aqi" in reading:
        aqi = reading["aqi"]
        if int(aqi) > int(thresholds["aqi_high"]):
            write_alert_event(device_id, "aqi_high", float(aqi), thresholds["aqi_high"])
    
    # CO2 check
    if "eco2" in reading:
        co2 = reading["eco2"]
        if int(co2) > int(thresholds["co2_high"]):
            write_alert_event(device_id, "co2_high", float(co2), thresholds["co2_high"])
    
    # TVOC check
    if "tvoc" in reading:
        tvoc = reading["tvoc"]
        if int(tvoc) > int(thresholds["tvoc_high"]):
            write_alert_event(device_id, "tvoc_high", float(tvoc), thresholds["tvoc_high"])

# ===== BACKGROUND ALARM PROCESSOR =====

async def discover_devices():
    """Scan the local subnet for ESP32 devices and register their IPs"""
    logger.info(f"Starting device discovery scan on {DEVICE_SUBNET}.{DEVICE_SCAN_FROM}-{DEVICE_SCAN_TO}")
    found = 0
    ips = [f"{DEVICE_SUBNET}.{i}" for i in range(DEVICE_SCAN_FROM, DEVICE_SCAN_TO + 1)]
    
    async def probe(ip: str, http_client: httpx.AsyncClient):
        nonlocal found
        try:
            resp = await http_client.get(f"http://{ip}/device_info")
            if resp.status_code == 200:
                info = resp.json()
                device_name = info.get("device_name", "")
                if device_name.startswith("ESP32"):
                    mac = info.get("device_id", "")
                    if mac:
                        device_id = "IESWIC3A_" + mac[-5:]
                        device_ip_registry[device_id] = ip
                        found += 1
                        logger.info(f"Discovered {device_id} at {ip}")
        except Exception:
            pass

    async with httpx.AsyncClient(timeout=3.0) as http_client:
        # Scan in parallel batches of 30
        batch_size = 30
        for b in range(0, len(ips), batch_size):
            batch = ips[b:b + batch_size]
            await asyncio.gather(*(probe(ip, http_client) for ip in batch))
    logger.info(f"Device discovery complete: found {found} device(s)")

async def device_discovery_task():
    """Background task that discovers devices on startup and every 5 minutes"""
    await asyncio.sleep(15)  # Wait for network to be ready
    while True:
        try:
            await discover_devices()
        except Exception as e:
            logger.error(f"Error in device discovery: {e}")
        await asyncio.sleep(300)  # Re-scan every 5 minutes

async def alarm_processor_task():
    """Background task that checks thresholds every 30s"""
    await asyncio.sleep(10)  # Wait for InfluxDB to be ready
    
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
            logger.error(f"Error in alarm processor: {e}")
            await asyncio.sleep(30)

@app.on_event("startup")
async def startup_event():
    """Start background tasks on app startup"""
    asyncio.create_task(alarm_processor_task())
    asyncio.create_task(device_discovery_task())
    logger.info("Alarm processor and device discovery tasks started")

# ===== API ENDPOINTS =====

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "ok", "service": "IoT Alarm API", "timestamp": datetime.utcnow().isoformat()}

@app.get("/api/devices", response_model=List[str])
async def get_devices():
    """Get list of all active device IDs"""
    devices = get_latest_device_ids()
    return devices

@app.get("/api/alarms/active", response_model=List[ActiveAlarm])
async def get_active_alarms():
    """Get all currently active (unacknowledged) alarms"""
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -24h)
      |> filter(fn: (r) => r._measurement == "alert_events")
      |> pivot(rowKey: ["_time", "device_id", "alert_type"], columnKey: ["_field"], valueColumn: "_value")
      |> filter(fn: (r) => r.acknowledged != "true")
      |> group(columns: ["device_id", "alert_type"])
      |> last()
      |> sort(columns: ["_time"], desc: true)
    '''
    try:
        result = query_api.query(query=query, org=INFLUXDB_ORG)
        alarms = []
        
        for table in result:
            for record in table.records:
                device_id = record.tags.get("device_id", "unknown")
                alert_type = record.tags.get("alert_type", "unknown")
                triggered_at = record.get_time()
                
                # After pivot, fields become columns in record.values
                value = record.values.get("value")
                threshold = record.values.get("threshold")
                
                if value is None or threshold is None:
                    logger.warning(f"Missing value or threshold for {device_id}-{alert_type}")
                    continue
                
                # Compute time elapsed
                now = datetime.utcnow().replace(tzinfo=None)
                elapsed = now - triggered_at.replace(tzinfo=None)
                hours = int(elapsed.total_seconds() / 3600)
                minutes = int((elapsed.total_seconds() % 3600) / 60)
                time_elapsed = f"{hours}h {minutes}m ago" if hours > 0 else f"{minutes}m ago"
                
                alarm = ActiveAlarm(
                    device_id=device_id,
                    alert_type=alert_type,
                    value=float(value),
                    threshold=float(threshold),
                    triggered_at=triggered_at.isoformat(),
                    time_elapsed=time_elapsed
                )
                alarms.append(alarm)
        
        logger.info(f"Returning {len(alarms)} active alarms")
        return alarms
    except Exception as e:
        logger.error(f"Error fetching active alarms: {e}", exc_info=True)
        return []

        
@app.get("/api/alarms/history")
async def get_alarm_history(hours: int = Query(24, ge=1, le=720)):
    """Get alarm history for the last N hours"""
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -{hours}h)
      |> filter(fn: (r) => r._measurement == "alert_events")
      |> sort(columns: ["_time"], desc: true)
    '''
    try:
        result = query_api.query(query=query, org=INFLUXDB_ORG)
        alarms = []
        
        for table in result:
            for record in table.records:
                device_id = record.tags.get("device_id", "unknown")
                alert_type = record.tags.get("alert_type", "unknown")
                triggered_at = record.get_time()
                
                field = record.get_field()
                value = record.get_value()
                
                alarm_entry = {
                    "device_id": device_id,
                    "alert_type": alert_type,
                    "triggered_at": triggered_at.isoformat(),
                    "field": field,
                    "value": value,
                }
                alarms.append(alarm_entry)
        
        return alarms
    except Exception as e:
        logger.error(f"Error fetching alarm history: {e}")
        return []

@app.post("/api/alarms/{device_id}/{alert_type}/acknowledge")
async def acknowledge_alarm(device_id: str, alert_type: str):
    """Mark an alarm as acknowledged"""
    try:
        timestamp = int(datetime.utcnow().timestamp())
        point = (
            f"alert_events,device_id={device_id},alert_type={alert_type} "
            f"acknowledged=true {timestamp}"
        )
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, write_precision="s", record=point)
        
        logger.info(f"Alarm acknowledged: {device_id} - {alert_type}")
        return {
            "status": "acknowledged",
            "device_id": device_id,
            "alert_type": alert_type,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Error acknowledging alarm: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/thresholds/batch")
async def set_thresholds_batch(update: BatchThresholdUpdate):
    """Set the same thresholds for multiple devices. If device_ids is empty/null, applies to all registered devices.
    Replaces existing devices IDs and set the new thresholds. """
    device_ids = update.device_ids if update.device_ids else list(device_ip_registry.keys())
    if not device_ids:
        # Fall back to devices known from InfluxDB
        device_ids = get_latest_device_ids()
    
    numeric_thresholds = {k: v for k, v in update.thresholds.items()
                         if isinstance(v, (int, float))}
    results = []
    for device_id in device_ids:
        try:
            timestamp = int(datetime.utcnow().timestamp())
            fields = ",".join([f"{k}={v}" for k, v in numeric_thresholds.items()])
            point = f"device_config,device_id={device_id} {fields} {timestamp}"
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, write_precision="s", record=point)
            
            entry = {"device_id": device_id, "status": "updated"}
            if device_id in device_ip_registry:
                device_error = await push_thresholds_to_device(device_id, numeric_thresholds)
                entry["device_synced"] = device_error is None
                if device_error:
                    entry["device_sync_error"] = device_error
            else:
                entry["device_synced"] = False
                entry["device_sync_error"] = "No IP registered"
            results.append(entry)
        except Exception as e:
            results.append({"device_id": device_id, "status": "error", "error": str(e)})

    return {"updated": len([r for r in results if r["status"] == "updated"]), "results": results}

@app.get("/api/thresholds/{device_id}", response_model=Dict[str, float])
async def get_thresholds(device_id: str):
    """Get current threshold settings for a device"""
    return get_device_thresholds(device_id)

@app.post("/api/thresholds/{device_id}")
async def set_thresholds(device_id: str, config: DeviceConfig):
    """Set threshold values for a device. Also pushes to the device via HTTP if its IP is known."""
    try:
        timestamp = int(datetime.utcnow().timestamp())
        # Only include numeric threshold fields to avoid boolean type conflicts
        numeric_thresholds = {k: v for k, v in config.thresholds.items() 
                             if isinstance(v, (int, float))}
        fields = ",".join([f"{k}={v}" for k, v in numeric_thresholds.items()])
        point = f"device_config,device_id={device_id} {fields} {timestamp}"
        
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, write_precision="s", record=point)
        
        # Push to device if IP is known in the registry
        device_push_result = None
        if device_id in device_ip_registry:
            device_push_result = await push_thresholds_to_device(device_id, numeric_thresholds)
        
        logger.info(f"Thresholds updated for {device_id}: {config.thresholds}")
        result = {
            "status": "updated",
            "device_id": device_id,
            "thresholds": config.thresholds
        }
        if device_push_result is None and device_id in device_ip_registry:
            result["device_synced"] = True
        elif device_push_result:
            result["device_synced"] = False
            result["device_sync_error"] = device_push_result
        return result
    except Exception as e:
        logger.error(f"Error setting thresholds: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/devices/{device_id}/register")
async def register_device_ip(device_id: str, ip: str = Body(..., embed=True)):
    """Register a device's LAN IP so the API can push threshold updates to it"""
    device_ip_registry[device_id] = ip
    logger.info(f"Device IP registered: {device_id} -> {ip}")
    return {"status": "registered", "device_id": device_id, "ip": ip}

@app.get("/api/devices/registry")
async def get_device_registry():
    """Get all registered device IPs"""
    return device_ip_registry

@app.get("/api/devices/{device_id}/thresholds_from_device")
async def get_thresholds_from_device(device_id: str):
    """Fetch current thresholds directly from the device via HTTP"""
    device_ip = device_ip_registry.get(device_id)
    if not device_ip:
        raise HTTPException(status_code=404, detail=f"No IP registered for {device_id}")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://{device_ip}/get_thresh")
            if resp.status_code == 200:
                return {"device_id": device_id, "ip": device_ip, "thresholds": resp.json()}
            else:
                raise HTTPException(status_code=502, detail=f"Device returned HTTP {resp.status_code}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Cannot reach device at {device_ip}: {e}")

@app.get("/api/devices/{device_id}/latest")
async def get_device_latest_reading(device_id: str):
    """Get latest sensor reading for a specific device"""
    reading = get_latest_sensor_reading(device_id)
    if not reading:
        raise HTTPException(status_code=404, detail=f"No recent data for device {device_id}")
    return {
        "device_id": device_id,
        "reading": reading,
        "timestamp": datetime.utcnow().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)




