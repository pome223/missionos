"""Arrival-aware synthetic confirmation and per-attempt anomaly evidence.

The channel observes the plant; the detector reads only arrived receipts. Neither
an acknowledgement nor a visual finding can increment confirmed inventory.
All delays below are registered simulator test settings, not flight hardware.
"""
from copy import deepcopy
from hashlib import sha256
import json
import math


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid_confirmation_time")
    return float(value)


class ConfirmationChannel:
    """A bounded-latency counter with an independently sampled fresh confirmation.

    The first attempt's receipt may be delayed or absent. Subsequent samples
    remain behind that arrival barrier, so later samples cannot accidentally
    reveal the withheld count. Once the barrier clears, periodic samples have
    the ordinary transport latency. Delayed old samples remain old evidence.
    """

    def __init__(self, run_id, *, first_delay_s=.5, latency_s=.5, cadence_s=.5):
        if first_delay_s is not None:
            _time(first_delay_s)
        self.run_id = run_id
        self.first_delay_s, self.latency_s, self.cadence_s = first_delay_s, _time(latency_s), _time(cadence_s)
        if cadence_s <= 0:
            raise ValueError("invalid_confirmation_cadence")
        self.attempts, self.receipts, self.rejections, self.pending = [], [], [], []
        self.latest, self.next_sample_s, self.sequence, self.clock = None, 0., 0, None
        self.barrier_s, self.current_attempt = None, None

    def attempt(self, now, physical_count, *, acknowledged=True):
        now = _time(now)
        if type(physical_count) is not int or physical_count < 0 or type(acknowledged) is not bool:
            raise ValueError("invalid_release_attempt")
        if self.current_attempt and not self.covers_attempt(now):
            raise ValueError("unconfirmed_previous_release_attempt")
        token = "a-" + digest([self.run_id, len(self.attempts)+1])[:20]
        before = self.latest["confirmed_count"] if self.latest else 0
        attempt = {"release_attempt_id": token, "source_time_s": now,
                   "acknowledged": acknowledged, "expected_count": before+1}
        self.attempts.append(attempt)
        self.current_attempt = attempt
        delay = self.first_delay_s if len(self.attempts) == 1 else self.latency_s
        self.barrier_s = None if delay is None else now+delay
        if delay is not None:
            self._sample(now, physical_count, self.barrier_s)
        self.next_sample_s = now+self.cadence_s
        return deepcopy(attempt)

    def _sample(self, now, physical_count, available):
        self.sequence += 1
        row = {"schema": "missionos.starship_counter_receipt.v1", "run_id": self.run_id,
               "source_time_s": now, "available_time_s": available, "source_sequence": self.sequence,
               "release_attempt_id": self.current_attempt["release_attempt_id"] if self.current_attempt else None,
               "confirmed_count": physical_count}
        row["receipt_sha256"] = digest(row)
        self.pending.append(row)

    def observe(self, now, physical_count):
        now = _time(now)
        if self.clock is not None and now < self.clock:
            raise ValueError("confirmation_clock_reversed")
        if type(physical_count) is not int or not 0 <= physical_count <= 26:
            raise ValueError("invalid_confirmation_count")
        self.clock = now
        if now+1e-9 >= self.next_sample_s:
            if self.current_attempt is None or self.barrier_s is not None and now+1e-9 >= self.barrier_s:
                self._sample(now, physical_count, now+self.latency_s)
            self.next_sample_s = now+self.cadence_s
        ready = [r for r in self.pending if r["available_time_s"] <= now+1e-9]
        self.pending = [r for r in self.pending if r["available_time_s"] > now+1e-9]
        for receipt in sorted(ready, key=lambda r: r["source_sequence"]):
            self.accept(receipt, now)
        return deepcopy(self.latest)

    def accept(self, receipt, now):
        """Validate arrival/source ordering before changing confirmed inventory."""
        now = _time(now)
        expected = {"schema", "run_id", "source_time_s", "available_time_s", "source_sequence",
                    "release_attempt_id", "confirmed_count", "receipt_sha256"}
        if (type(receipt) is not dict or set(receipt) != expected
                or receipt["schema"] != "missionos.starship_counter_receipt.v1"
                or receipt["run_id"] != self.run_id
                or receipt["receipt_sha256"] != digest({k:v for k,v in receipt.items() if k != "receipt_sha256"})
                or not 0 <= _time(receipt["source_time_s"]) <= _time(receipt["available_time_s"]) <= now+1e-9
                or type(receipt["source_sequence"]) is not int or receipt["source_sequence"] < 1
                or type(receipt["confirmed_count"]) is not int or not 0 <= receipt["confirmed_count"] <= 26):
            raise ValueError("invalid_counter_receipt")
        if self.latest and (receipt["source_sequence"] <= self.latest["source_sequence"]
                            or receipt["source_time_s"] < self.latest["source_time_s"]):
            self.rejections.append({"receipt_sha256": receipt["receipt_sha256"], "reason": "duplicate_or_reordered"})
            return False
        if receipt["source_sequence"] != (self.latest["source_sequence"]+1 if self.latest else 1):
            raise ValueError("counter_source_gap")
        token = receipt["release_attempt_id"]
        if token is not None:
            matches = [a for a in self.attempts if a["release_attempt_id"] == token]
            if len(matches) != 1 or receipt["source_time_s"] < matches[0]["source_time_s"]:
                raise ValueError("counter_attempt_binding")
        elif self.attempts and receipt["source_time_s"] > self.attempts[0]["source_time_s"]:
            raise ValueError("counter_attempt_binding")
        if self.latest and not self.latest["confirmed_count"] <= receipt["confirmed_count"] <= self.latest["confirmed_count"]+1:
            raise ValueError("counter_inventory_gap")
        self.latest = deepcopy(receipt)
        self.receipts.append(deepcopy(receipt))
        return True

    @property
    def count(self):
        return self.latest["confirmed_count"] if self.latest else 0

    def covers_attempt(self, now, *, max_age_s=None, expected_increment=False):
        if self.latest is None:
            return False
        if self.current_attempt and (self.latest["release_attempt_id"] != self.current_attempt["release_attempt_id"]
                or self.latest["source_time_s"] < self.current_attempt["source_time_s"]):
            return False
        if max_age_s is not None and now-self.latest["source_time_s"] > max_age_s+1e-9:
            return False
        return not expected_increment or self.current_attempt is None or self.count == self.current_attempt["expected_count"]


