from abc import ABC, abstractmethod
from typing import Dict, Any

class BasePlugin(ABC):
    @property
    @abstractmethod
    def name(self) -> str: pass
    @abstractmethod
    def validate(self, config: Dict[str, Any]) -> bool: pass
    @abstractmethod
    def apply(self, config: Dict[str, Any]) -> Dict[str, Any]: pass
    @abstractmethod
    def rollback(self, config: Dict[str, Any]): pass
