# ChestMemory — Automatic Fortnite Opened Chest Counter (Windows)

Полноценное нативное Windows-приложение на **Python 3.12+ / PySide6 / SQLite / OpenCV / Watchdog / PyInstaller** для автоматического подсчёта **открытых сундуков** в Fortnite и ведения долговременной статистики.

---

## Безопасность (100% EAC-Safe)

ChestMemory **не вмешивается** в игру:
- Не открывает процесс `FortniteClient-Win64-Shipping.exe` и не читает RAM.
- Не использует DLL-инжекты, драйверы или игровые хуки.
- Не модифицирует игровые файлы.
- Работает исключительно с локальными файлами `.replay` на диске (`%LOCALAPPDATA%\FortniteGame\Saved\Demos`) и/или независимым анализом экранных кадров через OpenCV + `mss`.

---

## Исследование формата Fortnite `.replay` и архитектура Chest Detector

### Как устроен `.replay` файл Fortnite
1. **Контейнер `FLocalFileReplayArchive`**:
   - Сигнатура файла (Magic): `0x1CA2E27F` (little-endian `uint32`).
   - Метаданные: `FileVersion`, `LengthInMS`, `NetworkVersion`, `Changelist`, `FriendlyName` (`FString`), `IsLive` (флаг активной записи), `Timestamp` (Windows FILETIME / .NET ticks), `IsCompressed`, `IsEncrypted`, `EncryptionKey`.
   - Чанки: `Header (0)`, `ReplayData (1)`, `Checkpoint (2)`, `Event (3)`.
2. **Где хранятся открытия сундуков**:
   - Высокоуровневые чанки `Event` содержат `AthenaMatchStats` (убийства, точность, урон, материалы) и `AthenaMatchTeamStats` (место, всего игроков), **но НЕ содержат готового счётчика сундуков**.
   - События открытия сундуков находятся внутри сетевых репликационных пакетов (`ReplayData` / `Checkpoint`) в свойствах акторов-контейнеров (`Tiered_Chest*`, `Apollo_TreasureChest*`, `Athena_Chest*`, `Helios_Chest*` с UAID-суффиксами):
     - `bAlreadySearched` (переход `False -> True`) — отличает реальное открытие от закрытого или уже ранее открытого сундука.
     - `SearchAnimationCount` (инкремент) — корректно учитывает повторно наполненные сундуки.
     - `SearchingPawn` — позволяет отфильтровать сундуки, открытые локальным игроком, от сундуков врагов в зоне видимости (PVS).
3. **Реальные ограничения `.replay`**:
   - Сжатие **Oodle** (`oo2core_*_win64.dll`) и/или **AES-256-ECB** шифрование.
   - Ограничение PVS-пузыря: в реплее пишутся только акторы в радиусе видимости клиента.
   - Epic Games периодически меняет битовую разметку `NetFieldExports` между сезонами UE5.

### Гибридная архитектура ChestMemory
Чтобы программа работала надёжно и **никогда не подменяла данные фиктивными цифрами**:
1. **Встроенный бинарный парсер (`detector/replay_parser.py` + `detector/replay_analyzer.py`)**:
   - Парсит `FLocalFileReplayArchive`, `FNetworkDemoHeader`, `AthenaMatchStats`, `AthenaMatchTeamStats`, `IsLive`, AES-256-ECB (`cryptography`), Oodle (`oo2core_*_win64.dll` через `ctypes`, загружаемую как автономную библиотеку) и zlib.
   - Нормализует UAID-имена классов (`Tiered_Chest_Athena_C_UAID_... -> Tiered_Chest_Athena_C`), исключает ящики с патронами и прочие не-сундуки.
   - Поддерживает **External Replay CLI Bridge** (например, C# `FortniteReplayReader` / `ChestCounter` CLI в JSON-формате) для мгновенной адаптации под свежие патчи UE5.
   - **Жёсткое правило достоверности**: если реплей ещё пишется (`IsLive=True`), повреждён, зашифрован или не поддаётся декодированию — программа **не записывает `0` как достоверный матч**, а фиксирует понятную ошибку в таблице `failed_analyses` и показывает статус в интерфейсе.
2. **Независимый Visual CV детектор (`detector/visual_detector.py`)**:
   - Использует **4-стадийный временной автомат (`ChestOpeningFSM`)** на OpenCV + `mss`.
   - Считает **именно момент завершения открытия сундука**, а не просто сундуки в кадре:
     - Обнаружение подсказки взаимодействия (`[E] Search` / `Обыскать`).
     - Отслеживание монотонного заполнения радиального кольца прогресса вокруг прицела (`0% -> >=78%`). Отмена поиска на 40% сбрасывает автомат в `IDLE` (0 сундуков).
     - Подтверждение вспышкой золотого лута и/или XP-баннером + cooldown `1.8s` против двойного счёта.
   - Работает как в реальном времени (`LiveVisualMonitor`), так и на записанных видеофайлах (`.mp4` / `.mkv`).

---

## Установка и запуск на Windows 10 / 11

### 1. Запуск из исходников
```bat
cd ChestMemory
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

### 2. CLI-режим (диагностика и анализ файлов)
```bat
python main.py --stats
python main.py --scan
python main.py --analyze "C:\Users\%USERNAME%\AppData\Local\FortniteGame\Saved\Demos\match.replay"
python main.py --analyze "C:\Videos\fortnite_match.mp4"
python main.py --backup
```

### 3. Запуск тестов
```bat
pytest
```
или стандартным `unittest`:
```bat
python -m unittest discover -s tests -v
```

### 4. Сборка `ChestMemory.exe`
```bat
build_windows.bat
```
Готовый исполняемый файл появится в `dist\ChestMemory.exe`.

---

## Структура проекта

```text
ChestMemory/
├── main.py                     # Точка входа (GUI single-instance + CLI)
├── requirements.txt            # Зависимости Python 3.12+
├── pyproject.toml              # Конфигурация pytest
├── ChestMemory.spec            # Конфигурация PyInstaller
├── build_windows.bat           # Автосборка ChestMemory.exe на Windows
├── core/
│   ├── controller.py           # Оркестратор мониторинга, БД, CV и бэкапов
│   ├── counter.py              # Потокобезопасный счётчик + ease-out анимация
│   └── statistics.py           # Статистика за день/неделю/месяц и экспорт CSV/JSON
├── detector/
│   ├── replay_parser.py        # Бинарный парсер Fortnite .replay (UE + AES + Oodle)
│   ├── replay_analyzer.py      # Трекер состояний сундуков (bAlreadySearched/SearchingPawn)
│   └── visual_detector.py      # OpenCV временной автомат открытия сундуков
├── database/
│   ├── database.py             # SQLite (WAL, дедупликация, failed_analyses, settings)
│   └── models.py               # Модели данных и перечисления
├── interface/
│   ├── main_window.py          # Компактное тёмное окно (drag, topmost, анимация)
│   ├── settings.py             # Настройки, история матчей, ошибки, бэкапы
│   └── tray.py                 # Системный трей и уведомления
├── services/
│   ├── replay_watcher.py       # Watchdog-мониторинг + стабилизация записи + проверка IsLive
│   └── backup_service.py       # Онлайн-бэкапы SQLite + SHA-256 + восстановление
├── tests/                      # Автотесты (pytest / unittest)
└── data/                       # База данных и резервные копии
```
