# database.py

import aiomysql
import json
import logging
import ssl

from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME, DB_SSL_CA

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, host: str = DB_HOST, port: int = DB_PORT, user: str = DB_USER, password: str = DB_PASSWORD, db: str = DB_NAME, ssl_ca: str | None = DB_SSL_CA):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.db = db
        self.ssl_ca = ssl_ca
        self.pool = None

    async def connect(self):
        try:
            ssl_context = None
            if self.ssl_ca:
                ssl_context = ssl.create_default_context(cadata=self.ssl_ca)

            self.pool = await aiomysql.create_pool(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                db=self.db,
                autocommit=True,
                charset='utf8mb4',
                maxsize=10,
                ssl=ssl_context
            )
            logger.info("Подключение к базе данных установлено.")
        except Exception as e:
            logger.critical(f"Не удалось подключиться к базе данных: {e}")
            raise

    async def close(self):
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
            logger.info("Подключение к базе данных закрыто.")

    # Метод для добавления нового пользователя
    async def add_user(self, user_id, username=None, status='pending'):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        """
                        INSERT INTO users (user_id, username, status)
                        VALUES (%s, %s, %s)
                        ON DUPLICATE KEY UPDATE username=VALUES(username), status=VALUES(status)
                        """,
                        (user_id, username, status)
                    )
                    logger.info(f"Пользователь {user_id} добавлен/обновлен с статусом {status}.")
                except Exception as e:
                    logger.error(f"Ошибка при добавлении пользователя {user_id}: {e}")
                    raise

    # Метод для получения информации о пользователе
    async def get_user(self, user_id):
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM users WHERE user_id=%s",
                    (user_id,)
                )
                user = await cur.fetchone()
                return user

    # Метод для обновления контактной информации пользователя
    async def update_user_contact(self, user_id, name, phone, city):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        """
                        UPDATE users
                        SET name=%s, phone=%s, city=%s
                        WHERE user_id=%s
                        """,
                        (name, phone, city, user_id)
                    )
                    logger.info(f"Контактная информация пользователя {user_id} обновлена.")
                except Exception as e:
                    logger.error(f"Ошибка при обновлении контактной информации пользователя {user_id}: {e}")
                    raise

    # Метод для обновления статуса пользователя
    async def update_user_status(self, user_id, status):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        """
                        UPDATE users
                        SET status=%s
                        WHERE user_id=%s
                        """,
                        (status, user_id)
                    )
                    logger.info(f"Статус пользователя {user_id} обновлен на {status}.")
                except Exception as e:
                    logger.error(f"Ошибка при обновлении статуса пользователя {user_id}: {e}")
                    raise

    # Метод для обновления последней активности пользователя
    async def update_last_active(self, user_id):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        """
                        UPDATE users
                        SET last_active=NOW()
                        WHERE user_id=%s
                        """,
                        (user_id,)
                    )
                    logger.debug(f"Обновлено время последней активности для пользователя {user_id}.")
                except Exception as e:
                    logger.error(f"Ошибка при обновлении last_active для пользователя {user_id}: {e}")
                    raise

    # Метод для проверки состояния бота (открыт/закрыт)
    async def is_bot_open(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT `value` FROM bot_settings WHERE `key`='is_open'"
                )
                result = await cur.fetchone()
                if result and result[0].lower() == 'true':
                    return True
                return False

    # Метод для установки состояния бота
    async def set_bot_state(self, state: bool):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                value = 'true' if state else 'false'
                await cur.execute(
                    """
                    INSERT INTO bot_settings (`key`, `value`)
                    VALUES ('is_open', %s)
                    ON DUPLICATE KEY UPDATE `value`=VALUES(`value`)
                    """,
                    (value,)
                )
                logger.info(f"Состояние бота установлено на {'открыт' if state else 'закрыт'}.")

    # Метод для добавления объявления
    async def add_ad(self, title, price, description, photos, inspection_photos, thickness_photos, model, year):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    photos_json = json.dumps(photos)
                    inspection_photos_json = json.dumps(inspection_photos)
                    thickness_photos_json = json.dumps(thickness_photos)
                    await cur.execute(
                        """
                        INSERT INTO ads (title, model, year, price, description, photos, inspection_photos, thickness_photos)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (title, model, year, price, description, photos_json, inspection_photos_json, thickness_photos_json)
                    )
                    ad_id = cur.lastrowid
                    logger.info(f"Объявление '{title}' добавлено с ID {ad_id}.")
                    return ad_id
                except Exception as e:
                    logger.error(f"Ошибка при добавлении объявления '{title}': {e}")
                    raise

    # Метод для получения всех объявлений
    async def get_ads(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM ads ORDER BY added_date DESC"
                )
                ads = await cur.fetchall()
                # Преобразование JSON-полей обратно в списки
                for ad in ads:
                    ad['photos'] = json.loads(ad['photos']) if ad['photos'] else []
                    ad['inspection_photos'] = json.loads(ad['inspection_photos']) if ad['inspection_photos'] else []
                    ad['thickness_photos'] = json.loads(ad['thickness_photos']) if ad['thickness_photos'] else []
                return ads

    # Метод для получения конкретного объявления по ID
    async def get_ad(self, ad_id):
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM ads WHERE ad_id=%s",
                    (ad_id,)
                )
                ad = await cur.fetchone()
                if ad:
                    ad['photos'] = json.loads(ad['photos']) if ad['photos'] else []
                    ad['inspection_photos'] = json.loads(ad['inspection_photos']) if ad['inspection_photos'] else []
                    ad['thickness_photos'] = json.loads(ad['thickness_photos']) if ad['thickness_photos'] else []
                return ad

    # Метод для удаления объявления
    async def delete_ad(self, ad_id):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        "DELETE FROM ads WHERE ad_id=%s",
                        (ad_id,)
                    )
                    logger.info(f"Объявление с ID {ad_id} удалено.")
                except Exception as e:
                    logger.error(f"Ошибка при удалении объявления {ad_id}: {e}")
                    raise

    # Метод для проверки, является ли объявление избранным для пользователя
    async def is_favorite(self, user_id, ad_id):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM favorites WHERE user_id=%s AND ad_id=%s",
                    (user_id, ad_id)
                )
                result = await cur.fetchone()
                return bool(result)

    # Метод для добавления объявления в избранное
    async def add_to_favorites(self, user_id, ad_id):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        """
                        INSERT INTO favorites (user_id, ad_id)
                        VALUES (%s, %s)
                        ON DUPLICATE KEY UPDATE ad_id=ad_id
                        """,
                        (user_id, ad_id)
                    )
                    logger.info(f"Объявление {ad_id} добавлено в избранное пользователя {user_id}.")
                except Exception as e:
                    logger.error(f"Ошибка при добавлении объявления {ad_id} в избранное пользователя {user_id}: {e}")
                    raise

    # Метод для удаления объявления из избранного
    async def remove_from_favorites(self, user_id, ad_id):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        "DELETE FROM favorites WHERE user_id=%s AND ad_id=%s",
                        (user_id, ad_id)
                    )
                    logger.info(f"Объявление {ad_id} удалено из избранного пользователя {user_id}.")
                except Exception as e:
                    logger.error(f"Ошибка при удалении объявления {ad_id} из избранного пользователя {user_id}: {e}")
                    raise

    # Метод для получения избранных объявлений пользователя
    async def get_favorite_ads(self, user_id):
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT ads.*
                    FROM ads
                    JOIN favorites ON ads.ad_id = favorites.ad_id
                    WHERE favorites.user_id=%s
                    ORDER BY ads.added_date DESC
                    """,
                    (user_id,)
                )
                ads = await cur.fetchall()
                for ad in ads:
                    ad['photos'] = json.loads(ad['photos']) if ad['photos'] else []
                    ad['inspection_photos'] = json.loads(ad['inspection_photos']) if ad['inspection_photos'] else []
                    ad['thickness_photos'] = json.loads(ad['thickness_photos']) if ad['thickness_photos'] else []
                return ads

    # Метод для добавления подписки
    async def add_subscription(self, user_id, model=None, price_min=None, price_max=None, year_min=None, year_max=None):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        """
                        INSERT INTO subscriptions (user_id, model, price_min, price_max, year_min, year_max)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (user_id, model, price_min, price_max, year_min, year_max)
                    )
                    logger.info(f"Пользователь {user_id} добавил новую подписку.")
                except Exception as e:
                    logger.error(f"Ошибка при добавлении подписки для пользователя {user_id}: {e}")
                    raise

    # Метод для получения подписок пользователя
    async def get_subscriptions(self, user_id):
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT * FROM subscriptions
                    WHERE user_id=%s
                    """,
                    (user_id,)
                )
                subscriptions = await cur.fetchall()
                return subscriptions

    # Метод для удаления подписки
    async def delete_subscription(self, rowid):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        "DELETE FROM subscriptions WHERE rowid=%s",
                        (rowid,)
                    )
                    logger.info(f"Подписка с rowid {rowid} удалена.")
                except Exception as e:
                    logger.error(f"Ошибка при удалении подписки {rowid}: {e}")
                    raise

    # Метод для получения контактных данных пользователей
    async def get_user_contacts(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT name, city, phone FROM users
                    WHERE name IS NOT NULL AND city IS NOT NULL AND phone IS NOT NULL
                    """
                )
                contacts = await cur.fetchall()
                return contacts

    # Метод для получения всех подписок (для уведомления при добавлении нового объявления)
    async def get_all_subscriptions(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM subscriptions"
                )
                subscriptions = await cur.fetchall()
                return subscriptions

    # Метод для получения одобренных пользователей (для рассылок)
    async def get_approved_users(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT user_id FROM users WHERE status='approved'"
                )
                users = await cur.fetchall()
                return [user[0] for user in users]

    # Метод для получения статистики
    async def get_statistics(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                # Общее количество пользователей
                await cur.execute("SELECT COUNT(*) FROM users")
                users_count = (await cur.fetchone())[0]
                # Общее количество объявлений
                await cur.execute("SELECT COUNT(*) FROM ads")
                ads_count = (await cur.fetchone())[0]
                return users_count, ads_count

    # Метод для получения количества активных пользователей (за последние 7 дней)
    async def get_active_users_count(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT COUNT(*) FROM users
                    WHERE last_active >= NOW() - INTERVAL 7 DAY
                    """
                )
                active_users = (await cur.fetchone())[0]
                return active_users

    # Метод для получения неактивных пользователей (которые были активны до cutoff_time)
    async def get_inactive_users(self, cutoff_time):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT user_id FROM users
                    WHERE last_active <= %s
                    """,
                    (cutoff_time,)
                )
                users = await cur.fetchall()
                return [user[0] for user in users]

    # Метод для обновления чекового file_id пользователя
    async def update_user_cheque(self, user_id, cheque_file_id):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        """
                        UPDATE users
                        SET cheque_file_id=%s
                        WHERE user_id=%s
                        """,
                        (cheque_file_id, user_id)
                    )
                    logger.info(f"Чек пользователя {user_id} обновлен.")
                except Exception as e:
                    logger.error(f"Ошибка при обновлении чека пользователя {user_id}: {e}")
                    raise

    # Метод для получения контактных данных пользователей (для экспорта)
    async def get_user_contacts_for_export(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT name, city, phone FROM users
                    WHERE name IS NOT NULL AND city IS NOT NULL AND phone IS NOT NULL
                    """
                )
                contacts = await cur.fetchall()
                return contacts