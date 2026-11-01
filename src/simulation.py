"""Fault simulation: makes an ammeter misbehave on purpose, in a repeatable way.

FaultInjectingAmmeter wraps a real ammeter and looks just like one to the
rest of the code. It raises the same errors as the TCP client, so we can see
how the framework handles them.

A simulated network error is like a reading that failed even after the
client's retries. It's raised right away, without waiting for a real
timeout. To test timing, use the "slow" fault.

With the same seed you get the same faults every run, so a problem seen once
can be reproduced.
"""
import random
import time
from collections import Counter
from dataclasses import asdict
from typing import Any, Callable

from src.ammeter_client import Ammeter
from src.exceptions import AmmeterConnectionError, AmmeterTimeoutError, InvalidResponseError
from src.settings import FaultProfile


class FaultInjectingAmmeter:
    """Wraps an ammeter and adds the faults from a FaultProfile."""

    def __init__(
        self,
        inner: Ammeter,
        profile: FaultProfile,
        seed: int,
        *,
        profile_name: str = "custom",
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._inner = inner
        self._profile = profile
        self._profile_name = profile_name
        self._seed = seed
        self._sleep = sleep
        # Each ammeter gets its own generator, so they don't all fail together.
        # A string seed gives the same numbers on every run and every OS.
        self._random = random.Random(f"{seed}:{inner.name}")
        self.injected: Counter[str] = Counter()

    @property
    def name(self) -> str:
        return self._inner.name

    def measure(self) -> float:
        profile, rng = self._profile, self._random

        # One random number decides which failure happens, if any. This way
        # the three chances never overlap.
        draw = rng.random()
        threshold = profile.timeout_probability
        if draw < threshold:
            self.injected["timeout"] += 1
            raise AmmeterTimeoutError(f"{self.name}: simulated timeout")
        threshold += profile.disconnect_probability
        if draw < threshold:
            self.injected["disconnect"] += 1
            raise AmmeterConnectionError(f"{self.name}: simulated connection drop")
        threshold += profile.invalid_response_probability
        if draw < threshold:
            self.injected["invalid_response"] += 1
            raise InvalidResponseError(f"{self.name}: simulated invalid reply 'ERR#42'")

        # Draw these every time, even for faults that are off. Then turning
        # one fault on doesn't change when the others happen.
        slow = rng.random() < profile.slow_probability
        spike = rng.random() < profile.spike_probability
        noise = rng.gauss(0.0, 1.0)

        value = self._inner.measure()
        if slow:
            self.injected["slow"] += 1
            self._sleep(profile.slow_delay_seconds)
        if spike:
            self.injected["spike"] += 1
            value *= profile.spike_factor
        if profile.bias_relative:
            value *= 1 + profile.bias_relative
        if profile.noise_relative:
            value += noise * profile.noise_relative * abs(value)
        return value

    def describe(self) -> dict[str, Any]:
        """What we simulated, to save with the result."""
        return {
            "profile": self._profile_name,
            "seed": self._seed,
            "settings": asdict(self._profile),
            "injected": dict(sorted(self.injected.items())),
        }


def new_seed() -> int:
    """A random seed for when the config doesn't set one.

    It's saved with each result, so the run can still be repeated.
    """
    return random.SystemRandom().randrange(2**32)
