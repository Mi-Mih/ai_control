import os

from ai_control.platform.base import PlatformAdapter
from ai_control.platform.unix import UnixPlatform
from ai_control.platform.windows import WindowsPlatform


def current_platform() -> PlatformAdapter:
    return WindowsPlatform() if os.name == "nt" else UnixPlatform()


__all__ = ["PlatformAdapter", "current_platform"]
