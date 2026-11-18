# NeoMarket — Пересдача 12

Локальная реализация 13 обязательных заданий БРС. Три API смонтированы в одном приложении FastAPI. Они используют одну базу SQLite, чтобы смена статуса, событие модерации и резерв могли фиксироваться атомарно. Оплата при оформлении имитируется.

## Запуск

Нужен Python 3.11 или новее. Команды для PowerShell.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:NEOMARKET_JWT_SECRET = (.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))")
$env:NEOMARKET_SERVICE_KEY = (.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))")
.\.venv\Scripts\python.exe -m uvicorn neomarket.main:create_app --factory
```

Swagger доступен по адресам [B2B](http://127.0.0.1:8000/b2b/docs), [B2C](http://127.0.0.1:8000/b2c/docs) и [Moderation](http://127.0.0.1:8000/moderation/docs). Файл базы по умолчанию — `neomarket.sqlite3`. Путь можно задать через `NEOMARKET_DB`.

Для локальной проверки создайте JWT нужной роли в том же окружении. Каждый вызов без `--subject` создаёт новый идентификатор. Используйте один токен продавца для создания товара, чтения его карточки и добавления SKU.

```powershell
$seller = .\.venv\Scripts\python.exe -m neomarket token seller
$buyer = .\.venv\Scripts\python.exe -m neomarket token buyer
$moderator = .\.venv\Scripts\python.exe -m neomarket token moderator
```

Передавайте токен в заголовке `Authorization: Bearer <JWT>`, межсервисный ключ — в `X-Service-Key`. Для гостевой корзины нужен случайный UUID в `X-Session-Id`. После получения JWT покупателя клиент вызывает `POST /b2c/api/v1/cart/merge` с обоими заголовками.

При первом запуске создаётся категория с ID `4ca1fea4-b66d-454d-9b47-60cd59d39310`. Создайте карточку через `POST /b2b/api/v1/products`, затем добавьте SKU через `POST /b2b/api/v1/skus`. Для демонстрации задайте остаток командой `python -m neomarket stock <sku-id> 5` из виртуального окружения. Модератор получает карточку через `POST /moderation/api/v1/queue/claim` и одобряет через `POST /moderation/api/v1/tickets/{ticket_id}/approve`. После этого товар доступен в каталоге B2C.

Для checkout добавьте SKU в корзину, создайте адрес и тестовый способ оплаты. Передайте их ID в `POST /b2c/api/v1/orders` и укажите новый UUID в заголовке `Idempotency-Key`.

## Проверка

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Тесты проходят через HTTP и создают отдельную БД на каждый сценарий. Сценарии отказа B2B используют подмену межмодульного интерфейса. Локальные JWT выпускает CLI, а остатки для демонстрации задаются командой `stock`. Для раздельного развёртывания модулей потребуется внешний транспорт событий и общая схема интеграции.

Ссылки на сценарии, список тестов и ADR для PR собраны в [PR.md](PR.md). Реализация сверена с [neomarket-canon, ревизия 2ff93a4](https://github.com/URFU2026-NeoMarket/neomarket-canon/tree/2ff93a4cebc119e860385b318ebd8753fda1d801) и [neomarket-protocols, ревизия 3b405c6](https://github.com/URFU2026-NeoMarket/neomarket-protocols/tree/3b405c6).
