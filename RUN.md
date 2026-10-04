# Запуск AI Control

Команды выполняются из каталога проекта, где лежит `config.yaml`. Одновременно может работать только один экземпляр с тем же каталогом данных. Если бот уже запущен службой, ручной старт завершится ошибкой блокировки: сначала остановите службу.

Остановка ручного запуска — `Ctrl+C` в том же терминале.

## Linux

```bash
cd /path/to/ai_control
.venv/bin/ai-control --config config.yaml run
```

С активированным окружением:

```bash
source .venv/bin/activate
ai-control --config config.yaml run
```

Если включён пользовательский systemd:

```bash
systemctl --user stop ai-control
systemctl --user start ai-control
systemctl --user status ai-control
```

## Windows

PowerShell:

```powershell
cd C:\path\to\ai_control
.\.venv\Scripts\ai-control.exe --config config.yaml run
```

С активированным окружением:

```powershell
.\.venv\Scripts\Activate.ps1
ai-control --config config.yaml run
```

Если включена задача планировщика, подставьте имя установки из `config.yaml` (`instance.name`):

```powershell
schtasks /End /TN "AI Control - <instance name>"
schtasks /Run /TN "AI Control - <instance name>"
schtasks /Query /TN "AI Control - <instance name>"
```