class ReleaseConfirmationDetector:
    def __init__(self, run_id, *, confirmation_timeout_s=2., authority_sha256):
        self.run_id, self.authority_sha256 = run_id, authority_sha256
        self.timeout_s = _time(confirmation_timeout_s)
        if self.timeout_s <= 0:
            raise ValueError("invalid_confirmation_timeout")
        self.events, self.seen = [], set()
        self.config_sha256 = digest({"confirmation_timeout_s": self.timeout_s})

    def poll(self, now, attempt, receipt, *, generation, episode_deadline_s):
        now = _time(now)
        if not attempt or not attempt["acknowledged"]:
            return None
        token = attempt["release_attempt_id"]
        if token in self.seen or now < attempt["source_time_s"]+self.timeout_s:
            return None
        if (receipt and receipt["available_time_s"] <= now and receipt["release_attempt_id"] == token
                and receipt["source_time_s"] >= attempt["source_time_s"]
                and receipt["confirmed_count"] == attempt["expected_count"]):
            return None
        self.seen.add(token)
        identity = digest([self.run_id, token, "release_confirmation_timeout"])
        event = {"schema": "missionos.starship_anomaly_event.v1", "event_id": "e-"+identity[:20],
                 "episode_id": "ep-"+identity[20:40], "run_id": self.run_id, "vehicle_id": "ship",
                 "release_attempt_id": token, "metric_id": "release_confirmation_timeout",
                 "detector_version": "arrival_confirmation_v1", "detector_config_sha256": self.config_sha256,
                 "source_window_start_s": attempt["source_time_s"], "source_window_end_s": now,
                 "detected_at_sim_s": now, "telemetry_evidence_sha256": digest({"attempt": attempt, "receipt": receipt}),
                 "observed_value": receipt["confirmed_count"] if receipt else None,
                 "expected_band": [attempt["expected_count"], attempt["expected_count"]], "unit": "payload_count",
                 "state_generation": generation, "authority_sha256": self.authority_sha256,
                 "episode_deadline_sim_s": episode_deadline_s}
        self.events.append(event)
        return deepcopy(event)
