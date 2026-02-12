import logging
import importlib
import pkgutil
from typing import Dict, Any
from core.base import BasePlugin

logger = logging.getLogger("ZatAgent.Core")

class PluginLoader:
    def __init__(self, package_name="plugins"):
        self.package_name = package_name

    def load_plugins(self) -> Dict[str, BasePlugin]:
        plugins = {}
        try:
            package = importlib.import_module(self.package_name)
            for _, name, _ in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
                module = importlib.import_module(name)
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if isinstance(attr, type) and issubclass(attr, BasePlugin) and attr is not BasePlugin:
                        instance = attr()
                        plugins[instance.name] = instance
                        logger.info(f"🔌 Loaded Plugin: {instance.name}")
        except Exception as e:
            logger.error(f"Plugin Load Error: {e}")
        return plugins

class TransactionManager:
    def __init__(self, plugins):
        self.plugins = plugins

    def execute(self, intent: Dict[str, Any]):
        for module, config in intent.items():
            if module not in self.plugins:
                return {"status": "error", "message": f"Unknown module: {module}"}
            try:
                self.plugins[module].validate(config)
            except Exception as e:
                return {"status": "error", "message": str(e)}
        
        applied = []
        try:
            results = {}
            for module, config in intent.items():
                res = self.plugins[module].apply(config)
                results[module] = res
                applied.append((module, config))
            return {"status": "success", "data": results}
        except Exception as e:
            logger.error(f"Transaction Failed: {e}")
            for module, config in reversed(applied):
                self.plugins[module].rollback(config)
            return {"status": "failed", "message": str(e)}
