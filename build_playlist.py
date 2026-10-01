name: build-playlist

on:
  schedule:
    # ВНИМАНИЕ: время в UTC. 0 3 * * * = один раз в сутки, в 03:00 UTC = 06:00 по Москве.
    # GitHub может задержать запуск на 5-20 минут при высокой нагрузке — это нормально.
    - cron: "0 3 * * *"
  workflow_dispatch:      # кнопка "запустить сейчас" в веб-интерфейсе
  push:
    paths:
      - "config.json"       # пересобрать сразу после правки списка источников
      - "config-my.json"
      - "config-tv.json"
      - "config-tv-verified.json"
      - "build_playlist.py"

permissions:
  contents: write         # нужно, чтобы бот мог закоммитить плейлист

concurrency:
  group: build-playlist
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4

      - name: Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: requirements.txt

      - name: Зависимости
        run: pip install -r requirements.txt

      - name: Сборка полного плейлиста
        run: python build_playlist.py

      - name: Сборка телевизионного плейлиста (короткий список)
        env:
          M3U_CONFIG: config-tv.json
          M3U_OUT: playlist-tv.m3u
          M3U_STATUS: status-tv.txt
        run: python build_playlist.py

      - name: Сборка проверенного плейлиста (белый список каналов)
        env:
          M3U_CONFIG: config-tv-verified.json
          M3U_OUT: playlist-tv-verified.m3u
          M3U_STATUS: status-tv-verified.txt
        run: python build_playlist.py

      - name: Сборка моего плейлиста (только выбранные каналы)
        env:
          M3U_CONFIG: config-my.json
          M3U_OUT: playlist-my.m3u
          M3U_STATUS: status-my.txt
        run: python build_playlist.py

      - name: Коммит плейлистов
        run: |
          git config user.name  "playlist-bot"
          git config user.email "playlist-bot@users.noreply.github.com"
          LISTS="playlist.m3u playlist-tv.m3u playlist-tv-verified.m3u playlist-my.m3u"

          # 1. Файлы обязаны существовать и быть непустыми
          for f in $LISTS; do
            if [ ! -s "$f" ]; then
              echo "::error::файл $f не создан — сборка не дала результата"
              ls -la
              exit 1
            fi
            echo "есть: $f — $(grep -c '^#EXTINF' "$f") каналов, $(stat -c%s "$f") байт"
          done

          # 2. Показываем, что git вообще видит (включая игнорируемые файлы)
          echo "--- git status:"
          git status --porcelain --ignored | head -30

          # 3. Добавляем принудительно: -f снимает возможные правила .gitignore,
          #    которые иначе заставляют git молча пропустить добавление
          git add -f $LISTS status.txt status-tv.txt status-tv-verified.txt status-my.txt 2>/dev/null || true

          # 4. Определяем, есть ли что коммитить. Сравниваем с HEAD, а не «пусто/не пусто»:
          #    так новые (ещё не отслеживаемые) файлы тоже видны как изменение.
          if git diff --cached --quiet HEAD -- $LISTS; then
            echo "Содержимое плейлистов не изменилось с прошлого коммита — коммита не будет"
            git reset >/dev/null
          else
            git commit -m "auto: плейлисты обновлены ($(date -u '+%Y-%m-%d %H:%M UTC'))"
            git push
            echo "закоммичено. Файлы в репозитории:"
            git show --stat --oneline HEAD | head -10
          fi

