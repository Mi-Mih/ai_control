from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass(slots=True)
class GitResult:
    stdout: str
    stderr: str


class GitService:
    def __init__(self, executable: str = "git", timeout: float = 30) -> None:
        self.executable = executable
        self.timeout = timeout

    async def run(self, cwd: Path, *args: str, ok_returncodes: tuple[int, ...] = (0,)) -> GitResult:
        process = await asyncio.create_subprocess_exec(
            self.executable,
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise GitError("git command timed out") from None
        result = GitResult(stdout.decode(errors="replace"), stderr.decode(errors="replace"))
        if process.returncode not in ok_returncodes:
            raise GitError(result.stderr.strip() or f"git exited with {process.returncode}")
        return result

    async def is_repository(self, cwd: Path) -> bool:
        try:
            result = await self.run(cwd, "rev-parse", "--is-inside-work-tree")
            return result.stdout.strip() == "true"
        except (GitError, OSError):
            return False

    async def status(self, cwd: Path) -> str:
        return (await self.run(cwd, "status", "--short", "--branch")).stdout

    async def diff(self, cwd: Path, max_chars: int = 100_000) -> str:
        chunks = [(await self.run(cwd, "diff", "--no-ext-diff", "--no-color")).stdout]
        untracked = await self.run(cwd, "ls-files", "--others", "--exclude-standard", "-z")
        for relative_path in untracked.stdout.split("\x00"):
            if not relative_path or sum(len(chunk) for chunk in chunks) >= max_chars:
                continue
            result = await self.run(
                cwd,
                "diff",
                "--no-index",
                "--no-ext-diff",
                "--no-color",
                "--",
                "/dev/null",
                relative_path,
                ok_returncodes=(0, 1),
            )
            chunks.append(result.stdout)
        return "".join(chunks)[:max_chars]

    async def changed_files(self, cwd: Path, limit: int = 200) -> list[Path]:
        result = await self.run(cwd, "ls-files", "--modified", "--others", "--exclude-standard", "-z")
        root = cwd.resolve()
        files: list[Path] = []
        for raw in result.stdout.split("\x00"):
            if not raw:
                continue
            candidate = (root / raw).resolve(strict=False)
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.is_file():
                files.append(candidate)
            if len(files) >= limit:
                break
        return files

    async def create_worktree(self, repo: Path, target: Path, branch: str) -> None:
        if not re.fullmatch(r"ai-control/[a-z0-9][a-z0-9._-]{0,80}", branch):
            raise GitError("invalid managed branch name")
        target.parent.mkdir(parents=True, exist_ok=True)
        await self.run(repo, "worktree", "add", "-b", branch, str(target), "HEAD")

    async def remove_worktree(self, repo: Path, target: Path) -> None:
        await self.run(repo, "worktree", "remove", str(target))

    async def create_wsl_worktree(self, distribution: str, repo: str, target: str, branch: str) -> None:
        if not re.fullmatch(r"ai-control/[a-z0-9][a-z0-9._-]{0,80}", branch):
            raise GitError("invalid managed branch name")
        process = await asyncio.create_subprocess_exec(
            "wsl.exe",
            "--distribution",
            distribution,
            "--cd",
            repo,
            "--exec",
            "git",
            "worktree",
            "add",
            "-b",
            branch,
            target,
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise GitError("WSL git command timed out") from None
        if process.returncode:
            raise GitError(stderr.decode(errors="replace").strip() or stdout.decode(errors="replace"))
