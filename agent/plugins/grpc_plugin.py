import grpc
import logging
import socket
import time
import random
import threading
from concurrent import futures
import sys
from core.base import BasePlugin

# --- SETUP LOGGING ---
logger = logging.getLogger("ZatAgent.gRPC")

# --- IMPORT PROTOBUF ---
# Ensure you run: python3 -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. zat.proto
try:
    import zat_pb2
    import zat_pb2_grpc
except ImportError:
    zat_pb2 = None
    zat_pb2_grpc = None
    logger.error("❌ Protobuf files missing! Please run: python3 -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. zat.proto")

class TelemetryServicer(zat_pb2_grpc.TelemetryServiceServicer):
    """
    Implements the gRPC Telemetry Service defined in zat.proto
    """
    def Subscribe(self, request, context):
        # Default duration to 1 hour if not specified
        duration = request.duration if request.duration > 0 else 3600
        logger.info(f"📡 Client Subscribed: {request.xpath} (Period: {request.period}s, Duration: {duration}s)")

        host = socket.gethostname()
        start_time = time.time()

        try:
            while context.is_active():
                # Check Time-To-Live
                if (time.time() - start_time) > duration:
                    logger.info("⏳ Subscription Expired (Duration Reached)")
                    break

                val = 0.0
                # --- HIGH VOLATILITY SIMULATION ---
                # 30% chance to generate an outlier to trigger "On-Change" events
                is_spike = random.random() < 0.3

                if "temp" in request.xpath:
                    # Normal: 45-55C | Spike: 85-95C
                    base = 90.0 if is_spike else 50.0
                    jitter = random.uniform(-5.0, 5.0)
                    val = round(base + jitter, 1)

                elif "fan" in request.xpath:
                    # Normal: 3000 | Spike: 7000 (Failure simulation)
                    base = 7000 if is_spike else 3000
                    jitter = random.randint(-200, 200)
                    val = float(base + jitter)

                elif "volt" in request.xpath:
                    # Normal: 12V | Spike: 10.5V (Undervolt)
                    base = 10.5 if is_spike else 12.0
                    jitter = random.uniform(-0.1, 0.1)
                    val = round(base + jitter, 2)

                elif "cpu" in request.xpath:
                    # Normal: 10-20% | Spike: 90-100%
                    base = 95.0 if is_spike else 15.0
                    jitter = random.uniform(-5.0, 5.0)
                    val = round(base + jitter, 1)

                # Yield binary data stream
                yield zat_pb2.TelemetryData(
                    timestamp=time.strftime("%H:%M:%S"),
                    host=host,
                    xpath=request.xpath,
                    value=val
                )

                time.sleep(request.period)
                
        except Exception as e:
            logger.info(f"gRPC Stream Closed: {e}")

    def GetCapabilities(self, request, context):
        return zat_pb2.CapabilitiesResponse(
            supported_sensors=[
                "/hardware/cpu/temp",
                "/hardware/fan/speed",
                "/hardware/psu/voltage",
                "/system/cpu/utilization"
            ],
            supported_modes=["periodic", "on-change"],
            max_subscriptions=10
        )

class GrpcPlugin(BasePlugin):
    def __init__(self):
        self._server = None
        self._thread = None

    @property
    def name(self): 
        return "grpc"

    def validate(self, config): 
        return True

    def _serve(self, port):
        if not zat_pb2:
            logger.error("❌ Cannot start gRPC: Protobuf files missing.")
            return

        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=5))
        zat_pb2_grpc.add_TelemetryServiceServicer_to_server(TelemetryServicer(), self._server)
        
        # Bind to all interfaces (IMPORTANT for EVE-NG connectivity)
        self._server.add_insecure_port(f'[::]:{port}')
        
        logger.info(f"🚀 gRPC Agent Plugin Active. Listening on port {port}...")
        self._server.start()
        self._server.wait_for_termination()

    def apply(self, config):
        if self._server: 
            return {"status": "already_running"}
            
        port = config.get("port", 50051)
        
        # Run the blocking server loop in a separate thread so main agent logic continues
        self._thread = threading.Thread(target=self._serve, args=(port,), daemon=True)
        self._thread.start()
        
        return {"status": "success", "port": port}

    def rollback(self, config):
        if self._server:
            logger.info("Stopping gRPC Server...")
            self._server.stop(0)
            self._server = None
        return {"status": "stopped"}
