import asyncio
from pathlib import Path

from ai_control.permissions import PermissionService
from ai_control.storage import Database


def test_permission_is_bound_and_one_use(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = Database(tmp_path / "state.db")
        await database.initialize()
        await database.execute(
            "INSERT INTO projects(id,name,path,runtime,agents_json,file_access) VALUES(?,?,?,?,?,?)",
            ("p", "Project", str(tmp_path), "linux", '["codex"]', "project_only"),
        )
        task_id = await database.execute(
            "INSERT INTO tasks(project_id,user_id,agent,status,checkout_path,prompt) VALUES(?,?,?,?,?,?)",
            ("p", 42, "codex", "running", str(tmp_path), "test"),
        )
        service = PermissionService(database)
        arguments = {"command": ["npm", "install"]}
        token = await service.create(task_id=task_id, user_id=42, action="command", arguments=arguments)
        assert not await service.consume(
            token,
            task_id=task_id,
            user_id=7,
            action="command",
            arguments=arguments,
            decision="allow_once",
        )
        assert await service.consume(
            token,
            task_id=task_id,
            user_id=42,
            action="command",
            arguments=arguments,
            decision="allow_once",
        )
        assert not await service.consume(
            token,
            task_id=task_id,
            user_id=42,
            action="command",
            arguments=arguments,
            decision="allow_once",
        )

    asyncio.run(scenario())
