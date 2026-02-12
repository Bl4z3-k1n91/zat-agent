import subprocess
import os
import logging
from core.base import BasePlugin

logger = logging.getLogger("ZatAgent.IPFIX")

class IPFIXPlugin(BasePlugin):
    @property
    def name(self): return "ipfix"
    def validate(self, config): return "collector" in config

    def apply(self, config):
        # 1. Kill previous instances to avoid conflict
        subprocess.run(["pkill", "softflowd"], check=False)
        
        results = {}
        for iface in config["interfaces"]:
            # Check if interface exists
            if not os.path.exists(f"/sys/class/net/{iface}"):
                results[iface] = "interface_not_found"
                continue

            # Command: softflowd -i ens3 -n 1.2.3.4:2055 -v 10 -t maxlife=30 -t expint=10
            # -t maxlife=30: Force export every 30s
            # -t expint=10: Check for expired flows every 10s
            # -v 10: IPFIX
            cmd = [
                "softflowd", 
                "-i", iface, 
                "-n", f"{config['collector']}:{config.get('port', 2055)}", 
                "-v", "10",
                "-t", "maxlife=30",
                "-t", "expint=10"
            ]
            
            try:
                subprocess.run(cmd, check=True)
                results[iface] = "active"
                logger.info(f"IPFIX started on {iface} -> {config['collector']}")
            except Exception as e:
                results[iface] = str(e)
                logger.error(f"IPFIX failed on {iface}: {e}")

        return {"status": "success", "details": results}

    def rollback(self, config): subprocess.run(["pkill", "softflowd"])
