# eBag AI Price Intelligence Agent · v0.2

Работещ агент за публичните цени на eBag: SQLite история, EUR/kg/liter/piece,
проверими исторически оценки, email само за подходящи сделки, ежедневен
scheduler, product matching, възможна shrinkflation и HTML отчет.

**Проверено на 24.10.2026:** 859 реални продукта от 50 JSON страници са записани
в базата. 35 автоматични теста преминават. Не е изпращан реален email.
Това не е пълен обход на магазина и не е натрупана многодневна история.
Docker конфигурацията е подготвена, но image build не е проверен тук.

## Стартиране на Windows с Docker Desktop

1. Разархивирайте проекта и отворете папката `ebag-agent`.
2. При работещ Docker Desktop (Linux containers / WSL2) изпълнете `start.ps1`
   от PowerShell. Скриптът създава `.env` и го отваря в Notepad.
3. Попълнете SMTP сървър, порт, потребител, парола и разрешен подател.
   Не споделяйте `.env`, не го качвайте в GitHub. След затваряне на Notepad
   скриптът изгражда и стартира контейнера.

Или от терминал в папката на проекта:

```bash
cp .env.example .env
# Попълнете SMTP настройките локално.
docker compose up --build -d
docker compose logs -f
```

Агентът проверява веднъж дневно в **08:00 Europe/Sofia**, включително лятното
часово време. Ако е стартиран след 08:00, изпълнява днешната проверка веднага.
Хостът и Docker трябва да работят. Спирането на хоста спира проверките.
Няма активиран външен хост или ChatGPT automation от тази сесия.

## Проверка и отчет

```bash
docker compose exec agent ebag health
docker compose exec agent ebag smtp-test --send
docker compose cp agent:/app/data/report.html ./report.html
```

`smtp-test --send` действително изпраща тестово писмо до конфигурирания
получател. Приетото от SMTP писмо не доказва доставка до Inbox; проверете Spam.
Получател по подразбиране: aiskrev@abv.bg.
Отчетът съдържа търсене, watchlist и BUY филтри, unit prices, увереност,
срокове и покритие. `data/live-report.html` в архива показва реалния обход
от тази сесия. Blacklist продуктите са скрити от HTML и email.

## Без Docker (Linux / WSL2, Python 3.11+)

```bash
python -m pip install .
python -m unittest discover -s tests -v
python -m ebag.cli run --output data/report.json
python -m ebag.cli schedule --send
```

Локалният CLI зарежда `.env`, без да изпълнява съдържанието му като shell код.
Изисква Linux файлови locks (`fcntl`); за Windows използвайте Docker/WSL2.
Демонстрацията е отделена от live базата:

```bash
python -m ebag.cli run --import-file tests/fixtures/products.json
```

Тя използва `data/demo.sqlite3` по подразбиране и отказва изпращане на email.
За да избегнете смесване на произхода, не насочвайте ръчно импорт към live DB.

## Достъп и обхват

Не се използват потребителски акаунт, authentication cookies, частни API,
скрити идентификатори, CAPTCHA bypass или прикриване на User-Agent.
Адаптерът чете JSON адресите, използвани от публичния eBag storefront.
`docs/ACCESS.md` описва прегледа на robots и условията и ограниченията му.

Преди всеки обход се проверява robots.txt. 401, 403, 429 или challenge спират
изпълнението. Кеш: 1 час; интервал: 5 секунди; backoff за мрежови/5xx грешки.
По подразбиране: максимум 50 listing страници (около 1000 реда преди dedup)
и 100 допълнителни обновявания на познати продукти. Първата страница на всяка
приоритетна категория се обхожда преди допълнително странициране.
Необходените страници се запазват за следващо успешно изпълнение. Лимитите
могат да се редактират в `config/preferences.yaml` (до 100 listing страници).
Coverage в отчета показва пропуснатите страници/отложените обновявания.
Нито липса от offers, нито 404 автоматично доказват липса на наличност.

Провереният storefront release е фиксиран в config. Нов release или изтичане
на 30 дни от прегледа спира live режима до нов преглед на условията/parser-а.
Release fingerprint не доказва, че динамичните условия не са променени.
Това е технически сигнал за повторна проверка, а не правно разрешение.

## История, идентичност и оценки

Наблюденията не се презаписват. Всяко изпълнение добавя ред; историческите
метрики използват по едно наблюдение за предишен локален ден и изключват
текущия ден. Различни разфасовки имат отделни identities. Метриките за 7/30/
90/180/365 дни изискват съответен период и >=65% дневно покритие.
HIGH: >=60 дневни наблюдения за >=90 дни; MEDIUM: >=20 за >=30 дни;
останалите LOW. При липсваща история няма доказана сделка и няма BUY email.
Baseline: 90-day median, след това 30-day median, след това общият median.

