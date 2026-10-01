from pathlib import Path

import pytest

from ai_control.core.instance_lock import InstanceAlreadyRunningError, InstanceLock


def test_instance_lock_rejects_second_process_and_releases(tmp_path: Path) -> None:
    path = tmp_path / "ai-control.lock"

    with InstanceLock(path):
        assert path.read_text(encoding="ascii").isdigit()
        with pytest.raises(InstanceAlreadyRunningError):
            with InstanceLock(path):
                pass

    with InstanceLock(path):
        assert path.read_text(encoding="ascii").isdigit()
