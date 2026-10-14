# Duyu-small (https://huggingface.co/KaanAydinli/Duyu-small, 1f1c327), duyu_small/__init__.py; Apache-2.0 (LICENSE).
# Değiştirilmeden alındı; model dosyaları scripts/fetch_models.py ile indirilir ({kök}/asr/duyu-small).
"""Duyu-small: a ~19 M-parameter Turkish + English speech recognizer for CPUs
and edge devices (Conformer encoder with shared refinement passes and a
sparse acoustic memory, CTC + attention decoder), shipped as ONNX graphs at
fp32 / int8 / 4-bit with a numpy decoder. See README.md."""

from .model import DuyuSmall, QUANTS  # noqa: F401

__version__ = "1.0.0"