Score (0–100) = историческо намаление (до 35) + близост до минимум (20) +
90-day unit-price discount / 2 (до 15) + предпочитана категория (10) +
watchlist (10) + увереност (0/6/10) − подозрителна промоция (20) − LOW history
(15) − кратък срок (20). Всички фактори присъстват в JSON отчета.
Обявеното намаление не носи самостоятелни точки.
BUY: >=70, >=20% под baseline и >=15% под 30-day median, без предупредителни
сигнали. STRONG BUY: >=85, >=25% под baseline и до 5% над 90-day minimum.
Праговете, confidence и watchlist условията са конфигурируеми.

Има неутрални сигнали за висока reference price, разлика в обявеното и
историческото намаление, често ниска цена, постоянна промоция и кратък
reference-price spike. Това са евристики, не доказателства за нарушение.

## Настройки

`preferences.yaml` поддържа YAML (safe loader) и JSON синтаксис. При редакция
рестартирайте контейнера. Watchlist е списък от обекти, например:

```yaml
watchlist:
  - keywords: пилешко филе
    brand: Моята предпочитана марка
    excluded_brands: [Друга марка]
    min_quantity: 0.4
    max_quantity: 1.5
    unit: kg
    max_price: 10
    max_unit_price: 12
    minimum_real_discount_for_email: 15
    preferred_quantity: 2
```

Ключовите думи могат да съвпадат с публичното българско или английско име.
Изключването на марка в watchlist означава, че тя не съвпада с този watch item;
за глобално изключване използвайте excluded_products / excluded_categories.
Пакетните quantities са kg/liter/piece според unit. Не смесвайте мерни единици.

Без проверено съхранение и срок: максимум BUY 1. BUY 2/STOCK UP изискват
изрично потвърден storage rule за internal external product ID:

```yaml
storage_rules:
  "123456":
    storage_verified: true
    shelf_stable: true
```

Това са потребителски потвърждения, не автоматични заключения. STOCK UP
изисква и STRONG BUY, >=30 дни до срок и preferred_quantity >=3.
Липсващ срок не се измисля; изтекъл продукт не се препоръчва.

## Matching и shrinkflation

Кандидатите използват external ID, brand, категория, сходство на име,
разфасовка и единица. Записват се в product_matches, без автоматично сливане
на история. При по-малка опаковка, близка крайна цена (±10%) и по-висока unit
price се отчита POSSIBLE SHRINKFLATION с confidence. Това не доказва еднакъв
състав; неизвестните състави не се приемат за еднакви.
Ръчно потвърждение е възможно чрез `manual_matches` с вътрешните ID от DB:

```yaml
manual_matches:
  - source_id: 1
    candidate_id: 2
    match_type: SAME PRODUCT DIFFERENT PACKAGE
    confidence: 0.99
```

## Email и deduplication

SMTP е зад Provider abstraction. TLS е задължителен: STARTTLS или SSL на 465.
Няма email при празна селекция; няма „No good deals today“.
Fingerprint пази продукт/цена/промоционен период; идентичната оферта не се
изпраща повторно. В 7-дневния cooldown нова цена изисква >=10% спад, +15 score
или подобрение до нов исторически минимум. Това е по-консервативно от повторно
изпращане само защото магазинът е сменил етикета/периода на промоцията.

Reservation се записва преди SMTP; при неясен резултат статусът е uncertain.
Не се прави автоматично повторно изпращане. Операторът проверява SMTP логовете
преди ръчно коригиране на sending/uncertain редове. Няма exactly-once гаранция
за външен SMTP. Импортът никога не изпраща писма.

## Поддръжка

```bash
docker compose exec agent ebag backup --output data/backup.sqlite3
docker compose cp agent:/app/data/backup.sqlite3 ./backup.sqlite3
```

Пазете резервното копие извън хоста. DB е в persistent named volume.
Не изпълнявайте `docker compose down -v`, ако искате да запазите историята.
Health отчита DB и възрастта на последния успешен live crawl; SMTP configured
не означава verified delivery. Import не прави live health зелен.
Дневните scheduler slots са в DB и преживяват рестарт; неуспешен slot не се
повтаря автоматично същия ден. След поправка изпълнете `ebag run --send`.
Миграция v1 е идемпотентна, SQL е в migrations/001_initial.sql и db.py.
За PostgreSQL е необходим отделен Repository adapter и миграция; не е
реализирана смяна само чрез connection string.

В `docs/VALIDATION.md` има доказателствата от проверките, а `GAPS.md` описва
границите на реализацията. README не твърди сертифицирана production готовност.


## GitHub

Кодът е в основната папка на това хранилище. Runtime базата и генерираните отчети са untracked; възпроизведими публични fixtures са в tests/fixtures. Прочетете GAPS.md и HANDOFF.md преди довършване.
