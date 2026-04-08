#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Adafruit_SHTC3.h>


const char* ssid       = "EEXME Solution Co Ltd";
const char* password   = "EEXMESOLUTION1568";
const char* mqttServer = "192.168.0.250"; 
const int   mqttPort   = 1883;
const char* mqttTopic  = "esp32/sensors";

WiFiClient espClient;
PubSubClient client(espClient);
Adafruit_SHTC3 shtc3;

void connectWifi() {
  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected: " + WiFi.localIP().toString());
}

void connectMQTT() {
  while (!client.connected()) {
    Serial.print("Connecting to MQTT...");
    if (client.connect("ESP32Client")) {
      Serial.println("connected");
    } else {
      Serial.print("failed, rc=");
      Serial.println(client.state());
      delay(2000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  // I2C — C3 SuperMini default: SDA=8, SCL=9
  Wire.begin(8, 9);

  if (!shtc3.begin()) {
    Serial.println("Could not find SHTC3 sensor! Check wiring.");
    while (1) delay(10);
  }
  Serial.println("SHTC3 found!");

  connectWifi();
  client.setServer(mqttServer, mqttPort);
}

void loop() {
  if (!client.connected()) connectMQTT();
  client.loop();

  sensors_event_t humidity, temperature;
  shtc3.getEvent(&humidity, &temperature);

  // Build JSON payload
  JsonDocument doc;
  doc["sensor_id"]   = "esp32_01";
  doc["temperature"] = round(temperature.temperature * 10) / 10.0;
  doc["humidity"]    = round(humidity.relative_humidity * 10) / 10.0;

  char payload[128];
  serializeJson(doc, payload);

  client.publish(mqttTopic, payload);
  Serial.println(payload);

  delay(5000);  // read every 5 seconds
}
