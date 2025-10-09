import paho.mqtt.client as mqtt
import firebase_admin
from firebase_admin import credentials, db
import json
import time
import logging
import signal
import sys
from datetime import datetime
import threading
import socket

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class MQTTFirebaseBridge:
    def __init__(self):
        self.TOTAL_SLOTS = 4
        self.running = True
        self.mqtt_connected = False
        self.firebase_initialized = False
        self.last_mqtt_attempt = 0
        self.MQTT_RECONNECT_DELAY = 2  # Reduced from 5 seconds
        self.setup_signal_handlers()
        self.setup_firebase()
        self.setup_mqtt()
        
    def setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown"""
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

    def signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False
        if hasattr(self, 'mqtt_client'):
            self.mqtt_client.disconnect()
        if firebase_admin._apps:
            self.update_firebase('system/connectionStatus', 'offline')
        sys.exit(0)

    def setup_firebase(self):
        """Initialize Firebase Admin SDK"""
        try:
            # Initialize Firebase with your service account key
            cred = credentials.Certificate("service-account-key.json")
            firebase_admin.initialize_app(cred, {
                'databaseURL': 'https://park-poa-default-rtdb.asia-southeast1.firebasedatabase.app/'
            })
            logger.info("Firebase initialized successfully")
            self.firebase_initialized = True
            
            # Initialize default values
            self.initialize_firebase_data()
            
        except Exception as e:
            logger.error(f"Firebase initialization failed: {e}")
            self.firebase_initialized = False

    def initialize_firebase_data(self):
        """Initialize Firebase with default values"""
        try:
            defaults = {
                'parking/availableSlots': self.TOTAL_SLOTS,
                'parking/occupiedSlots': 0,
                'parking/totalSlots': self.TOTAL_SLOTS,
                'parking/gateStatus': 'CLOSED',
                'system/connectionStatus': 'connecting',
                'system/lastUpdate': datetime.now().isoformat(),
                'system/lastConnected': datetime.now().isoformat()
            }
            
            # Initialize slot status
            slots = {}
            for i in range(1, self.TOTAL_SLOTS + 1):
                slots[f"slot{i}"] = "Unoccupied"
            defaults['parking/slots'] = slots
            
            # Update Firebase with defaults
            for path, value in defaults.items():
                self.update_firebase(path, value, log=False)
                
            logger.info("Firebase data initialized with default values")
            
        except Exception as e:
            logger.error(f"Error initializing Firebase data: {e}")

    def setup_mqtt(self):
        """Initialize MQTT client with faster connection settings"""
        self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect
        
        # Set faster timeouts and keepalive
        self.mqtt_client.connect_timeout = 5  # Reduced from default
        self.mqtt_client.keepalive = 30  # Reduced from 60
        
        # Topic mappings: MQTT topic -> Firebase path
        self.topic_mappings = {
            'parking/available_slots': 'availableSlots',
            'parking/occupied_slots': 'occupiedSlots',
            'parking/gate_status': 'gateStatus'
        }

    def check_mqtt_broker_available(self, host, port, timeout=3):
        """Quick check if MQTT broker is reachable"""
        try:
            socket.setdefaulttimeout(timeout)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except:
            return False

    def validate_slot_data(self, available_slots, occupied_slots):
        """Validate that slot data makes sense"""
        # Ensure values are within bounds
        available_slots = max(0, min(self.TOTAL_SLOTS, available_slots))
        occupied_slots = max(0, min(self.TOTAL_SLOTS, occupied_slots))
        
        # Check if sum matches total slots
        if available_slots + occupied_slots != self.TOTAL_SLOTS:
            logger.warning(f"Slot count mismatch - Available: {available_slots}, Occupied: {occupied_slots}, Total: {self.TOTAL_SLOTS}")
            # Auto-correct: prioritize available slots
            available_slots = max(0, min(self.TOTAL_SLOTS, available_slots))
            occupied_slots = self.TOTAL_SLOTS - available_slots
            logger.info(f"Auto-corrected to - Available: {available_slots}, Occupied: {occupied_slots}")
        
        return available_slots, occupied_slots

    def update_slot_status(self, available_slots, occupied_slots):
        """Update individual slot status in Firebase"""
        try:
            # Validate data first
            available_slots, occupied_slots = self.validate_slot_data(available_slots, occupied_slots)
            
            # Calculate which slots are occupied
            slots = {}
            for i in range(1, self.TOTAL_SLOTS + 1):
                if i <= occupied_slots:
                    slots[f"slot{i}"] = "Occupied"
                else:
                    slots[f"slot{i}"] = "Unoccupied"
            
            # Update Firebase with slot status
            self.update_firebase('parking/slots', slots)
            logger.info(f"Updated slot status - Occupied: {occupied_slots}, Available: {available_slots}")
            
        except Exception as e:
            logger.error(f"Error updating slot status: {e}")

    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None):
        """Callback when connected to MQTT broker"""
        if rc == 0:
            logger.info("Successfully connected to MQTT broker")
            self.mqtt_connected = True
            
            # Subscribe to all parking topics
            for topic in self.topic_mappings.keys():
                client.subscribe(topic)
                logger.info(f"Subscribed to topic: {topic}")
            
            # Update connection status in Firebase
            self.update_firebase('system/connectionStatus', 'connected')
            self.update_firebase('system/lastConnected', datetime.now().isoformat())
            
        else:
            logger.error(f"Failed to connect to MQTT broker with code: {rc}")
            self.mqtt_connected = False
            self.update_firebase('system/connectionStatus', f'error_{rc}')

    def on_mqtt_disconnect(self, client, userdata, rc, properties=None):
        """Callback when disconnected from MQTT broker"""
        logger.warning(f"Disconnected from MQTT broker with code: {rc}")
        self.mqtt_connected = False
        self.update_firebase('system/connectionStatus', 'disconnected')

    def on_mqtt_message(self, client, userdata, msg):
        """Callback when MQTT message is received"""
        try:
            topic = msg.topic
            payload = msg.payload.decode('utf-8')
            
            logger.info(f"MQTT Received - Topic: {topic}, Payload: {payload}")
            
            # Process the message based on topic
            self.process_message(topic, payload)
            
        except UnicodeDecodeError:
            logger.error(f"Failed to decode MQTT message payload from topic: {topic}")
        except Exception as e:
            logger.error(f"Error processing MQTT message: {e}")

    def process_message(self, topic, payload):
        """Process and route MQTT messages to Firebase"""
        if topic in self.topic_mappings:
            firebase_field = self.topic_mappings[topic]
            
            try:
                # Convert data types appropriately
                if topic in ['parking/available_slots', 'parking/occupied_slots']:
                    value = int(payload)  # Convert to integer
                    
                    # Update the main field first
                    self.update_firebase(f'parking/{firebase_field}', value)
                    
                    # Calculate and update the complementary value
                    if topic == 'parking/available_slots':
                        available_slots = value
                        occupied_slots = self.TOTAL_SLOTS - value
                        # Update occupied slots for consistency
                        self.update_firebase('parking/occupiedSlots', occupied_slots)
                    else:  # parking/occupied_slots
                        occupied_slots = value
                        available_slots = self.TOTAL_SLOTS - value
                        # Update available slots for consistency
                        self.update_firebase('parking/availableSlots', available_slots)
                    
                    # Update slot status whenever slot counts change
                    self.update_slot_status(available_slots, occupied_slots)
                    
                else:  # gate_status and other string topics
                    value = str(payload).strip().upper()  # Clean and standardize
                    self.update_firebase(f'parking/{firebase_field}', value)
                
                # Update timestamp
                self.update_firebase('system/lastUpdate', datetime.now().isoformat())
                
            except ValueError as e:
                logger.error(f"Data conversion error for {topic} with payload '{payload}': {e}")
            except Exception as e:
                logger.error(f"Firebase update error for {topic}: {e}")
        else:
            logger.warning(f"Received message from unknown topic: {topic}")

    def update_firebase(self, path, value, log=True):
        """Update Firebase Realtime Database"""
        if not self.firebase_initialized:
            if log:
                logger.warning(f"Firebase not initialized, skipping update: {path}")
            return
            
        try:
            ref = db.reference(path)
            ref.set(value)
            if log:
                logger.info(f"Firebase updated - {path}: {value}")
        except Exception as e:
            logger.error(f"Failed to update Firebase at {path}: {e}")

    def connect_mqtt(self, host='localhost', port=1883):
        """Connect to MQTT broker with faster retry logic"""
        current_time = time.time()
        
        # Rate limiting: don't attempt too frequently
        if current_time - self.last_mqtt_attempt < self.MQTT_RECONNECT_DELAY:
            return False
            
        self.last_mqtt_attempt = current_time
        
        try:
            # Quick check if broker is reachable
            if not self.check_mqtt_broker_available(host, port, timeout=2):
                logger.warning(f"MQTT broker at {host}:{port} is not reachable")
                return False
                
            logger.info(f"Connecting to MQTT broker at {host}:{port}")
            self.mqtt_client.connect(host, port, keepalive=30)
            logger.info("MQTT connection established successfully")
            return True
            
        except Exception as e:
            logger.error(f"MQTT connection error: {e}")
            return False

    def start(self, host, port):
        """Start the bridge service with faster connection monitoring"""
        try:
            # Initial connection attempt
            if not self.connect_mqtt(host, port):
                logger.warning("Initial MQTT connection failed, will retry in background")
            
            # Start MQTT loop in background thread
            self.mqtt_client.loop_start()
            
            logger.info("Bridge service started successfully")
            
            # Faster connection monitoring loop
            connection_check_interval = 5  # Check every 5 seconds
            last_status_update = 0
            status_update_interval = 30  # Update status every 30 seconds
            
            while self.running:
                current_time = time.time()
                
                # Check MQTT connection status
                if not self.mqtt_connected:
                    logger.info("MQTT connection lost, attempting to reconnect...")
                    self.update_firebase('system/connectionStatus', 'reconnecting')
                    self.connect_mqtt(host, port)
                
                # Periodic status update
                if current_time - last_status_update > status_update_interval:
                    if self.mqtt_connected and self.firebase_initialized:
                        self.update_firebase('system/connectionStatus', 'connected', log=False)
                    last_status_update = current_time
                
                time.sleep(connection_check_interval)  # Reduced sleep time
                
        except KeyboardInterrupt:
            logger.info("Shutdown initiated by user")
        except Exception as e:
            logger.error(f"Bridge error: {e}")
        finally:
            self.cleanup()

    def cleanup(self):
        """Cleanup resources before shutdown"""
        logger.info("Cleaning up resources...")
        self.running = False
        
        # Stop MQTT client
        if hasattr(self, 'mqtt_client'):
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
        
        # Update Firebase status
        if self.firebase_initialized:
            self.update_firebase('system/connectionStatus', 'offline')
        
        logger.info("Bridge shutdown complete")

# Configuration
class Config:
    MQTT_BROKER_HOST = 'localhost'  # or your computer's IP
    MQTT_BROKER_PORT = 1883

def main():
    """Main application entry point"""
    logger.info("Starting MQTT to Firebase Bridge...")
    
    bridge = None
    try:
        bridge = MQTTFirebaseBridge()
        bridge.start(
            Config.MQTT_BROKER_HOST, 
            Config.MQTT_BROKER_PORT
        )
    except KeyboardInterrupt:
        logger.info("Application interrupted by user")
    except Exception as e:
        logger.error(f"Failed to start bridge: {e}")
        if bridge:
            bridge.cleanup()
    finally:
        if bridge:
            bridge.cleanup()

if __name__ == "__main__":
    main()