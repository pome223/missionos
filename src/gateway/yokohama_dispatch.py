"""One Gateway process: share dispatch ownership from preparation through cleanup."""
import threading


class Dispatch:
    def __init__(self):
        self.lock = threading.RLock()
        self.owner = None

    def reserve(self, identity):
        with self.lock:
            if self.owner is not None:
                raise ValueError("別の横浜配送を準備または実行中です")
            self.owner = identity

    def release(self, identity):
        with self.lock:
            if self.owner == identity:
                self.owner = None


_registry: dict[str, Dispatch] = {}
_registry_lock = threading.Lock()


def shared_dispatch(store):
    key = str(store.db_path.resolve())
    with _registry_lock:
        return _registry.setdefault(key, Dispatch())
