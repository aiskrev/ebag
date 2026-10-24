# Проверки — 24.10.2026

## Успешни проверки

- 35 unittest теста: normalization, multipack, variable weight, currencies,
  blacklist, daily dedup, cold-start restrictions, matching/shrinkflation,
  anonymous eBag schema fixtures, expired products, continuous promotion,
  ambiguous SMTP result, robots denial, configuration and HTML escaping.
- Реален crawl: run 1 — 40 продукта; run 2 — 859 продукта от 50 страници,
  247.65 секунди, status=success; run 3 — 859 продукта с HTTP cache, success.
- Текущият adapter е проверен повторно offline срещу всички 859 реални
  response продукта: всички се parse-ват; неизвестните физически количества
  остават null и не получават BUY. Това не създава нови live наблюдения.
- SQLite PRAGMA integrity_check: ok.
- SQLite online backup: успешно.
- Health: database=true, live_crawler_healthy=true, smtp_configured=false.
- HTML отчет и JSON данни са генерирани.

Дневните observations са от един календарен ден. Повторенията не се броят
като отделни дни и не създават фалшива историческа увереност.

## Непроверени външни зависимости

- Реална SMTP доставка: липсват SMTP настройки; няма изпратени писма.
- Docker image build и compose startup: Docker не е наличен в средата.
- Няма разположен 24/7 хост или активиран външен scheduler.
- Няма данни от бъдещи дни, затова няма доказан многодневен live soak test.

Включеният data/ebag.sqlite3 съдържа реалните наблюдения. demo.sqlite3 е
отделен синтетичен пример. test-results.txt съдържа пълния резултат от тестовете.
