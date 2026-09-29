"""Pure DSP math for the native audio path.

No I/O, no threads, no Python-object allocation in the inner loops beyond
what numpy already does: every function takes a float32 block and returns a
float32 block, which is what the real-time callback needs to stay
deterministic. The biquad implementation is transposed direct form II with
pre-computed coefficients - coefficients are recomputed only when a parameter
*changes*, never per block.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

SAMPLE_FORMAT = "float32"


def db_to_gain(db: float) -> float:
    """Decibels to linear gain; ``-inf``/very negative mutes."""
    if db <= -120.0:
        return 0.0
    return float(10.0 ** (db / 20.0))


@dataclass(frozen=True)
class BiquadCoeffs:
    """Transposed direct form II coefficients (normalized so a0 == 1)."""

    b0: float
    b1: float
    b2: float
    a1: float
    a2: float


def low_shelf_coeffs(sample_rate: float, freq: float, gain_db: float, q: float = 0.707) -> BiquadCoeffs:
    """RBJ cookbook low-shelf: boosts/cuts everything below ``freq``."""
    A = 10.0 ** (gain_db / 40.0)  # noqa: N806 - follows the RBJ paper's naming
    w0 = 2.0 * math.pi * min(freq, sample_rate * 0.45) / sample_rate
    alpha = math.sin(w0) / (2.0 * q)
    cos_w0 = math.cos(w0)
    sqrt_A_alpha = 2.0 * math.sqrt(A) * alpha

    a0 = (A + 1.0) + (A - 1.0) * cos_w0 + sqrt_A_alpha
    b0 = A * ((A + 1.0) - (A - 1.0) * cos_w0 + sqrt_A_alpha)
    b1 = 2.0 * A * ((A - 1.0) - (A + 1.0) * cos_w0)
    b2 = A * ((A + 1.0) - (A - 1.0) * cos_w0 - sqrt_A_alpha)
    a1 = -2.0 * ((A - 1.0) + (A + 1.0) * cos_w0)
    a2 = (A + 1.0) + (A - 1.0) * cos_w0 - sqrt_A_alpha
    return BiquadCoeffs(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


def high_shelf_coeffs(sample_rate: float, freq: float, gain_db: float, q: float = 0.707) -> BiquadCoeffs:
    """RBJ cookbook high-shelf: boosts/cuts everything above ``freq``."""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * min(freq, sample_rate * 0.45) / sample_rate
    alpha = math.sin(w0) / (2.0 * q)
    cos_w0 = math.cos(w0)
    sqrt_A_alpha = 2.0 * math.sqrt(A) * alpha

    a0 = (A + 1.0) - (A - 1.0) * cos_w0 + sqrt_A_alpha
    b0 = A * ((A + 1.0) + (A - 1.0) * cos_w0 + sqrt_A_alpha)
    b1 = -2.0 * A * ((A - 1.0) + (A + 1.0) * cos_w0)
    b2 = A * ((A + 1.0) + (A - 1.0) * cos_w0 - sqrt_A_alpha)
    a1 = 2.0 * ((A - 1.0) - (A + 1.0) * cos_w0)
    a2 = (A + 1.0) - (A - 1.0) * cos_w0 - sqrt_A_alpha
    return BiquadCoeffs(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


def peaking_coeffs(sample_rate: float, freq: float, gain_db: float, q: float = 1.0) -> BiquadCoeffs:
    """RBJ cookbook peaking EQ: boosts/cuts a band around ``freq``."""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * min(freq, sample_rate * 0.45) / sample_rate
    alpha = math.sin(w0) / (2.0 * q)
    cos_w0 = math.cos(w0)

    a0 = 1.0 + alpha / A
    b0 = 1.0 + alpha * A
    b1 = -2.0 * cos_w0
    b2 = 1.0 - alpha * A
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha / A
    return BiquadCoeffs(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


class Biquad:
    """One channel of transposed direct form II filtering.

    State lives in two floats per instance. A stereo pair is two instances,
    which keeps the callback allocation-free: filtering is pure arithmetic on
    pre-existing buffers.
    """

    __slots__ = ("_coeffs", "z1", "z2")

    def __init__(self, coeffs: BiquadCoeffs) -> None:
        self._coeffs = coeffs
        self.z1 = 0.0
        self.z2 = 0.0

    def set_coeffs(self, coeffs: BiquadCoeffs) -> None:
        # Keep filter state across parameter changes: an audible click is
        # worse than a short transition through the old response.
        self._coeffs = coeffs

    @property
    def coeffs(self) -> BiquadCoeffs:
        return self._coeffs

    def process(self, block: np.ndarray) -> np.ndarray:
        """Filter a 1-D float32 block in place and return it."""
        c = self._coeffs
        z1 = self.z1
        z2 = self.z2
        b0, b1, b2, a1, a2 = c.b0, c.b1, c.b2, c.a1, c.a2
        # The loop over a numpy buffer in Python is the one weak point; the
        # engine converts to numpy-native ops where it matters. For blocks of
        # 480-2048 frames the cost is still far below real time on any modern
        # CPU (measured in the test suite).
        for i in range(block.shape[0]):
            x = float(block[i])
            y = b0 * x + z1
            z1 = b1 * x - a1 * y + z2
            z2 = b2 * x - a2 * y
            block[i] = y
        self.z1 = z1
        self.z2 = z2
        return block


class StereoBiquad:
    """A left/right pair of biquads sharing coefficients."""

    __slots__ = ("left", "right")

    def __init__(self, coeffs: BiquadCoeffs) -> None:
        self.left = Biquad(coeffs)
        self.right = Biquad(coeffs)

    def set_coeffs(self, coeffs: BiquadCoeffs) -> None:
        self.left.set_coeffs(coeffs)
        self.right.set_coeffs(coeffs)

    def process(self, block: np.ndarray) -> np.ndarray:
        """Filter an (frames, 2) interleaved block in place."""
        self.left.process(block[:, 0])
        self.right.process(block[:, 1])
        return block


def apply_gain(block: np.ndarray, gain: float) -> np.ndarray:
    """Multiply a block by a linear gain, in place."""
    if gain != 1.0:
        block *= gain
    np.clip(block, -1.0, 1.0, out=block)
    return block


def spectrum_peak_db(input_block: np.ndarray, output_block: np.ndarray,
                     sample_rate: float, freq_hz: float,
                     bandwidth: float = 4.0) -> float:
    """Measured level change at ``freq_hz`` between two blocks, in dB.

    This is the objective verification hook: feed the same signal through the
    DSP with effects off and on, and this function quantifies the difference
    at a specific frequency. It is deliberately part of the DSP module so the
    "did the audio actually change" question has a first-class answer.
    """

    def magnitude_db(block: np.ndarray) -> float:
        # Mono or multi-channel: average the channels into one signal so a
        # stereo loopback capture can be measured directly.
        if block.ndim > 1:
            block = block.mean(axis=1)
        windowed = block * np.hanning(block.shape[0])
        spectrum = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(block.shape[0], 1.0 / sample_rate)
        # Average energy in a small band around the target frequency.
        mask = (freqs >= freq_hz / bandwidth) & (freqs <= freq_hz * bandwidth)
        if not mask.any():
            return -np.inf
        energy = float(np.mean(spectrum[mask] ** 2))
        return 10.0 * math.log10(max(energy, 1e-20))

    return magnitude_db(output_block) - magnitude_db(input_block)
