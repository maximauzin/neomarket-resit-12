# Реализация обязательных заданий NeoMarket

13 сценариев БРС для команды «Пересдача 12». Команда проверки — `python -m pytest -v`.

См. [канонические потоки](https://github.com/URFU2026-NeoMarket/neomarket-canon/tree/main/flows) и [действующий OpenAPI](https://github.com/URFU2026-NeoMarket/neomarket-protocols/tree/master).

## US-B2B-01

Рассмотрены JSON в записи товара, таблица значений и EAV. Выбран JSON с типизированным входом, чтобы добавлять характеристики без миграции схемы. Фильтрация проверяет значения при чтении каталога, поэтому при росте каталога отдельная таблица дала бы больше возможностей для индексации.

[Задание](https://contract.tochka-urfu.tech/quests/contraction-implement-us-b2b-01-create-product) · [Канон](https://github.com/URFU2026-NeoMarket/neomarket-canon/blob/main/flows/b2b-flows.md#create-product)

- `test_create_product_returns_201_with_created_status`
- `test_seller_id_taken_from_jwt`
- `test_missing_images_returns_400`
- `test_missing_category_returns_400`
- `test_invalid_category_id_returns_400`

## US-B2B-02

Рассмотрены синхронный HTTP, outbox с отправителем и вызов без ожидания. Для одного процесса выбрана запись события и карточки модерации в общей транзакции. При ошибке откатываются и SKU, и событие, а на первой итерации не требуется отдельный процесс доставки.

[Задание](https://contract.tochka-urfu.tech/quests/contraction-implement-us-b2b-02-add-sku) · [Канон](https://github.com/URFU2026-NeoMarket/neomarket-canon/blob/main/flows/b2b-flows.md#add-sku)

- `test_first_sku_transitions_product_to_on_moderation`
- `test_second_sku_no_state_change`
- `test_add_sku_to_moderated_product_requires_another_review`
- `test_add_sku_to_hard_blocked_returns_403`
- `test_missing_image_returns_400`
- `test_first_sku_emits_created_event_to_moderation`

## US-B2B-05

Рассмотрены один обработчик с ветвлением, два URL и общий класс разрешений. Оставлен предусмотренный контрактом URL с явной проверкой режима и отдельными функциями представления. Проверка владельца читается рядом с вызовом представления продавца, а закрытые поля исключаются из межсервисного представления по списку разрешённых полей.

[Задание](https://contract.tochka-urfu.tech/quests/contraction-implement-us-b2b-05-view-product-card) · [Канон](https://github.com/URFU2026-NeoMarket/neomarket-canon/blob/main/flows/b2b-flows.md#view-product)

- `test_get_moderated_product_returns_full_payload`
- `test_get_blocked_product_returns_blocking_reason_and_field_reports`
- `test_get_others_product_returns_404`
- `test_get_nonexistent_returns_404`

## US-B2B-07

Рассмотрены два URL, переключение по заголовку и выбор обработчика на уровне роутера. Для каталога выделен маршрут /public/products с обязательным сервисным ключом. Список разрешённых полей снижает риск утечки, а новый режим можно добавить отдельным маршрутом.

[Задание](https://contract.tochka-urfu.tech/quests/contraction-implement-us-b2b-07-catalog-for-b2c) · [Канон](https://github.com/URFU2026-NeoMarket/neomarket-canon/blob/main/flows/b2b-flows.md#catalog-for-b2c)

- `test_catalog_returns_moderated_in_stock_products`
- `test_batch_ids_returns_visible_subset`
- `test_catalog_excludes_hard_blocked`
- `test_catalog_missing_service_key_returns_401`
- `test_catalog_response_has_no_cost_price`

## US-B2B-08

Рассмотрены блокировки строк, оптимистическая блокировка с повтором и двухфазный коммит. Для SQLite выбрана одна транзакция BEGIN IMMEDIATE, в которой проверяются все позиции до изменения остатков. Это упрощает откат и обеспечивает последовательное выполнение конкурирующих резервов, ценой конкуренции за единственную блокировку записи.

[Задание](https://contract.tochka-urfu.tech/quests/contraction-implement-us-b2b-08-reserve-sku) · [Канон](https://github.com/URFU2026-NeoMarket/neomarket-canon/blob/main/flows/b2b-flows.md#reserve-sku)

- `test_reserve_all_skus_succeeds`
- `test_idempotent_reserve_returns_200_without_double_deduction`
- `test_partial_insufficient_stock_returns_409_all_rollback`
- `test_sku_out_of_stock_event_emitted`
- `test_unreserve_restores_quantities`

## US-B2B-09

Рассмотрены отдельный журнал обработанных событий, последний ключ в записи товара и условный upsert. Выбран журнал с уникальным ключом и записью в той же транзакции, что и смена статуса. Он защищает от параллельной обработки и повторов старых событий, сохраняя возможность посмотреть исходный payload.

[Задание](https://contract.tochka-urfu.tech/quests/contraction-implement-us-b2b-09-apply-moderation) · [Канон](https://github.com/URFU2026-NeoMarket/neomarket-canon/blob/main/flows/b2b-flows.md#apply-moderation)

- `test_moderated_event_clears_blocking_data`
- `test_blocked_soft_saves_field_reports`
- `test_blocked_hard_sets_terminal_status`
- `test_duplicate_event_same_idempotency_key_no_side_effects`
- `test_missing_service_key_returns_401`
- `test_hard_blocked_product_rejects_seller_edits`

## US-CART-03

Рассмотрены X-Session-Id, cookie и временный JWT. Выбран предусмотренный контрактом UUID в X-Session-Id, одинаково доступный веб- и мобильным клиентам. Клиент должен генерировать случайный UUID и хранить его как идентификатор доступа к своей гостевой корзине.

[Задание](https://contract.tochka-urfu.tech/quests/contraction-implement-us-cart-03-cart) · [Канон](https://github.com/URFU2026-NeoMarket/neomarket-canon/blob/main/flows/b2c-cart-flows.md#b2c-8-cart)

- `test_add_sku_increments_quantity_if_already_in_cart`
- `test_get_cart_enriched_with_b2b_data`
- `test_unavailable_sku_shown_with_reason`
- `test_guest_cart_merged_on_login`

## US-CAT-01

Рассмотрены пересчёт при запросе, кэш с TTL и денормализованные счётчики. Для первой итерации выбран пересчёт по отфильтрованному набору товаров в одном снимке БД. Счётчики остаются согласованными с выдачей, а стоимость пересчёта растёт вместе с каталогом.

[Задание](https://contract.tochka-urfu.tech/quests/contraction-implement-us-cat-01-catalog-filters) · [Канон](https://github.com/URFU2026-NeoMarket/neomarket-canon/blob/main/flows/b2c-catalog-flows.md#b2c-1-catalog-filters)

- `test_catalog_returns_filtered_sorted_products`
- `test_b2b_unavailable_returns_502`
- `test_invalid_sort_returns_400`
- `test_facets_return_counts_per_filter_value`

## US-CAT-03

Рассмотрены отдельное представление, исключение полей в обработчике и выделенный URL. Выбрано отдельное представление с явным набором полей товара и SKU. Новое внутреннее поле не попадает в ответ автоматически, а поддерживаемые поля сосредоточены в двух коротких функциях представления.

[Задание](https://contract.tochka-urfu.tech/quests/contraction-implement-us-cat-03-view-product-card) · [Канон](https://github.com/URFU2026-NeoMarket/neomarket-canon/blob/main/flows/b2c-catalog-flows.md#b2c-3-product-card)

- `test_product_card_returns_full_data_with_skus`
- `test_blocked_product_returns_404`
- `test_sku_without_stock_is_shown_as_unavailable`
- `test_cost_price_absent_in_response`

## US-ORD-01

Рассмотрены ключ в записи заказа, отдельный реестр и Redis. Выбран отдельный реестр с ключом покупателя, сроком действия и телом запроса. Реестр, заказ и резерв записываются в одной транзакции, защищая от двойного списания при одновременных повторах без отдельной инфраструктуры.

[Задание](https://contract.tochka-urfu.tech/quests/contraction-implement-us-ord-01-checkout) · [Канон](https://github.com/URFU2026-NeoMarket/neomarket-canon/blob/main/flows/b2c-orders-flows.md#b2c-9-checkout)

- `test_checkout_creates_paid_order_with_fixed_prices`
- `test_partial_reserve_failure_returns_409`
- `test_idempotency_returns_existing_order`
- `test_b2b_unavailable_returns_503`

## US-ORD-03

Рассмотрены Celery, команда по cron и фоновый цикл приложения. Выбран фоновый цикл, который читает заказы в CANCEL_PENDING из БД и повторяет идемпотентный unreserve. Дополнительная инфраструктура не требуется, а после перезапуска отложенная отмена подхватывается из текущего состояния заказа.

[Задание](https://contract.tochka-urfu.tech/quests/contraction-implement-us-ord-03-cancel-order) · [Канон](https://github.com/URFU2026-NeoMarket/neomarket-canon/blob/main/flows/b2c-orders-flows.md#b2c-11-cancel-order)

- `test_cancel_paid_order_transitions_to_cancelled`
- `test_unreserve_failure_transitions_to_cancel_pending`
- `test_cancel_in_progress_releases_stock` (ASSEMBLING и DELIVERING)
- `test_cancel_delivered_order_returns_409`
- `test_other_user_order_returns_404`

Для новых пересдач действует уточнение от 14 июня 2026. При сборке и доставке заказ можно отменить, поэтому вместо прежнего теста на 409 при сборке проверяется успешная отмена обоих статусов и 409 после доставки.

## US-MOD-03

Рассмотрены синхронный HTTP, outbox и шина событий. В локальной архитектуре решение и обработка события B2B фиксируются в общей транзакции. Это обеспечивает согласованность без сетевого вызова в обработчике и позволяет откатить решение при ошибке B2B.

[Задание](https://contract.tochka-urfu.tech/quests/contraction-implement-us-mod-03-approve-product) · [Канон](https://github.com/URFU2026-NeoMarket/neomarket-canon/blob/main/flows/moderation-flows.md#approve-product)

- `test_approve_transitions_to_moderated_and_emits_event`
- `test_approve_others_card_returns_403`
- `test_approve_after_edited_returns_409`
- `test_approve_without_sku_returns_409`

## US-MOD-05

Рассмотрены терминальный статус, отдельный флаг и архивная таблица. Выбран HARD_BLOCKED с общей проверкой для изменения товара продавцом и проверками на входящих событиях. Исходное решение можно отследить по журналу, а административное исправление потребовало бы явного изменения данных.

[Задание](https://contract.tochka-urfu.tech/quests/contraction-implement-us-mod-05-hard-block) · [Канон](https://github.com/URFU2026-NeoMarket/neomarket-canon/blob/main/flows/moderation-flows.md#hard-block)

- `test_hard_block_transitions_to_terminal_and_emits_event`
- `test_any_modify_on_hard_blocked_returns_403`
- `test_edited_event_on_hard_blocked_is_ignored`
- `test_hard_block_event_carries_hard_block_true`
- `test_deleted_event_removes_hard_blocked`
