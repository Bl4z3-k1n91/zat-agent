import subprocess, logging, os, time
from jinja2 import Template
from typing import Dict, Any
from core.base import BasePlugin

logger = logging.getLogger("ZatAgent.OSPF")

class OSPFPlugin(BasePlugin):
    OSPF_CLI_TEMPLATE = """configure terminal
router ospf
 router-id {{ router_id }}
 log-adjacency-changes
 exit
!
{% for iface in interfaces %}
interface {{ iface.name }}
 ip ospf area {{ iface.area }}
 {% if iface.cost %}ip ospf cost {{ iface.cost }}{% endif %}
 {% if iface.hello_interval %}ip ospf hello-interval {{ iface.hello_interval }}{% endif %}
 exit
!
{% endfor %}
end
write memory
"""
    @property
    def name(self): return "ospf"
    def validate(self, config): 
        if "router_id" not in config: raise ValueError("Missing router_id")
        return True

    def _ensure_daemon(self):
        if not os.path.exists("/etc/frr/daemons"): return
        try:
            with open("/etc/frr/daemons", "r") as f: c = f.read()
            if "ospfd=no" in c:
                with open("/etc/frr/daemons", "w") as f: f.write(c.replace("ospfd=no", "ospfd=yes"))
                # Only restart if we actually enabled the daemon
                subprocess.run(["systemctl", "restart", "frr"], check=False)
                time.sleep(1)
        except: pass

    def _clean(self, path):
        if not os.path.exists(path): return
        try:
            with open(path, "r") as f: lines = f.readlines()
            new = [l for l in lines if "! --- ZAT-AGENT" not in l and l.strip() != "end"]
            with open(path, "w") as f: f.writelines(new)
            subprocess.run(["chown", "frr:frr", path], check=False)
        except: pass

    def apply(self, config):
        config.setdefault("process_id", 1)
        self._ensure_daemon()
        self._clean("/etc/frr/frr.conf")
        
        # Use reload to avoid start-limit-hit
        try:
            subprocess.run(["systemctl", "reload", "frr"], check=True)
        except:
            time.sleep(1)
            subprocess.run(["systemctl", "restart", "frr"], check=True)
        
        cmd = Template(self.OSPF_CLI_TEMPLATE).render(config)
        subprocess.run(["vtysh"], input=cmd, text=True, check=True)
        return {"status": "success", "router_id": config['router_id']}

    def rollback(self, config): pass
