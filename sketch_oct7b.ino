#include <WiFi.h>
#include <PubSubClient.h> // MQTT library
#include <ESP32Servo.h>   // For controlling the Servo motor

// ---------------------------
// 1. WiFi & MQTT Configuration
// ---------------------------
const char* ssid = "REDMI 15C";
const char* password = "benson1234";
const char* mqtt_server = "10.197.145.50"; // Your computer's IP address where Mosquitto is running
const char* mqtt_client_id = "ESP32_Smart_Parking_Client";
const int mqtt_port = 1883; // Standard MQTT port

// MQTT Topics
const char* topic_available_slots = "parking/available_slots";
const char* topic_occupied_slots = "parking/occupied_slots";
const char* topic_gate_status = "parking/gate_status";

// ---------------------------
// 2. Hardware Pin Definitions
// ---------------------------
// Servo Gate
const int SERVO_PIN = 13;
Servo gateServo;
const int GATE_OPEN_ANGLE = 90;  // Angle for open gate
const int GATE_CLOSED_ANGLE = 0; // Angle for closed gate

// Ultrasonic Sensor - Entry
const int TRIG_ENTRY_PIN = 4;
const int ECHO_ENTRY_PIN = 2;

// Ultrasonic Sensor - Exit
const int TRIG_EXIT_PIN = 25;
const int ECHO_EXIT_PIN = 34;

// ---------------------------
// 3. System Variables
// ---------------------------
const int MAX_SLOTS = 4;
int availableSlots = MAX_SLOTS;
int occupiedSlots = 0;
bool gateOpen = false; // Track gate status
const int DETECTION_DISTANCE_CM = 6; // Max distance to consider a car detected
unsigned long lastMsg = 0;
const long interval = 5000; // Update interval for status (5 seconds)

// Connection tracking
unsigned long lastReconnectAttempt = 0;
const long RECONNECT_INTERVAL = 5000; // 5 seconds between reconnect attempts

// ---------------------------
// 4. Object Initialization
// ---------------------------
WiFiClient espClient;
PubSubClient client(espClient);

// ---------------------------
// 5. Function Prototypes
// ---------------------------
void setup_wifi();
bool reconnect();
long readDistanceCM(int trigPin, int echoPin);
void handleGate(bool open);
void updateParkingStatus();
void publishParkingData();
void publishGateStatus(bool isOpen);
void checkWiFiConnection();

// ---------------------------
// 6. Setup Function
// ---------------------------
void setup() {
  Serial.begin(115200);
  Serial.println("Starting ESP32 Smart Parking System...");

  // Initialize Servo
  gateServo.attach(SERVO_PIN);
  handleGate(false); // Start with the gate closed

  // Initialize Ultrasonic Pins
  // Entry Sensor
  pinMode(TRIG_ENTRY_PIN, OUTPUT);
  pinMode(ECHO_ENTRY_PIN, INPUT);
  // Exit Sensor
  pinMode(TRIG_EXIT_PIN, OUTPUT);
  pinMode(ECHO_EXIT_PIN, INPUT);

  setup_wifi();
  
  // Configure MQTT client with keepalive and connection timeout
  client.setServer(mqtt_server, mqtt_port);
  client.setKeepAlive(60); // 60 second keepalive
  client.setSocketTimeout(30); // 30 second socket timeout
  
  // No client.setCallback() needed as we are only publishing data.

  Serial.println("Setup complete.");
  
  // Publish initial status
  if (client.connected()) {
    publishParkingData();
    publishGateStatus(false);
  }
}

// ---------------------------
// 7. Main Loop Function
// ---------------------------
void loop() {
  // Check and maintain WiFi connection
  checkWiFiConnection();
  
  // Check and maintain MQTT connection
  if (!client.connected()) {
    unsigned long now = millis();
    if (now - lastReconnectAttempt > RECONNECT_INTERVAL) {
      lastReconnectAttempt = now;
      if (reconnect()) {
        lastReconnectAttempt = 0;
      }
    }
  } else {
    client.loop();
  }

  // Read sensor data and manage parking logic only if connected
  if (client.connected()) {
    updateParkingStatus();
  }
  
  // Basic delay to prevent watchdog timer resets
  delay(100); 
}

// ----------------------------------------
// 8. Parking System Logic Implementation
// ----------------------------------------

/**
 * @brief Reads the distance from an ultrasonic sensor.
 * @param trigPin Trigger pin of the sensor.
 * @param echoPin Echo pin of the sensor.
 * @return long Distance in centimeters.
 */
long readDistanceCM(int trigPin, int echoPin) {
  // Clears the trigPin condition
  digitalWrite(trigPin, LOW);
  delayMicroseconds(2);

  // Sets the trigPin HIGH for 10 micro-seconds
  digitalWrite(trigPin, HIGH);
  delayMicroseconds(10);
  digitalWrite(trigPin, LOW);

  // Reads the echoPin, returns the sound wave travel time in microseconds
  long duration = pulseIn(echoPin, HIGH, 30000); // 30ms timeout

  // If pulseIn timed out, return a large distance
  if (duration == 0) {
    return 1000; // Return 1000cm to indicate no detection
  }

  // Calculating the distance in cm
  // Distance = Duration * speed of sound (340 m/s or 0.034 cm/µs) / 2
  long distance = duration * 0.034 / 2;
  return distance;
}

/**
 * @brief Controls the servo gate position and publishes status.
 * @param open True to open the gate, false to close it.
 */
