import threading
import time
import random
import requests
import logging
import uuid
from typing import Dict, Any
from core.base import BasePlugin

logger = logging.getLogger("ZatAgent.Sensor")

class SensorPlugin(BasePlugin):
    """
    YANG Push Lite Simulation (On-Change Edition)
    - Simulates hardware sensors (Temp, Fan, Voltage).
    - Only pushes updates when values CHANGE significantly (Dampening).
    - Sends periodic heartbeats if no change occurs for a while.
    """
    
    def __init__(self):
        self._subscriptions = {} 
        self._lock = threading.Lock()

    @property
    def name(self): return "sensor"

    def validate(self, config):
        # We allow 'discover' action now too
        if "action" not in config: raise ValueError("Sensor needs 'action'")
        return True

    def _generate_value(self, xpath):
        """Simulate hardware reading with random jitter"""
        if "temp" in xpath: return round(random.uniform(35.0, 85.0), 1)
        if "fan" in xpath: return int(random.uniform(2000, 6000))
        if "volt" in xpath: return round(random.uniform(11.8, 12.2), 2)
        if "errors" in xpath: return int(random.uniform(0, 5)) # Interface errors
        return 0

    def _stream_loop(self, sub_id, xpath, check_interval, url, stop_event):
        logger.info(f"📡 Subscription {sub_id} STARTED: {xpath} (On-Change Mode)")
        
        last_val = None
        last_push_time = 0
        max_period = 30 # Force push every 30s even if unchanged
        
        while not stop_event.is_set():
            try:
                # 1. Read Sensor
                current_val = self._generate_value(xpath)
                now = time.time()
                
                # 2. Determine if we should push (On-Change Logic)
                should_push = False
                
                if last_val is None:
                    should_push = True # First sample
                elif (now - last_push_time) > max_period:
                    should_push = True # Heartbeat timeout
                else:
                    # Check dampening thresholds
                    if "temp" in xpath and abs(current_val - last_val) > 0.5: should_push = True
                    elif "volt" in xpath and abs(current_val - last_val) > 0.1: should_push = True
                    elif "fan" in xpath and abs(current_val - last_val) > 100: should_push = True
                    elif "errors" in xpath and current_val != last_val: should_push = True
                
                if should_push:
                    # 3. Build & Push Notification
                    payload = {
                        "ietf-push:notification": {
                            "subscription-id": sub_id,
                            "time": now,
                            "event_type": "on-change" if last_val else "sync",
                            "updates": [{"path": xpath, "value": current_val}]
                        },
                        "meta": {"hostname": "device-sim"}
                    }
                    requests.post(url, json=payload, timeout=2)
                    
                    last_val = current_val
                    last_push_time = now
                    logger.debug(f"Event Pushed: {xpath} = {current_val}")
                
            except Exception as e:
                logger.debug(f"Push failed: {e}")
                
            # Sleep for the check interval (sampling rate)
            time.sleep(check_interval)

    def apply(self, config: Dict[str, Any]) -> Dict[str, Any]:
        action = config.get("action")
        
        with self._lock:
            # --- CAPABILITY DISCOVERY ---
            if action == "discover":
                return {
                    "status": "success",
                    "ietf-yp-lite-capabilities": {
                        "supported-sensors": [
                            "/hardware/cpu/temp",
                            "/hardware/fan/speed",
                            "/hardware/psu/voltage",
                            "/interfaces/eth0/errors"
                        ],
                        "supported-modes": ["periodic", "on-change"],
                        "max-subscriptions": 10
                    }
                }

            # --- ESTABLISH SUBSCRIPTION ---
            elif action == "subscribe":
                sub_id = str(uuid.uuid4())[:8]
                xpath = config.get("xpath", "/hardware/cpu/temp")
                # In on-change mode, 'period' becomes 'sampling rate'
                period = float(config.get("period", 1)) 
                receiver = config.get("receiver")
                
                stop_event = threading.Event()
                t = threading.Thread(target=self._stream_loop, args=(sub_id, xpath, period, receiver, stop_event))
                t.daemon = True
                t.start()
                
                self._subscriptions[sub_id] = {"xpath": xpath, "stop_event": stop_event}
                return {"status": "success", "subscription-id": sub_id}

            elif action == "unsubscribe":
                sub_id = config.get("subscription-id")
                if sub_id in self._subscriptions:
                    self._subscriptions[sub_id]["stop_event"].set()
                    del self._subscriptions[sub_id]
                    return {"status": "success", "message": "Subscription terminated"}
                return {"status": "error", "message": "ID not found"}

        return {"status": "error", "message": "Unknown action"}

    def rollback(self, config):
        for sub in self._subscriptions.values(): sub["stop_event"].set()
        self._subscriptions.clear()
