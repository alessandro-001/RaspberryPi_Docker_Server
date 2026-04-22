# ESP32 MQTT + InfluxDB Stack

This project runs a small IoT platform for ESP32 sensor devices using Docker Compose.

It includes:

- `mosquitto`: MQTT broker for device messages
- `telegraf`: subscribes to MQTT topics and writes sensor/config data into InfluxDB
- `influxdb`: time-series database
- `grafana`: dashboards and visualization
- `api`: FastAPI service for device readings, alarms, thresholds, and device registry

## Architecture

Data flow:

1. ESP32 devices publish JSON payloads to MQTT topics under `IESWIC3A/...`
2. `mosquitto` receives the messages
3. `telegraf` consumes MQTT messages and writes them to InfluxDB
4. `grafana` reads from InfluxDB for dashboards
5. `api` reads from InfluxDB and exposes HTTP endpoints for health, devices, alarms, and threshold management

## Services

### `mosquitto`

- Image: `eclipse-mosquitto:2`
- Ports:
  - `1883` MQTT
  - `9001` WebSocket
- Persistent data:
  - `./mosquitto/config`
  - `./mosquitto/data`
  - `./mosquitto/log`

Current config:

- anonymous access is enabled
- persistence is enabled
- logs are written to `/mosquitto/log/mosquitto.log`

### `influxdb`

- Image: `influxdb:2.7`
- Port: `8086`
- Persistent data:
  - named volume `influxdb_data`

Initialized from `.env`:

- `INFLUXDB_USERNAME`
- `INFLUXDB_PASSWORD`
- `INFLUXDB_ORG`
- `INFLUXDB_BUCKET`
- `INFLUXDB_TOKEN`

### `telegraf`

- Image: `telegraf:1.30`
- Reads MQTT topics:
  - `IESWIC3A/#`
  - `IESWIC3A/+/config`
- Writes into InfluxDB measurement names:
  - `IESWIC3A`
  - `device_config`

Important:

- The InfluxDB token, org, and bucket are currently hardcoded in [`telegraf/telegraf.conf`](./telegraf/telegraf.conf).
- Keep them aligned with `.env`, especially `token`, `organization`, and `bucket`.

### `grafana`

- Image: `grafana/grafana:latest`
- Port: `3000`
- Persistent data:
  - named volume `grafana_data`

### `api`

- Built locally from [`api/Dockerfile`](./api/Dockerfile)
- Runs FastAPI on port `8000`
- Uses `network_mode: host`
- Reads InfluxDB connection settings from environment variables

Main purpose:

- health check
- fetch latest device readings
- fetch active alarm state and history
- acknowledge alarms
- read and update device thresholds
- store and return device IP registry data

## Project Structure

```text
esp32-influx/
├── api/
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
├── mosquitto/
│   ├── config/
│   ├── data/
│   └── log/
├── telegraf/
│   └── telegraf.conf
├── .env
└── docker-compose.yml
```

## Requirements

Before starting:

- Docker installed
- Docker Compose available
- ESP32 devices publishing to the expected MQTT topics

## Environment Variables

Configured in [`.env`](./.env):

```env
INFLUXDB_USERNAME=admin
INFLUXDB_PASSWORD=admin1234
INFLUXDB_ORG=myorg
INFLUXDB_BUCKET=esp32_sensors
INFLUXDB_TOKEN=my-super-secret-token
```

Notes:

- These values initialize InfluxDB on first startup.
- If you change the token, bucket, or org, also update `telegraf/telegraf.conf`.

## Start the Stack

From the `esp32-influx` directory:

```bash
docker-compose up -d
```

Or with the newer CLI:

```bash
docker compose up -d
```

## Stop the Stack

```bash
docker-compose down
```

This stops and removes containers, but does not remove named volumes unless you add `-v`.

## Restart the Stack

```bash
docker-compose down
docker-compose up -d
```

This is normally safe and should not destroy your persisted data.

Data that survives a normal restart:

- InfluxDB data in `influxdb_data`
- Grafana data in `grafana_data`
- Mosquitto files in `./mosquitto/data`

