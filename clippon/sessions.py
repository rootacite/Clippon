"""Which client is streaming which instance.

GameStream has one global "current game" per host, which is what makes a second
client refuse to start anything until the first one quits. The gateway answers
that field per client instead: a client's own session is the instance it
launched, and while that backend still reports the app running, other clients
are told nothing about it.

A record is dropped as soon as the backend says the app is gone, so a client
that dies without sending /cancel cannot hold an instance forever.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

from clippon.gateway import external_app_id


class Sessions:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_client: dict[str, str] = {}

    def remember(self, fingerprint: str | None, instance: str) -> None:
        if not fingerprint:
            return
        with self._lock:
            self._by_client[fingerprint] = instance

    def forget(self, fingerprint: str | None) -> None:
        if not fingerprint:
            return
        with self._lock:
            self._by_client.pop(fingerprint, None)

    def forget_instance(self, instance: str) -> int:
        """Drop every record for *instance*. Returns how many there were."""
        with self._lock:
            gone = [key for key, name in self._by_client.items() if name == instance]
            for key in gone:
                self._by_client.pop(key, None)
        return len(gone)

    def claimed(self, fingerprint: str | None) -> str | None:
        """The instance this client launched, whether or not it is still running."""
        if not fingerprint:
            return None
        with self._lock:
            return self._by_client.get(fingerprint)

    def active(self) -> bool:
        with self._lock:
            return bool(self._by_client)

    def live(self, busy: Mapping[str, int]) -> dict[str, tuple[str, int]]:
        """Records the backends still back, as fingerprint -> (instance, app id).

        *busy* maps an instance to the internal app id its backend reports.
        """
        found: dict[str, tuple[str, int]] = {}
        with self._lock:
            for fingerprint, instance in list(self._by_client.items()):
                internal = busy.get(instance)
                if internal is None:
                    self._by_client.pop(fingerprint, None)
                    continue
                found[fingerprint] = (instance, external_app_id(instance, internal))
        return found

    def hidden(self, busy: Mapping[str, int], fingerprint: str | None) -> set[str]:
        """Instances this client must not see, because someone else is streaming them."""
        return {
            instance
            for owner, (instance, _external) in self.live(busy).items()
            if owner != fingerprint
        }

    def holder(self, instance: str, busy: Mapping[str, int], fingerprint: str | None) -> str | None:
        """The other client streaming *instance*, if there is one."""
        for owner, (name, _external) in self.live(busy).items():
            if name == instance and owner != fingerprint:
                return owner
        return None
