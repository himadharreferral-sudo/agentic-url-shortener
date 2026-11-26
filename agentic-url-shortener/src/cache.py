from collections import OrderedDict
import threading
from typing import Any, Optional


class LRUCache:
    def __init__(self, capacity: int = 10000):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._items: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._items:
                return None
            value = self._items.pop(key)
            self._items[key] = value
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if key in self._items:
                self._items.pop(key)
            self._items[key] = value
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)

    def remove(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def snapshot(self) -> dict:
        with self._lock:
            return {"entries": len(self._items), "capacity": self.capacity}