Dangerous command:

```bash
docker-compose down -v
```

That removes named volumes and can delete stored InfluxDB and Grafana data.

## Check Container Status

```bash
docker-compose ps
```

## View Logs

All services:

```bash
docker-compose logs -f
```

Single service examples:

```bash
docker-compose logs -f api
docker-compose logs -f influxdb
docker-compose logs -f grafana
docker-compose logs -f mosquitto
docker-compose logs -f telegraf
```

## Rebuild the API Container

If you change files in `api/`:

```bash
docker-compose up -d --build api
```

Or rebuild the whole stack:

```bash
docker-compose up -d --build
```

## Exposed Ports

- `1883`: Mosquitto MQTT
- `9001`: Mosquitto WebSocket
- `3000`: Grafana
- `8086`: InfluxDB
- `8000`: FastAPI service

## API Endpoints

Examples based on [`api/main.py`](./api/main.py):

- `GET /health`
- `GET /api/devices`
- `GET /api/devices/{device_id}/latest`
- `GET /api/alarms/active`
- `GET /api/alarms/active/grouped`
- `GET /api/alarms/history`
- `POST /api/alarms/{device_id}/{alert_type}/acknowledge`
- `POST /api/alarms/acknowledge-all`
- `GET /api/thresholds/{device_id}`
- `POST /api/thresholds/{device_id}`
- `POST /api/thresholds/batch`
- `GET /api/devices/registry`
- `POST /api/devices/{device_id}/register`
- `GET /api/devices/{device_id}/thresholds_from_device`

FastAPI docs are available at:

- `http://<host>:8000/docs`

## MQTT Topics

The current Telegraf config expects:

- sensor data topics under `IESWIC3A/#`
- threshold/config topics under `IESWIC3A/+/config`

Fields parsed from sensor payloads include:

- `device_id`
- `firmware`
- `rssi`
- `temperature`
- `humidity`
- `alert_temp`
- `alert_hum`
- `aqi`
- `aqi_label`
- `tvoc`
- `eco2`
- `air_quality_status`
- `light_on`

Fields parsed from config payloads include:

- `temp_high`
- `temp_low`
- `hum_high`
- `hum_low`
- `aqi_high`
- `co2_high`
- `tvoc_high`

## Health Checks

Compose health checks are configured for:

- `mosquitto`
- `influxdb`
- `telegraf`
- `grafana`
- `api`

Useful commands:

```bash
docker-compose ps
docker inspect --format='{{json .State.Health}}' mosquitto
docker inspect --format='{{json .State.Health}}' influxdb
docker inspect --format='{{json .State.Health}}' grafana
docker inspect --format='{{json .State.Health}}' iot-api
```

## Common Admin Tasks

### Stop everything

```bash
docker-compose down
```

### Start everything

```bash
docker-compose up -d
```

### Restart one service

```bash
docker-compose restart api
```

### Rebuild API after code changes

```bash
docker-compose up -d --build api
```

### Follow API logs

```bash
docker-compose logs -f api
```

## Notes and Caveats

- `api` uses `network_mode: host`, so it shares the host network stack instead of the Compose network.
- `mosquitto` currently allows anonymous connections. That is convenient for local testing, but not ideal for untrusted networks.
- `grafana/grafana:latest` will move over time. Pinning a version can make deployments more predictable.
- `telegraf/telegraf.conf` currently hardcodes InfluxDB connection settings instead of reading them from environment variables.

## Troubleshooting

### API cannot reach InfluxDB

Check:

- InfluxDB is healthy
- `INFLUXDB_TOKEN`, `INFLUXDB_ORG`, and `INFLUXDB_BUCKET` are correct
- the `api` container is running

### No sensor data in InfluxDB

Check:

- ESP32 devices are publishing to MQTT
- Mosquitto is running on port `1883`
- Telegraf is healthy
- topic names match `IESWIC3A/#`
- payload field names match `telegraf/telegraf.conf`

### Restart did not pick up API code changes

Use:

```bash
docker-compose up -d --build api
```

because the `api` service is built from the local Dockerfile.
