# Запуск на Windows без Docker и WSL

Проект можно запускать напрямую через Python. Docker Desktop и WSL для этого
не нужны: данные хранятся в SQLite-файле `data/tenders.db`, а все зависимости
ставятся через `pip`.

## Требования

- Windows 10/11
- Python 3.11 или новее
- Доступ к интернету для сбора тендеров

Проверьте Python:

```powershell
py -3 --version
```

Если команда не найдена, установите Python с https://www.python.org/downloads/
и включите опцию `Add python.exe to PATH`.

## Быстрый старт

Откройте PowerShell в корне проекта и выполните:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
```

Скрипт создаст `.venv`, обновит `pip`, установит зависимости и создаст папки
`data`, `logs`, `data\exports`.

После установки можно запускать команды так:

```powershell
scripts\run_windows.ps1 stats
scripts\run_windows.ps1 collect-bico -Search "маркетинговая платформа" -MaxPages 3
scripts\run_windows.ps1 export-excel
```

## Доступные команды

```powershell
scripts\run_windows.ps1 stats
scripts\run_windows.ps1 last -Count 30
scripts\run_windows.ps1 search -Query "IMEI"
scripts\run_windows.ps1 collect-bico -Search "IMEI" -MaxPages 5
scripts\run_windows.ps1 collect-bico-all -MaxPages 2
scripts\run_windows.ps1 filter
scripts\run_windows.ps1 export-csv
scripts\run_windows.ps1 export-excel
scripts\run_windows.ps1 export-html
```

Для произвольной команды Python используйте виртуальное окружение напрямую:

```powershell
.\.venv\Scripts\python.exe -m src.viewer --stats
.\.venv\Scripts\python.exe -m src.collector_bico --search "IMEI" --max-pages 5
```

## Если PowerShell запрещает запуск скриптов

Разовый запуск с обходом политики:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
powershell -ExecutionPolicy Bypass -File scripts\run_windows.ps1 stats
```

## Что делать с Docker

Папка `docker/` остаётся рабочей для тех, у кого Docker Desktop настроен, но
для Windows-коллеги с ошибкой `WSL needs updating` этот путь можно не
использовать. Запускайте проект через локальный Python по инструкции выше.
