# Исходный код UmaRU Launcher 1.2 для Windows

Лаунчер запускает японскую Steam-версию Umamusume с русским переводом. Модули в `player/` отвечают за окно, установку перевода и проверку обновлений. Общие компоненты Tk и значок находятся в `ui_kit.py` и `ui_icon.py`.

## Запуск из исходников

В Windows с Python 3.11 и Tk откройте PowerShell в каталоге `launcher`.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-build.txt
python player/umaru_app.py
```

Для проверки без открытия игры используйте `python player/umaru_app.py --check --json report.json`. В установленном мастере адрес канала и вариант перевода задаёт Inno Setup в `data/update-channel.json`.

## Сборка установщика

Потребуются Inno Setup 6 и внешние файлы Hachimi Edge 0.33.1. Перед сборкой скрипт сверяет SHA-256 предоставленного установщика и запись о его происхождении. Укажите полный путь к каждому входному файлу. Для каталога сборки выберите место вне репозитория.

```powershell
python setup/build_setup.py --build-dir ../../local-build --hachimi-installer D:/inputs/hachimi_installer.exe --hachimi-provenance D:/inputs/hachimi_installer.txt --iscc "C:/Program Files (x86)/Inno Setup 6/ISCC.exe"
```

Рецепт использует `setup/pyi-spec/UmaRU.spec` и `setup/UmaRU.iss`. Готовый установщик появится по пути `../../local-build/output/UmaRU-Setup.exe`.

## Пакеты и публикация

- `player_pack.py` собирает `pack.json` и `pack.zip` из внешнего каталога перевода. Для команды ниже переменная `UMA_RU_GAME_DIR` указывает на локальную копию игры с подготовленным каталогом `hachimi`. Скрипту `server_bound.py` для проверки служебных строк нужны `hachimi/localize_dump.json` и `hachimi/ru_translator/data/en_dicts/localize_dict.json`.

  ```powershell
  python player_pack.py build --no-release --out ../../local-pack
  ```

- `publish_online.py prepare` проверяет утверждённые квитанции выпусков и формирует `channel.json`, манифесты полного и облегчённого пакетов и дельты от предыдущей версии. `publish` загружает проверенные файлы в GitHub Releases. Команда `release` объединяет эти шаги для нового утверждённого выпуска. Для поиска предыдущей квитанции можно задать `UMA_RU_RELEASE_WORK`.
- `publisher/sign-launcher.ps1` подписывает версию, размер, SHA-256 установщика и заметки для `launcher.json` ключом из хранилища Windows CNG издателя. В `player/umaru_launcher.py` находится открытый ключ для проверки. Для независимого канала выпуска понадобятся свой ключ издателя и соответствующий открытый ключ в клиенте.
- `publish_online.py publish-launcher --installer PATH/UmaRU-Setup.exe --manifest PATH/launcher.json` публикует добровольное обновление лаунчера. Публикация требует авторизованного GitHub CLI с доступом к целевому репозиторию. Параметры каждой команды доступны через `--help`.

Инструкции для автономных тестов и проверок с внешними данными находятся в [tests/README.md](tests/README.md).
