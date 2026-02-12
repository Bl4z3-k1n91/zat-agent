import uvicorn
import logging
import os
import subprocess
from fastapi import FastAPI, HTTPException, Body
from typing import Dict, Any
from core.engine import PluginLoader, TransactionManager

# CONFIGURATION
CONTROLLER_IP = os.getenv("ZAT_CONTROLLER", "10.152.171.244")
REPORTING_SERVER_URL = f"http://{CONTROLLER_IP}:5000/telemetry"
HEARTBEAT_INTERVAL = 10

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ZatAgent")
app = FastAPI(title="Zat-Agent", version="2.2.0")
state = {"tx_manager": None}

@app.on_event("startup")
async def startup():
    logger.info("🚀 Booting...")
    loader = PluginLoader(package_name="plugins")
    plugins = loader.load_plugins()
    state["tx_manager"] = TransactionManager(plugins)
    logger.info(f"✅ Ready. Modules: {list(plugins.keys())}")

    if "discovery" in plugins:
        try:
            plugins["discovery"].apply({
                "controller_url": REPORTING_SERVER_URL,
                "interval": HEARTBEAT_INTERVAL
            })
            logger.info(f"📡 Telemetry -> {REPORTING_SERVER_URL}")
        except Exception as e: logger.error(f"Telemetry failed: {e}")

@app.post("/configure")
async def configure_node(intent: Dict[str, Any] = Body(...)):
    if not state["tx_manager"]: raise HTTPException(500, "Initializing...")
    result = state["tx_manager"].execute(intent)
    if result["status"] == "success": return result
    else: raise HTTPException(400, result)

@app.get("/config")
def get_running_config():
    """Reads the current FRR configuration"""
    try:
        result = subprocess.run(["vtysh", "-c", "show running-config"], capture_output=True, text=True)
        if result.returncode != 0: raise HTTPException(500, "Failed to read config")
        return {"hostname": os.uname()[1], "config": result.stdout}
    except Exception as e: raise HTTPException(500, str(e))

@app.get("/health")
def health(): return {"status": "healthy", "agent_version": "2.2.0"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