void handleGate(bool open) {
  int targetAngle = open ? GATE_OPEN_ANGLE : GATE_CLOSED_ANGLE;
  gateServo.write(targetAngle);
  gateOpen = open; // Update gate status
  Serial.printf("Gate is now %s (Angle: %d)\n", open ? "OPEN" : "CLOSED", targetAngle);
  
  // Publish gate status immediately when it changes
  if (client.connected()) {
    publishGateStatus(open);
  }
}

/**
 * @brief Publishes gate status to MQTT.
 * @param isOpen True if gate is open, false if closed.
 */
void publishGateStatus(bool isOpen) {
  const char* status = isOpen ? "OPEN" : "CLOSED";
  if (client.publish(topic_gate_status, status, true)) { // true for retained
    Serial.printf("Gate status published: %s\n", status);
  } else {
    Serial.println("Failed to publish gate status");
  }
}

/**
 * @brief Publishes all parking data to MQTT topics.
 */
void publishParkingData() {
  // Update occupied slots
  occupiedSlots = MAX_SLOTS - availableSlots;
  
  // Publish available slots
  char availablePayload[10];
  snprintf(availablePayload, 10, "%d", availableSlots);
  if (client.publish(topic_available_slots, availablePayload, true)) { // true for retained
    Serial.printf("Available slots published: %s\n", availablePayload);
  } else {
    Serial.println("Failed to publish available slots");
  }
  
  // Publish occupied slots
  char occupiedPayload[10];
  snprintf(occupiedPayload, 10, "%d", occupiedSlots);
  if (client.publish(topic_occupied_slots, occupiedPayload, true)) { // true for retained
    Serial.printf("Occupied slots published: %s\n", occupiedPayload);
  } else {
    Serial.println("Failed to publish occupied slots");
  }
}

/**
 * @brief Main function to read sensors, update slot count, and manage gate.
 */
void updateParkingStatus() {
  long distEntry = readDistanceCM(TRIG_ENTRY_PIN, ECHO_ENTRY_PIN);
  long distExit = readDistanceCM(TRIG_EXIT_PIN, ECHO_EXIT_PIN);

  bool carAtEntry = distEntry > 0 && distEntry < DETECTION_DISTANCE_CM;
  bool carAtExit = distExit > 0 && distExit < DETECTION_DISTANCE_CM;

  // --- Entry Logic ---
  if (carAtEntry) {
    if (availableSlots > 0) {
      Serial.println("Car detected at entry. Slot available. Opening gate...");
      handleGate(true); // Open the gate

      // Wait for the car to pass (you might need to fine-tune this delay)
      delay(3000); 

      // Re-check if car has moved past the sensor
      if (readDistanceCM(TRIG_ENTRY_PIN, ECHO_ENTRY_PIN) > DETECTION_DISTANCE_CM) {
        availableSlots--;
        Serial.printf("Car entered. Slots remaining: %d\n", availableSlots);
        if (client.connected()) {
          publishParkingData(); // Publish updated slot counts
        }
      } else {
        Serial.println("Car is still blocking entry sensor after delay.");
      }
      handleGate(false); // Close the gate regardless of the second check

    } else {
      Serial.println("Car detected at entry. NO SLOTS AVAILABLE. Gate remains closed.");
      handleGate(false); // Ensure it's closed
    }
  }

  // --- Exit Logic ---
  if (carAtExit) {
    // A car is detected at the exit sensor
    Serial.println("Car detected at exit. Opening gate for exit...");
    handleGate(true); // Open the gate

    // Wait for the car to pass
    delay(3000); 

    // Re-check if car has moved past the sensor
    if (readDistanceCM(TRIG_EXIT_PIN, ECHO_EXIT_PIN) > DETECTION_DISTANCE_CM) {
        if (availableSlots < MAX_SLOTS) {
          availableSlots++;
          Serial.printf("Car exited. Slots remaining: %d\n", availableSlots);
          if (client.connected()) {
            publishParkingData(); // Publish updated slot counts
          }
        }
    } else {
      Serial.println("Car is still blocking exit sensor after delay.");
    }
    handleGate(false); // Close the gate regardless of the second check
  }

  // --- Periodic MQTT Publishing ---
  unsigned long now = millis();
  if (now - lastMsg > interval) {
    lastMsg = now;
    if (client.connected()) {
      publishParkingData(); // Publish periodic updates
    }
  }
}

// ---------------------------
// 9. Networking Functions
// ---------------------------

/**
 * @brief Checks and maintains WiFi connection
 */
void checkWiFiConnection() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi connection lost. Reconnecting...");
    setup_wifi();
  }
}

/**
 * @brief Connects to the Wi-Fi network.
 */
void setup_wifi() {
  delay(10);
  Serial.println();
  Serial.print("Connecting to ");
  Serial.println(ssid);

  WiFi.disconnect();
  WiFi.begin(ssid, password);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 20) {
    delay(500);
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("");
    Serial.println("WiFi connected");
    Serial.print("IP address: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("");
    Serial.println("Failed to connect to WiFi");
  }
}

/**
 * @brief Handles reconnection to the MQTT broker.
 * @return true if connected successfully, false otherwise
 */
bool reconnect() {
  Serial.print("Attempting MQTT connection...");
  
  // Attempt to connect with clean session
  if (client.connect(mqtt_client_id)) {
    Serial.println("connected");
    
    // Publish initial status after reconnection
    publishParkingData();
    publishGateStatus(gateOpen);
    return true;
  } else {
    Serial.print("failed, rc=");
    Serial.print(client.state());
    Serial.println(" try again in 5 seconds");
    return false;
  }
}