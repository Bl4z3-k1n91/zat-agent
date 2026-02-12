import threading, time, socket, psutil, requests, subprocess, json
from core.base import BasePlugin

class DiscoveryPlugin(BasePlugin):
    def __init__(self):
        self._stop = threading.Event()
    @property
    def name(self): return "discovery"
    def validate(self, c): return "controller_url" in c
    
    def _j(self, cmd):
        try: 
            # FRR 7.x json output check
            res = subprocess.run(cmd, capture_output=True, text=True)
            return json.loads(res.stdout)
        except: return {}

    def _lldp(self):
        try:
            # Requires lldpd installed
            res = subprocess.run(["lldpcli", "-f", "json", "show", "neighbors"], capture_output=True, text=True)
            data = json.loads(res.stdout)
            return data.get("lldp", {}).get("interface", [])
        except: return []

    def _stats(self):
        return {
            "meta": {"hostname": socket.gethostname(), "ts": time.time()},
            "system": {"cpu": psutil.cpu_percent(), "ram": psutil.virtual_memory().percent, "uptime": time.time()-psutil.boot_time()},
            "network": {
                "routes": self._j(["vtysh", "-c", "show ip route json"]),
                "ospf_neighbors": self._j(["vtysh", "-c", "show ip ospf neighbor json"]),
                "lldp_neighbors": self._lldp()
            }
        }

    def _loop(self, url, iv):
        while not self._stop.is_set():
            try: requests.post(url, json=self._stats(), timeout=5)
            except: pass
            time.sleep(iv)

    def apply(self, c):
        self._stop.set()
        self._stop.clear()
        threading.Thread(target=self._loop, args=(c["controller_url"], c.get("interval", 10)), daemon=True).start()
        return {"status": "telemetry_started"}
    def rollback(self, c): self._stop.set()
