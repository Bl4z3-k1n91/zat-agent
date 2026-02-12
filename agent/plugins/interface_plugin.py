import subprocess, logging
from jinja2 import Template
from core.base import BasePlugin

class InterfacePlugin(BasePlugin):
    # Live CLI Template
    IFACE_TEMPLATE = """configure terminal
interface {{ name }}
 no ip address
 ip address {{ ip }}
 no shutdown
 exit
end
write memory
"""
    @property
    def name(self): return "interface"
    def validate(self, c): return "name" in c and "ip" in c
    def apply(self, config):
        try:
            # Render CLI commands
            cmd = Template(self.IFACE_TEMPLATE).render(config)
            
            # Push directly via vtysh (Live Injection)
            subprocess.run(["vtysh"], input=cmd, text=True, check=True)
            
            return {"status": "success", "iface": config['name']}
        except Exception as e: raise RuntimeError(f"Interface Config Failed: {str(e)}")
    def rollback(self, c): pass
