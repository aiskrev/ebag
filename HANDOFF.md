# Handoff

Състояние към 2026-11-04. Прочетете и GAPS.md, README.md, docs/ACCESS.md и
docs/VALIDATION.md. Код: ebag/. Тестове: tests/.

## Текущо състояние

**Live режимът работи.** Собственикът одобри записа на прегледа на release
`2d865171801aba36d91ca62de3454d1898093484` на 04.11. Реалният crawl в Docker мина:
850 продукта, 50 страници, 256.5 s, 0 parser грешки, `health` зелен, първи ден
история в Docker volume `ebag_ebag-data`. JSON полетата са същите като в fixtures.

Агентът е активен на Windows машината на собственика от `D:\Projects\eBag\ebag`:
контейнерът е с `restart: unless-stopped` и обхожда всеки ден в 08:00 Europe/Sofia.
Ако компютърът е бил изключен в 08:00, обходът тръгва при следващото пускане същия
ден. Docker Desktop още НЕ стартира сам при влизане в Windows, затова след рестарт
агентът чака Docker да бъде отворен. Няма BUY препоръки, докато не се натрупа история.

При следващ нов release (`blocked_review` в `ebag health`):

```bash
# Прегледайте robots, условията и отговорите, после обновете в
# config/preferences.yaml: crawl.policy_reviewed_at, crawl.reviewed_store_commit
docker compose up --build -d
docker compose exec agent ebag health
```

Рестартът с обновен config изпълнява днешния блокиран slot автоматично.
Slot, записан от версия преди 04.11, се възстановява с `ebag run --send`.

## Завършено в тази сесия

- Първи реален Docker build, старт и проверка на stack-а.
- Scheduler-ът вече не държи lock постоянно: `smtp-test`, `report` и
  `run --send` работят до него. Два обхода все още се изключват взаимно.
- Дневни слотове с причина: мрежови/5xx грешки се повтарят до 3 пъти на ден;
  401/403/429/challenge никога; нужен преглед се повтаря само след запис на
  прегледа в config и рестарт. Slot-ът вече не се губи при първи неуспех.
- Email грешка вече не маркира crawl-а като неуспешен и не губи frontier.
- Единичен лош продукт вече не губи целия ден: статус `partial`, наблюденията се
  пазят, email-ите се спират. Над 5% грешки остава schema drift и спира обхода.
- Release проверката винаги чете свежа страница; кешът криеше нови release-и.
- Health вече не отчита работещ crawl като срив и показва причината за спиране.
- Служебен email при спиране, най-много веднъж на вид на ден.
- `ebag notifications` за uncertain редове, вместо ръчен SQL.
- Срив на връзка/TLS/вход вече е `failed` и офертата се изпраща при следващ run;
  само резултат след предаване на писмото остава `uncertain`.
- Дневен компресиран backup с ротация (`backup.keep`, по подразбиране 7).
- Опция `crawl.on_release_change`: `stop` (по подразбиране) или `warn`.
- Дневно резюме по email след всеки завършен обход, с предупреждение 7 дни преди
  края на 30-дневния преглед. Първото автоматично резюме още не е наблюдавано.
- Поправена проверка на сертификата на порт 465.

## Резултати

- Тестове: 95/95 в python:3.12-slim. На хоста няма Python.
- Docker: build, старт, read-only файлова система, рестарт, отказ на втори
  scheduler и backup са проверени. Контейнерът работи от постоянната папка.
- Scheduler процес срещу затворената защита: `blocked_review`, 0 наблюдения.
- Реален crawl: 850 продукта, 0 parser грешки, схемата е същата като във fixtures.
- SMTP: целият pipeline е проверен срещу локален STARTTLS сървър с истинска
  проверка на сертификата. smtp.abv.bg:465 прие тестово писмо на 04.11 с проверен
  сертификат и вход. Собственикът потвърди, че писмото пристигна.

## Команди

```bash
docker compose up --build -d
docker compose logs -f
docker compose exec agent ebag health
docker compose exec agent ebag notifications
docker compose exec agent ebag run --send
```

Тестове без Python на хоста, от Git Bash:

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd -W):/src" -w /src python:3.12-slim sh -c "pip install -q 'PyYAML>=6,<7' && python -B -m unittest discover -s tests"
```

## Запазете

Append-only история, консервативни cold-start оценки, blacklist, robots политика,
SMTP deduplication. Не commit-вайте .env, credentials, SQLite данни или кеш.
`config/preferences.yaml` трябва да остане валиден JSON: тестовете го четат така.
