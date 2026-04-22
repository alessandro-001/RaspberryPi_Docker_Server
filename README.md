# BOSS FARM — ESP32 IoT Monitoring Stack

![Mosquitto](https://img.shields.io/badge/Mosquitto-MQTT_Broker-ff6600?style=flat-square)
![InfluxDB](https://img.shields.io/badge/InfluxDB-2.7-22ADF6?style=flat-square)
![Telegraf](https://img.shields.io/badge/Telegraf-1.30-green?style=flat-square)
![Grafana](https://img.shields.io/badge/Grafana-Dashboard-orange?style=flat-square)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.11-blue?style=flat-square)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square)

Real-time environmental monitoring platform for IESWIC3A sensor nodes (ESP32-C3). Devices publish temperature, humidity, AQI, TVOC and eCO2 readings over MQTT. The stack stores, visualises and raises alarms on threshold breaches — all running on a Raspberry Pi.

---

## Architecture

```
IESWIC3A Sensor Nodes (ESP32-C3)
        │
        │  MQTT  (topic: IESWIC3A/#)
        ▼
  ┌─────────────┐
  │  Mosquitto  │  MQTT Broker  :1883
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐
  │   Telegraf  │  Parses JSON payloads → writes to InfluxDB
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐        ┌─────────────┐
  │   InfluxDB  │◄───────│   FastAPI   │  :8000  Alarms · Thresholds · Devices
  └──────┬──────┘        └─────────────┘
         │
         ▼
  ┌─────────────┐
  │   Grafana   │  :3000  Real-time dashboards
  └─────────────┘
```

---

## Services

| Service | Image | Port | Purpose |
|---|---|---|---|
| `mosquitto` | eclipse-mosquitto:2 | 1883 / 9001 | MQTT broker for device messages |
| `influxdb` | influxdb:2.7 | 8086 | Time-series database |
| `telegraf` | telegraf:1.30 | — | MQTT → InfluxDB pipeline |
| `grafana` | grafana/grafana:latest | 3000 | Dashboards and visualisation |
| `api` | Built locally | 8000 | FastAPI — alarms, thresholds, device registry |

---

## Project Structure

```
esp32-influx/
├── api/
│   ├── Dockerfile
│   ├── main.py            ← FastAPI application
│   └── requirements.txt
├── mosquitto/
│   ├── config/
│   │   └── mosquitto.conf
│   ├── data/
│   └── log/
├── telegraf/
│   └── telegraf.conf      ← MQTT topic subscriptions and field mappings
├── .env                   ← Credentials (never commit this)
├── .gitignore
└── docker-compose.yml
```

---

## Quick Start

### 1. Configure environment

Copy the example and fill in your values:

```bash
cp .env.example .env
```

`.env` contents:
```env
INFLUXDB_USERNAME=admin
INFLUXDB_PASSWORD=yourpassword
INFLUXDB_ORG=myorg
INFLUXDB_BUCKET=esp32_sensors
INFLUXDB_TOKEN=your-secret-token
```

> **Important:** If you change `INFLUXDB_TOKEN`, `INFLUXDB_ORG` or `INFLUXDB_BUCKET`, update `telegraf/telegraf.conf` to match.

### 2. Start the stack

```bash
cd esp32-influx
docker-compose up -d
```

### 3. Verify everything is running

```bash
docker-compose ps
```

All containers should show `healthy` or `running`. InfluxDB takes ~30 seconds to initialise on first boot.

### 4. Access the services

| Service | URL | Default credentials |
|---|---|---|
| Grafana | http://\<pi-ip\>:3000 | admin / admin |
| InfluxDB | http://\<pi-ip\>:8086 | from .env |
| FastAPI docs | http://\<pi-ip\>:8000/docs | — |

---

## API

The FastAPI service exposes endpoints for device management, alarm handling and threshold configuration. Interactive docs are available at `/docs`.

### Endpoint summary

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Service health check |
| `GET` | `/api/devices` | List all active device IDs |
| `GET` | `/api/devices/{id}/latest` | Latest sensor reading for a device |
| `GET` | `/api/devices/registry` | Show registered device IPs |
| `POST` | `/api/devices/{id}/register` | Register a device LAN IP |
| `GET` | `/api/alarms/active` | All unacknowledged alarms (flat) |
| `GET` | `/api/alarms/active/grouped` | Alarms grouped by device |
| `GET` | `/api/alarms/history?hours=24` | Alarm history (1–720 h) |
| `POST` | `/api/alarms/{id}/{type}/acknowledge` | Acknowledge one alarm |
| `POST` | `/api/alarms/acknowledge-all` | Acknowledge all active alarms |
| `GET` | `/api/thresholds/{id}` | Read thresholds for a device |
| `POST` | `/api/thresholds/{id}` | Set thresholds for one device |
| `POST` | `/api/thresholds/batch` | Set thresholds for multiple devices |
| `GET` | `/api/devices/{id}/thresholds_from_device` | Read thresholds from physical device |

### Alert types

`temp_high` · `temp_low` · `hum_high` · `hum_low` · `aqi_high` · `tvoc_high` · `co2_high`

### Default thresholds

| Parameter | Default |
|---|---|
| temp_high | 30.0 °C |
| temp_low | 5.0 °C |
| hum_high | 80.0 % |
| hum_low | 20.0 % |
| aqi_high | 3 (Moderate) |
| co2_high | 1000 ppm |
| tvoc_high | 500 ppb |

---

## MQTT Topics

| Topic | Written by | Content |
|---|---|---|
| `IESWIC3A/data` | ESP32 devices | Full sensor payload (telemetry) |
| `IESWIC3A/{device_id}/config` | API / device | Threshold configuration |

### Sensor payload fields

`device_id` · `firmware` · `rssi` · `temperature` · `humidity` · `aqi` · `aqi_label` · `tvoc` · `eco2` · `air_quality_status` · `alert_temp` · `alert_hum` · `light_on`

---

## Common Operations

```bash
# Start everything
docker-compose up -d

# Stop everything (data is preserved)
docker-compose down

# Rebuild API after code changes
docker-compose up -d --build api

# Follow API logs
docker-compose logs -f api

# Restart one service
docker-compose restart api

# View all service status
docker-compose ps
```

> ⚠️ `docker-compose down -v` removes named volumes and **deletes all InfluxDB and Grafana data**. Use with caution.

---

## Alarm Logic

The API runs a background processor every **30 seconds** that:

1. Reads the latest sensor values from InfluxDB for each active device
2. Compares them against per-device thresholds (or defaults if none set)
3. Writes an `alert_events` record when a threshold is breached
4. **Auto-resolves** alarms when the value returns within range
5. Suppresses duplicate alarms — a new alarm is only written when the previous one has been acknowledged or resolved

---

## Remote Access

The API is exposed publicly via a Cloudflare Tunnel running as a systemd service on the Pi. The tunnel URL is assigned automatically and can be retrieved with:

```bash
sudo journalctl -u cloudflared | grep trycloudflare
```

If the Pi reboots, the service restarts automatically but the URL changes.

---

## Troubleshooting

**API cannot reach InfluxDB**
- Check `INFLUXDB_TOKEN`, `INFLUXDB_ORG` and `INFLUXDB_BUCKET` in `.env`
- Confirm InfluxDB is healthy: `docker-compose ps`
- The API uses `network_mode: host` and connects to `127.0.0.1:8086`

**No sensor data in InfluxDB**
- Confirm ESP32 devices are publishing to `IESWIC3A/#`
- Check Telegraf logs: `docker-compose logs telegraf`
- Verify topic and field names match `telegraf/telegraf.conf`

**Alarms not appearing**
- Check that the alarm processor is running: `docker-compose logs api | grep "Checking"`
- Confirm sensor readings are present: `GET /api/devices/{id}/latest`
- Check thresholds are set correctly: `GET /api/thresholds/{id}`

**Mosquitto permission denied on mosquitto.db**
- Fix ownership: `sudo chown -R 1883:1883 mosquitto/data`

**Grafana datasource error**
- Verify InfluxDB is healthy and the token in Grafana settings matches `.env`

---

## Security Notes

- Mosquitto currently allows **anonymous connections** — suitable for a trusted LAN, not for internet-facing deployments
- The API key for external access is set via `API_KEY` in `docker-compose.yml` under the `api` service environment
- Never commit `.env` — it is listed in `.gitignore`
- `telegraf/telegraf.conf` currently hardcodes the InfluxDB token — keep it aligned with `.env`

---

## Health Checks

```bash
# Check container health states
docker-compose ps

# Inspect individual health
docker inspect --format='{{json .State.Health}}' iot-api
docker inspect --format='{{json .State.Health}}' influxdb
docker inspect --format='{{json .State.Health}}' mosquitto
docker inspect --format='{{json .State.Health}}' grafana
```