import os
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Boss Farm Sensors API",
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
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN", "my-super-secret-token")
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

class DeviceConfig(BaseModel):
    device_id: str
    thresholds: Dict[str, float]

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
                if field in thresholds:
                    thresholds[field] = value
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
    logger.info("Alarm processor task started")

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
      |> filter(fn: (r) => r._measurement == "alert_events" and r.acknowledged == false)
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
                
                # Find value and threshold in fields
                value = 0.0
                threshold = 0.0
                field = record.get_field()
                
                if field == "value":
                    value = record.get_value()
                elif field == "threshold":
                    threshold = record.get_value()
                
                # Compute time elapsed
                triggered_at = record.get_time()
                now = datetime.utcnow().replace(tzinfo=None)
                elapsed = now - triggered_at.replace(tzinfo=None)
                hours = int(elapsed.total_seconds() / 3600)
                minutes = int((elapsed.total_seconds() % 3600) / 60)
                
                time_elapsed = f"{hours}h {minutes}m ago" if hours > 0 else f"{minutes}m ago"
                
                alarm = ActiveAlarm(
                    device_id=device_id,
                    alert_type=alert_type,
                    value=value,
                    threshold=threshold,
                    triggered_at=triggered_at.isoformat(),
                    time_elapsed=time_elapsed
                )
                alarms.append(alarm)
        
        return alarms
    except Exception as e:
        logger.error(f"Error fetching active alarms: {e}")
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

@app.get("/api/thresholds/{device_id}", response_model=Dict[str, float])
async def get_thresholds(device_id: str):
    """Get current threshold settings for a device"""
    return get_device_thresholds(device_id)

@app.post("/api/thresholds/{device_id}")
async def set_thresholds(device_id: str, config: DeviceConfig):
    """Set threshold values for a device"""
    try:
        timestamp = int(datetime.utcnow().timestamp())
        fields = ",".join([f"{k}={v}" for k, v in config.thresholds.items()])
        point = f"device_config,device_id={device_id} {fields} {timestamp}"
        
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, write_precision="s", record=point)
        
        logger.info(f"Thresholds updated for {device_id}: {config.thresholds}")
        return {
            "status": "updated",
            "device_id": device_id,
            "thresholds": config.thresholds
        }
    except Exception as e:
        logger.error(f"Error setting thresholds: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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
