import asyncio
from pathlib import Path

from ai_control.git import GitService


def test_diff_includes_untracked_files(tmp_path: Path) -> None:
    async def scenario() -> None:
        git = GitService()
        await git.run(tmp_path, "init")
        (tmp_path / "new_file.py").write_text('print("hello")\n', encoding="utf-8")

        value = await git.diff(tmp_path)

        assert "new file mode" in value
        assert "+++ b/new_file.py" in value
        assert '+print("hello")' in value

    asyncio.run(scenario())
