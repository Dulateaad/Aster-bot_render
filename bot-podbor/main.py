# Импорт необходимых библиотек
import os
import sys
import json
import logging
import re
import datetime
import pandas as pd
import urllib.parse
import random
import string
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove,
    KeyboardButton, ReplyKeyboardMarkup, error as telegram_error  # Импортируем error
)
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
    CallbackQueryHandler,
    ConversationHandler,
)
from cachetools import TTLCache
import mysql.connector
from mysql.connector import errorcode
import openai
import requests  # Перемещаем импорт requests сюда

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG  # Изменено на DEBUG для более подробного логирования
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

TELEGRAM_API_TOKEN = os.getenv('TELEGRAM_API_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
ADMIN_IDS_ENV = os.getenv('ADMIN_IDS', '')
ADMIN_IDS = list(map(int, ADMIN_IDS_ENV.split(','))) if ADMIN_IDS_ENV else []

DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = int(os.getenv('DB_PORT', 3306))
DB_USER = os.getenv('DB_USER', 'root')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')
DB_NAME = os.getenv('DB_NAME', 'aster_bot')

# Проверка наличия обязательных переменных окружения
if not TELEGRAM_API_TOKEN:
    logger.error("Не найден TELEGRAM_API_TOKEN в переменных окружения.")
    sys.exit(1)

if not OPENAI_API_KEY:
    logger.error("Не найден OPENAI_API_KEY в переменных окружения.")
    sys.exit(1)

if not DB_HOST or not DB_USER or not DB_PASSWORD or not DB_NAME:
    logger.error("Не заданы параметры подключения к базе данных MySQL.")
    sys.exit(1)

# Настройка OpenAI
openai.api_key = OPENAI_API_KEY

# Инициализация кэша (1 час, максимум 1000 записей)
cache = TTLCache(maxsize=1000, ttl=3600)

# Подключение к базе данных MySQL с использованием пула соединений
def connect_db():
    try:
        pool = mysql.connector.pooling.MySQLConnectionPool(
            pool_name="mypool",
            pool_size=10,
            pool_reset_session=True,
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        conn = pool.get_connection()
        cursor = conn.cursor(buffered=True)
        return pool
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_BAD_DB_ERROR:
            # База данных не существует, создаём её
            try:
                conn = mysql.connector.connect(
                    host=DB_HOST,
                    port=DB_PORT,
                    user=DB_USER,
                    password=DB_PASSWORD
                )
                cursor = conn.cursor(buffered=True)
                cursor.execute(f"CREATE DATABASE {DB_NAME} DEFAULT CHARACTER SET 'utf8mb4'")
                conn.database = DB_NAME
                conn.close()
                # Создаём пул после создания базы данных
                pool = mysql.connector.pooling.MySQLConnectionPool(
                    pool_name="mypool",
                    pool_size=10,
                    pool_reset_session=True,
                    host=DB_HOST,
                    port=DB_PORT,
                    user=DB_USER,
                    password=DB_PASSWORD,
                    database=DB_NAME
                )
                return pool
            except mysql.connector.Error as create_err:
                logger.error(f"Не удалось создать базу данных: {create_err}")
                sys.exit(1)
        else:
            logger.error(err)
            sys.exit(1)

pool = connect_db()

# Создание таблиц при необходимости
def init_db(pool):
    try:
        conn = pool.get_connection()
        cursor = conn.cursor(buffered=True)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                first_name VARCHAR(255),
                last_name VARCHAR(255),
                phone_number VARCHAR(50),
                name VARCHAR(255),
                city VARCHAR(255),
                join_date DATE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS statistics (
                stat_id INT AUTO_INCREMENT PRIMARY KEY,
                date DATE,
                total_users INT,
                new_users INT,
                messages_sent INT,
                links_sent INT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_requests (
                request_id INT AUTO_INCREMENT PRIMARY KEY,
                user_id BIGINT,
                preferences TEXT,
                timestamp DATETIME,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS prizes (
                prize_id INT AUTO_INCREMENT PRIMARY KEY,
                prize_name VARCHAR(255)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_prizes (
                user_id BIGINT PRIMARY KEY,
                prize_id INT,
                promo_code VARCHAR(255),
                win_date DATE,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (prize_id) REFERENCES prizes(prize_id) ON DELETE CASCADE
            )
        ''')
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("База данных и таблицы инициализированы.")
    except mysql.connector.Error as err:
        logger.error(f"Ошибка при инициализации базы данных: {err}")
        sys.exit(1)

init_db(pool)

# Состояния для ConversationHandler
GET_CONTACT, GET_NAME, GET_CITY = range(3)

# Маппинг призов на уникальные ID
prize_id_mapping = {
    '1': 'Сертификат на полугодовую мойку авто (24 мойки)',
    '2': 'Сертификат на бесплатный эвакуатор годовой',
    '3': 'Сертификат на 3 замены масла',
    '4': 'Годовой сертификат на тех помощь на дороге 24/7',
    '5': 'Сертификат на секретный приз'
}

# Константа для ссылки на WhatsApp менеджера
WHATSAPP_LINK = "https://wa.me/77019911161?text=Здравствуйте%20я%20перешел%20из%20телеграмма."

# Функция для регистрации нового пользователя или обновления информации
def register_user(pool, user_id, username, first_name, last_name, phone_number=None, name=None, city=None):
    try:
        conn = pool.get_connection()
        cursor = conn.cursor(buffered=True)
        cursor.execute('SELECT * FROM users WHERE user_id = %s', (user_id,))
        user_exists = cursor.fetchone()
        if not user_exists:
            cursor.execute('''
                INSERT INTO users (user_id, username, first_name, last_name, phone_number, name, city, join_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, CURDATE())
            ''', (user_id, username, first_name, last_name, phone_number, name, city))
            conn.commit()
            logger.info(f"Зарегистрирован новый пользователь: {user_id}")
        else:
            updates = []
            params = []
            if phone_number is not None:
                updates.append('phone_number = %s')
                params.append(phone_number)
            if name is not None:
                updates.append('name = %s')
                params.append(name)
            if city is not None:
                updates.append('city = %s')
                params.append(city)
            if updates:
                update_stmt = ', '.join(updates)
                params.append(user_id)
                cursor.execute(f'''
                    UPDATE users SET {update_stmt} WHERE user_id = %s
                ''', params)
                conn.commit()
                logger.info(f"Обновлены данные пользователя: {user_id}")
        cursor.close()
        conn.close()
    except mysql.connector.Error as err:
        logger.error(f"Ошибка при регистрации/обновлении пользователя {user_id}: {err}")

# Функция для проверки, зарегистрирован ли пользователь
def is_user_registered(pool, user_id):
    try:
        conn = pool.get_connection()
        cursor = conn.cursor(buffered=True)
        cursor.execute('SELECT * FROM users WHERE user_id = %s', (user_id,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()
        return user
    except mysql.connector.Error as err:
        logger.error(f"Ошибка при проверке регистрации пользователя {user_id}: {err}")
        return None

# Функция для получения имени пользователя из базы данных
def get_user_name(pool, user_id):
    try:
        conn = pool.get_connection()
        cursor = conn.cursor(buffered=True)
        cursor.execute('SELECT name FROM users WHERE user_id = %s', (user_id,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        return result[0] if result and result[0] else "Пользователь"
    except mysql.connector.Error as err:
        logger.error(f"Ошибка при получении имени пользователя {user_id}: {err}")
        return "Пользователь"

# Функция для получения города пользователя из базы данных
def get_user_city(pool, user_id):
    try:
        conn = pool.get_connection()
        cursor = conn.cursor(buffered=True)
        cursor.execute('SELECT city FROM users WHERE user_id = %s', (user_id,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        return result[0] if result and result[0] else "Не указан"
    except mysql.connector.Error as err:
        logger.error(f"Ошибка при получении города пользователя {user_id}: {err}")
        return "Не указан"

# Функция для проверки активности акции
def is_promo_active():
    today = datetime.date.today()
    weekday = today.weekday()  # 0 - понедельник, 6 - воскресенье
    # Акция активна с понедельника (0) до пятницы (4)
    return 0 <= weekday <= 4

# Функция для генерации уникального промокода
def generate_promo_code(length=8):
    letters_and_digits = string.ascii_uppercase + string.digits
    return ''.join(random.choice(letters_and_digits) for _ in range(length))

# Функция для валидации фильтров
def validate_filters(filters):
    # Проверка форматов значений и преобразование ключей в нужный формат
    key_mapping = {
        'price_min': 'priceFrom',
        'price_max': 'priceTo',
        'year_min': 'yearFrom',
        'year_max': 'yearTo',
        'body_type': 'bodyType',
        'transmission': 'transmission',
        'brand': 'brand',
        'model': 'model'
    }
    transmission_mapping = {
        'автомат': 'AKPP',  # Изменено на верхний регистр
        'механика': 'MT',
        'робот': 'ROBOT',
        'вариатор': 'VARIATOR'
    }
    body_type_mapping = {
        'седан': 'sedan',
        'хэтчбек': 'hatchback',
        'кроссовер': 'crossover',
        'suv': 'suv'
    }
    validated_filters = {}
    for key, value in filters.items():
        if value and str(value).lower() not in ['any', 'любая', 'любой']:
            mapped_key = key_mapping.get(key.lower(), key)
            if mapped_key in ['priceFrom', 'priceTo', 'yearFrom', 'yearTo']:
                try:
                    # Удаляем все, кроме цифр
                    numeric_value = int(re.sub(r'\D', '', str(value)))
                    # Дополнительная логика для корректировки цены
                    if mapped_key == 'priceTo' and numeric_value < 100000:  # Если priceTo < 100k, предполагаем миллион
                        numeric_value *= 1000000
                        logger.info(f"priceTo скорректирована до {numeric_value} тенге.")
                    validated_filters[mapped_key] = numeric_value
                except ValueError:
                    raise ValueError(f"Значение для {key} должно быть числом.")
            elif mapped_key == 'transmission':
                # Преобразование типа трансмиссии
                value_lower = str(value).lower()
                mapped_value = transmission_mapping.get(value_lower)
                if mapped_value:
                    validated_filters[mapped_key] = mapped_value
                else:
                    # Если значение не распознано, пропускаем
                    logger.warning(f"Некорректное значение для transmission: {value}")
                    continue
            elif mapped_key == 'bodyType':
                # Преобразование типа кузова
                value_lower = str(value).lower()
                mapped_value = body_type_mapping.get(value_lower)
                if mapped_value:
                    validated_filters[mapped_key] = mapped_value
                else:
                    # Если значение не распознано, пропускаем
                    logger.warning(f"Некорректный тип кузова: {value}")
                    continue
            elif mapped_key == 'color':
                # Исключаем 'color' из фильтров, так как ваш сайт не поддерживает фильтрацию по цвету
                continue
            else:
                validated_filters[mapped_key] = value
    logger.debug(f"Validated filters: {validated_filters}")
    return validated_filters

# Функция для формирования ссылки с фильтрами
# Функция для формирования ссылки с фильтрами
def create_filtered_url(filters):
    base_url = "https://aster.kz/cars"
    path_parts = []

    # Маппинг типов кузова к их URL-представлениям
    body_type_mapping = {
        'sedan': 'sedan',
        'hatchback': 'hatchback',
        'crossover': 'crossover',
        'suv': 'suv'
    }

    # Извлечение bodyType из filters и добавление в путь, если не "any"
    if 'bodyType' in filters:
        body_type_eng = filters.pop('bodyType').lower()
        if body_type_eng not in ['any', 'любая', 'любой']:
            body_type_eng = body_type_mapping.get(body_type_eng, 'all')
            path_parts.append(body_type_eng)
    # Если bodyType отсутствует или "any", не добавляем ничего

    # Если указана марка и не "any", добавляем ее в путь
    if 'brand' in filters:
        brand = filters.pop('brand').lower()
        if brand not in ['any', 'любая', 'любой']:
            brand_url = urllib.parse.quote(brand)
            path_parts.append(brand_url)
        # Если бренд "any", не добавляем ничего
    # Если brand отсутствует или "any", не добавляем ничего

    # Если указана модель и не "any", добавляем ее в путь
    if 'model' in filters:
        model = filters.pop('model').lower()
        if model not in ['any', 'любая', 'любой']:
            model_url = urllib.parse.quote(model)
            path_parts.append(model_url)
        # Если модель "any", не добавляем ничего
    # Если model отсутствует или "any", не добавляем ничего

    # Добавляем 'autosalon-ads' только если есть какие-либо сегменты пути
    if path_parts:
        path_parts.append('autosalon-ads')
        full_path = '/'.join(path_parts)
        final_url = f"{base_url}/{full_path}"
    else:
        # Если нет сегментов пути, сразу добавляем 'autosalon-ads'
        final_url = f"{base_url}/autosalon-ads"

    query_params = []

    # Преобразование каждого фильтра в параметр URL
    for key, value in filters.items():
        if value and str(value).lower() not in ['any', 'любая', 'любой']:
            encoded_value = urllib.parse.quote(str(value))
            query_params.append(f"{key}={encoded_value}")

    # Формирование окончательной ссылки
    if query_params:
        final_url = f"{final_url}?{'&'.join(query_params)}"

    logger.debug(f"Сформированная ссылка: {final_url}")
    return final_url

# Функция для отправки ссылки пользователю после подбора авто
def send_filtered_link(update: Update, context: CallbackContext, filtered_url: str) -> None:
    user_id = update.effective_user.id
    name = get_user_name(pool, user_id)

    # Логирование фильтров и URL
    logger.info(f"Формирование ссылки для пользователя {user_id}: {filtered_url}")

    # Создаем кнопку с ссылкой
    button = InlineKeyboardButton("🔗 Посмотреть все варианты", url=filtered_url)
    reply_markup = InlineKeyboardMarkup([[button]])

    # Отправка кнопки с ссылкой
    message = (
        f"{name}, вот ссылка на автомобили по вашим параметрам:"
    )
    context.bot.send_message(
        chat_id=user_id,
        text=message,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    logger.info(f"Отправлена ссылка пользователю {user_id}: {filtered_url}")

    # Обновление статистики
    update_statistics(pool, messages_sent=False, links_sent=True)

    # Устанавливаем таймер неактивности
    set_inactivity_timer(context, user_id)

# Функция для обновления статистики
def update_statistics(pool, messages_sent=False, links_sent=False):
    today = datetime.date.today()
    try:
        conn = pool.get_connection()
        cursor = conn.cursor(buffered=True)
        cursor.execute('SELECT * FROM statistics WHERE date = %s', (today,))
        record = cursor.fetchone()
        if record:
            cursor.execute('SELECT COUNT(*) FROM users')
            total_users = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM users WHERE join_date = %s', (today,))
            new_users = cursor.fetchone()[0]
            messages = record[4] + 1 if messages_sent else record[4]
            links = record[5] + 1 if links_sent else record[5]
            cursor.execute('''
                UPDATE statistics
                SET total_users = %s, new_users = %s, messages_sent = %s, links_sent = %s
                WHERE date = %s
            ''', (total_users, new_users, messages, links, today))
        else:
            cursor.execute('SELECT COUNT(*) FROM users')
            total_users = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM users WHERE join_date = %s', (today,))
            new_users = cursor.fetchone()[0]
            messages = 1 if messages_sent else 0
            links = 1 if links_sent else 0
            cursor.execute('''
                INSERT INTO statistics (date, total_users, new_users, messages_sent, links_sent)
                VALUES (%s, %s, %s, %s, %s)
            ''', (today, total_users, new_users, messages, links))
        conn.commit()
        cursor.close()
        conn.close()
    except mysql.connector.Error as err:
        logger.error(f"Ошибка при обновлении статистики: {err}")

# Функция для отправки сообщения о неактивности
def send_inactivity_message(context: CallbackContext):
    job = context.job
    user_id = job.context
    logger.info(f"Отправка сообщения о неактивности пользователю {user_id}")

    # Создаем кнопки
    keyboard = [
        [
            InlineKeyboardButton("🔍 Продолжить подбор", callback_data='menu:select_car'),
            InlineKeyboardButton("📞 Связаться с менеджером", url=WHATSAPP_LINK)
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Отправляем сообщение
    try:
        context.bot.send_message(
            chat_id=user_id,
            text="Можем ли мы помочь с подбором автомобиля? Или хотите связаться с менеджером?",
            reply_markup=reply_markup
        )
        logger.info(f"Сообщение о неактивности отправлено пользователю {user_id}")
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение о неактивности пользователю {user_id}: {e}")

# Функция для установки таймера неактивности
def set_inactivity_timer(context: CallbackContext, user_id: int):
    # Удаляем предыдущий таймер, если есть
    if 'inactivity_job' in context.user_data:
        context.job_queue.cancel(context.user_data['inactivity_job'])
        del context.user_data['inactivity_job']

    # Устанавливаем новый таймер на 30 минут (1800 секунд)
    job = context.job_queue.run_once(send_inactivity_message, when=1800, context=user_id)
    context.user_data['inactivity_job'] = job

# Функция для отмены диалога
def cancel(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    if 'conversation_history' in context.user_data:
        del context.user_data['conversation_history']
    if 'inactivity_job' in context.user_data:
        context.job_queue.cancel(context.user_data['inactivity_job'])
        del context.user_data['inactivity_job']
    update.message.reply_text(
        "Понял, если у вас возникнут вопросы, обращайтесь! 😊", reply_markup=ReplyKeyboardRemove()
    )
    logger.info(f"Диалог с пользователем {user_id} отменён.")

# Функция для регистрации нового пользователя или обновления информации
def start(update: Update, context: CallbackContext) -> int:
    user = update.effective_user
    user_id = user.id

    user_data = is_user_registered(pool, user_id)

    if user_data:
        phone_number = user_data[4]
        name = user_data[5]
        city = user_data[6]

        if not phone_number:
            # Запрос контакта пользователя
            contact_button = KeyboardButton('📱 Поделиться контактом', request_contact=True)
            reply_markup = ReplyKeyboardMarkup([[contact_button]], one_time_keyboard=True, resize_keyboard=True)
            update.message.reply_text(
                "Пожалуйста, поделитесь своим номером телефона или введите его для завершения регистрации:",
                reply_markup=reply_markup
            )
            return GET_CONTACT
        elif not name:
            update.message.reply_text("Как к вам обращаться?")
            return GET_NAME
        elif not city:
            update.message.reply_text("Пожалуйста, укажите ваш город.")
            return GET_CITY
        else:
            update.message.reply_text(f"С возвращением, {name}! Чем могу помочь?", reply_markup=ReplyKeyboardRemove())
            logger.info(f"Пользователь {user_id} вернулся.")
            # Отправляем главное меню с кнопкой "Связаться с менеджером"
            main_menu_keyboard = [
                [InlineKeyboardButton("🔍 Подобрать авто", callback_data='menu:select_car')],
                [InlineKeyboardButton("🎁 Мои призы", callback_data='menu:my_prizes')],
                [InlineKeyboardButton("📞 Связаться с менеджером", url=WHATSAPP_LINK)]
            ]
            update.message.reply_text(
                "Выберите действие:",
                reply_markup=InlineKeyboardMarkup(main_menu_keyboard)
            )
            return ConversationHandler.END
    else:
        # Сохранение базовой информации о пользователе
        register_user(pool, user_id, user.username, user.first_name, user.last_name)

        # Запрос контакта пользователя
        contact_button = KeyboardButton('📱 Поделиться контактом', request_contact=True)
        reply_markup = ReplyKeyboardMarkup([[contact_button]], one_time_keyboard=True, resize_keyboard=True)
        update.message.reply_text(
            "Здравствуйте! Рады видеть вас. Пожалуйста, поделитесь своим номером телефона или введите его для регистрации:",
            reply_markup=reply_markup
        )
        return GET_CONTACT

# Обработчик получения контакта
def get_contact(update: Update, context: CallbackContext) -> int:
    user = update.effective_user
    user_id = user.id

    if update.message.contact:
        phone_number = update.message.contact.phone_number
    else:
        # Пытаемся извлечь номер телефона из текста
        phone_number = update.message.text.strip()
        # Удаляем все, кроме цифр
        phone_number = re.sub(r'\D', '', phone_number)
        if not phone_number:
            update.message.reply_text("Пожалуйста, отправьте ваш номер телефона или воспользуйтесь кнопкой ниже.")
            return GET_CONTACT

    context.user_data['phone_number'] = phone_number

    # Обновление информации в базе данных
    register_user(pool, user_id, user.username, user.first_name, user.last_name, phone_number=phone_number)

    # Проверка, есть ли имя
    user_data = is_user_registered(pool, user_id)
    name = user_data[5]
    if not name:
        update.message.reply_text(
            "Спасибо! Как к вам обращаться?",
            reply_markup=ReplyKeyboardRemove()
        )
        return GET_NAME
    else:
        update.message.reply_text("Спасибо! Пожалуйста, укажите ваш город.", reply_markup=ReplyKeyboardRemove())
        return GET_CITY

# Обработчик получения имени
def get_name(update: Update, context: CallbackContext) -> int:
    user = update.effective_user
    user_id = user.id
    name = update.message.text.strip()
    context.user_data['name'] = name

    # Обновление информации в базе данных
    register_user(pool, user_id, user.username, user.first_name, user.last_name, name=name)

    update.message.reply_text(f"Приятно познакомиться, {name}!")

    # Запрос города пользователя
    update.message.reply_text("Пожалуйста, укажите ваш город.")
    return GET_CITY

# Обработчик получения города
def get_city(update: Update, context: CallbackContext) -> int:
    user = update.effective_user
    user_id = user.id
    city = update.message.text.strip()
    context.user_data['city'] = city

    # Обновление информации в базе данных
    register_user(pool, user_id, user.username, user.first_name, user.last_name, city=city)

    update.message.reply_text(f"Спасибо! Ваш город: {city}")

    # Предложение акции 'Щедрая пятница'
    if is_promo_active():
        update.message.reply_text(
            "У нас сейчас проходит акция 'Щедрая пятница'! Хотите принять участие и выбрать приз?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎁 Участвовать в акции", callback_data='user:select_prize')],
                [InlineKeyboardButton("🔗 Посмотреть все варианты", url="https://aster.kz/cars")]
            ])
        )
    else:
        # Отправляем главное меню с кнопкой "Связаться с менеджером"
        main_menu_keyboard = [
            [InlineKeyboardButton("🔍 Подобрать авто", callback_data='menu:select_car')],
            [InlineKeyboardButton("🎁 Мои призы", callback_data='menu:my_prizes')],
            [InlineKeyboardButton("📞 Связаться с менеджером", url=WHATSAPP_LINK)]
        ]
        update.message.reply_text(
            "Выберите действие:",
            reply_markup=InlineKeyboardMarkup(main_menu_keyboard)
        )

    # Устанавливаем таймер неактивности
    set_inactivity_timer(context, user_id)

    return ConversationHandler.END

# Функция для обработки выбора приза
def select_prize(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()

    # Проверяем, не выбрал ли пользователь уже приз
    try:
        conn = pool.get_connection()
        cursor = conn.cursor(buffered=True)
        cursor.execute('''
            SELECT * FROM user_prizes WHERE user_id = %s
        ''', (user_id,))
        if cursor.fetchone():
            query.edit_message_text("🎁 Вы уже выбрали приз и получили свой подарок.")
            cursor.close()
            conn.close()
            return
    except mysql.connector.Error as err:
        logger.error(f"Ошибка при проверке призов пользователя {user_id}: {err}")
        query.edit_message_text("⚠️ Произошла ошибка. Пожалуйста, попробуйте позже.")
        return

    cursor.close()
    conn.close()

    # Отображаем доступные призы для выбора с использованием уникальных ID
    prize_buttons = []
    for prize_id, prize_name in prize_id_mapping.items():
        prize_buttons.append([InlineKeyboardButton(prize_name, callback_data=f'user:prize:{prize_id}')])

    # Добавляем кнопку для связи с менеджером
    prize_buttons.append([InlineKeyboardButton("📞 Связаться с менеджером", url=WHATSAPP_LINK)])

    reply_markup = InlineKeyboardMarkup(prize_buttons)
    query.edit_message_text(
        "Пожалуйста, выберите один из доступных призов:",
        reply_markup=reply_markup
    )

# Обработчик выбора приза
def prize_selection_handler(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    query.answer()

    # Извлекаем выбранный prize_id
    match = re.match(r'user:prize:(\d+)', data)
    if not match:
        query.edit_message_text("⚠️ Произошла ошибка при выборе приза. Пожалуйста, попробуйте снова.")
        logger.error(f"Пользователь {user_id} отправил некорректные данные для выбора приза: {data}")
        return

    selected_prize_id = match.group(1)
    selected_prize_name = prize_id_mapping.get(selected_prize_id)

    if not selected_prize_name:
        query.edit_message_text("⚠️ Выбранный приз недоступен. Пожалуйста, выберите другой приз.")
        logger.warning(f"Пользователь {user_id} попытался выбрать недоступный приз: {selected_prize_id}")
        return

    promo_code = generate_promo_code()

    # Сохраняем приз пользователя в таблице user_prizes
    today = datetime.date.today()
    try:
        conn = pool.get_connection()
        cursor = conn.cursor(buffered=True)

        # Проверяем, существует ли приз в таблице prizes
        cursor.execute('''
            SELECT prize_id FROM prizes WHERE prize_name = %s
        ''', (selected_prize_name,))
        prize = cursor.fetchone()
        if prize:
            prize_id_db = prize[0]
        else:
            cursor.execute('''
                INSERT INTO prizes (prize_name)
                VALUES (%s)
            ''', (selected_prize_name,))
            conn.commit()
            prize_id_db = cursor.lastrowid

        cursor.execute('''
            INSERT INTO user_prizes (user_id, prize_id, promo_code, win_date)
            VALUES (%s, %s, %s, %s)
        ''', (user_id, prize_id_db, promo_code, today))
        conn.commit()
        cursor.close()
        conn.close()
    except mysql.connector.Error as err:
        logger.error(f"Ошибка при сохранении приза для пользователя {user_id}: {err}")
        query.edit_message_text("⚠️ Произошла ошибка при сохранении приза. Пожалуйста, попробуйте позже.")
        return

    # Получаем предпочтения пользователя
    try:
        conn = pool.get_connection()
        cursor = conn.cursor(buffered=True)
        cursor.execute('''
            SELECT preferences FROM user_requests
            WHERE user_id = %s ORDER BY timestamp DESC LIMIT 1
        ''', (user_id,))
        preferences_record = cursor.fetchone()
        if preferences_record:
            preferences = json.loads(preferences_record[0])
        else:
            preferences = {}
        cursor.close()
        conn.close()
    except (mysql.connector.Error, json.JSONDecodeError) as err:
        logger.error(f"Ошибка при получении предпочтений пользователя {user_id}: {err}")
        preferences = {}

    # Формирование ссылки с фильтрами
    try:
        filtered_url = create_filtered_url(preferences)
        logger.info(f"Сформированная ссылка для пользователя {user_id}: {filtered_url}")
    except Exception as e:
        logger.error(f"Ошибка при формировании ссылки для пользователя {user_id}: {e}")
        query.edit_message_text("⚠️ Произошла ошибка при формировании ссылки. Пожалуйста, попробуйте позже.")
        return

    # Создаем кнопку с ссылкой
    button = InlineKeyboardButton("🔗 Посмотреть все варианты", url=filtered_url)
    reply_markup = InlineKeyboardMarkup([[button]])

    # Получаем имя пользователя для персонализации сообщения
    user_name = get_user_name(pool, user_id)

    # Отправляем подтверждение и информацию о призе
    prize_message = (
        f"🎉 Поздравляем, {user_name}! Вы выбрали приз: *{selected_prize_name}* 🎁\n"
        f"📄 Ваш промокод: `{promo_code}`\n\n"
        "📝 Сохраните этот промокод для активации приза.\n\n"
        "*Приз активируется только при покупке в нашем автосалоне.*\n"
        f"*Приз действителен до окончания акции.*"
    )

    query.edit_message_text(
        prize_message,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

    logger.info(f"Пользователь {user_id} выбрал приз: {selected_prize_name} с промокодом {promo_code}")

    # Отправляем главное меню
    main_menu_keyboard = [
        [InlineKeyboardButton("🔍 Подобрать авто", callback_data='menu:select_car')],
        [InlineKeyboardButton("🎁 Мои призы", callback_data='menu:my_prizes')],
        [InlineKeyboardButton("📞 Связаться с менеджером", url=WHATSAPP_LINK)]
    ]
    context.bot.send_message(
        chat_id=user_id,
        text="Пожалуйста, выберите дальнейшее действие:",
        reply_markup=InlineKeyboardMarkup(main_menu_keyboard)
    )

# Функция для просмотра призов пользователя
def view_prizes(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.from_user.id

    # Проверяем, зарегистрирован ли пользователь
    if not is_user_registered(pool, user_id):
        query.edit_message_text("Пожалуйста, сначала зарегистрируйтесь с помощью команды /start.")
        return

    # Получаем призы пользователя
    try:
        conn = pool.get_connection()
        cursor = conn.cursor(buffered=True)
        cursor.execute('''
            SELECT prizes.prize_name, user_prizes.promo_code, user_prizes.win_date
            FROM prizes
            INNER JOIN user_prizes ON prizes.prize_id = user_prizes.prize_id
            WHERE user_prizes.user_id = %s
        ''', (user_id,))
        prizes = cursor.fetchall()
        cursor.close()
        conn.close()
    except mysql.connector.Error as err:
        logger.error(f"Ошибка при получении призов пользователя {user_id}: {err}")
        query.edit_message_text("⚠️ Произошла ошибка при получении ваших призов.")
        return

    if prizes:
        message = "🎁 **Ваши призы:**\n\n"
        for prize_name, promo_code, win_date in prizes:
            message += (
                f"- *{prize_name}*\n"
                f"  📄 Промокод: `{promo_code}`\n"
                f"  🗓 Выигран: {win_date}\n"
                f"  🔗 _Приз активируется только при покупке в нашем автосалоне._\n"
                f"  🕒 _Приз действителен до окончания акции._\n\n"
            )
    else:
        message = "🎁 У вас пока нет призов."

    query.edit_message_text(message, parse_mode='Markdown')

# Функция экспорта контактов
def export_contacts(update: Update, context: CallbackContext, period: str) -> None:
    query = update.callback_query
    user_id = query.from_user.id

    if period == 'week':
        date_from = datetime.date.today() - datetime.timedelta(days=7)
    elif period == 'month':
        date_from = datetime.date.today() - datetime.timedelta(days=30)
    elif period == 'all':
        date_from = None
    else:
        query.edit_message_text(text="❌ Некорректный период для экспорта.")
        logger.warning(f"Администратор {user_id} выбрал некорректный период: {period}")
        return

    file_name = f'contacts_{period}.xlsx'

    try:
        conn = pool.get_connection()
        cursor = conn.cursor(buffered=True)
        if date_from:
            cursor.execute('''
                SELECT phone_number, name, city FROM users WHERE join_date >= %s
            ''', (date_from,))
        else:
            cursor.execute('''
                SELECT phone_number, name, city FROM users
            ''')

        users_data = cursor.fetchall()
        data = []
        for user in users_data:
            phone_number, name, city = user
            data.append({
                'Phone Number': phone_number,
                'Name': name,
                'City': city
            })

        df = pd.DataFrame(data)

        # Сохранение в Excel
        df.to_excel(file_name, index=False)

        cursor.close()
        conn.close()

        # Отправка файла администратору
        try:
            with open(file_name, 'rb') as doc:
                context.bot.send_document(chat_id=user_id, document=doc)
            query.edit_message_text(text="📂 Экспорт контактов выполнен успешно.")
            logger.info(f"Администратор {user_id} выполнил экспорт контактов за период: {period}")
        except Exception as e:
            logger.error(f"Не удалось отправить файл экспорта контактов: {e}")
            query.edit_message_text(text="⚠️ Произошла ошибка при экспорте контактов.")
    except mysql.connector.Error as err:
        logger.error(f"Ошибка при экспорте контактов: {err}")
        query.edit_message_text(text="⚠️ Произошла ошибка при экспорте контактов.")
    except Exception as e:
        logger.error(f"Неизвестная ошибка при экспорте контактов: {e}")
        query.edit_message_text(text="⚠️ Произошла ошибка при экспорте контактов.")
    finally:
        # Удаляем временный файл
        if os.path.exists(file_name):
            os.remove(file_name)

# Функция админ-панели
def admin_panel(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        update.message.reply_text("🔒 У вас нет доступа к админ-панели.")
        logger.warning(f"Пользователь {user_id} попытался получить доступ к админ-панели.")
        return

    context.user_data['admin_mode'] = True  # Включаем режим администратора

    keyboard = [
        [InlineKeyboardButton("📣 Рассылка сообщений", callback_data='admin:broadcast')],
        [InlineKeyboardButton("📈 Просмотр статистики", callback_data='admin:stats')],
        [InlineKeyboardButton("📂 Экспорт контактов", callback_data='admin:export_contacts')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text("🔧 **Админ-панель:**", reply_markup=reply_markup)
    logger.info(f"Администратор {user_id} открыл админ-панель.")

# Функция админ-команды
def admin_command(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        update.message.reply_text("🔒 У вас нет доступа к админ-командам.")
        logger.warning(f"Пользователь {user_id} попытался использовать админ-команду.")
        return

    admin_panel(update, context)

# Обработчик админских кнопок
def admin_button_handler(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    parts = data.split(':')
    if len(parts) < 2:
        query.edit_message_text(text="❓ Неизвестная команда.")
        logger.warning(f"Администратор {user_id} отправил некорректные данные callback: {data}")
        return

    action = parts[1]

    if action == 'broadcast':
        query.edit_message_text(text="✉️ Пожалуйста, отправьте сообщение для рассылки всем пользователям (текст, фото или документ).")
        context.user_data['admin_action'] = 'broadcast'
        logger.info(f"Администратор {user_id} выбрал рассылку сообщений.")
    elif action == 'stats':
        # Получение статистики
        try:
            conn = pool.get_connection()
            cursor = conn.cursor(buffered=True)
            cursor.execute('SELECT COUNT(*) FROM users')
            total_users = cursor.fetchone()[0]

            today = datetime.date.today()
            cursor.execute('SELECT COUNT(*) FROM users WHERE join_date = %s', (today,))
            new_users_today = cursor.fetchone()[0]

            cursor.execute('SELECT SUM(messages_sent), SUM(links_sent) FROM statistics')
            result = cursor.fetchone()
            if result:
                messages_sent, links_sent = result
                messages_sent = messages_sent or 0
                links_sent = links_sent or 0
            else:
                messages_sent = 0
                links_sent = 0

            stats_message = (
                f"📊 **Статистика бота:**\n\n"
                f"• Всего пользователей: {total_users}\n"
                f"• Новых пользователей сегодня: {new_users_today}\n"
                f"• Сообщений отправлено: {messages_sent}\n"
                f"• Ссылок отправлено: {links_sent}"
            )
            query.edit_message_text(text=stats_message, parse_mode='Markdown')
            logger.info(f"Администратор {user_id} запросил статистику.")
            cursor.close()
            conn.close()
        except mysql.connector.Error as err:
            logger.error(f"Ошибка при получении статистики: {err}")
            query.edit_message_text(text="⚠️ Произошла ошибка при получении статистики.")
    elif action == 'export_contacts':
        query.edit_message_text(text="📂 Выберите период для экспорта контактов:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Последняя неделя", callback_data='admin:export:week')],
            [InlineKeyboardButton("📅 Последний месяц", callback_data='admin:export:month')],
            [InlineKeyboardButton("🗂 Все время", callback_data='admin:export:all')]
        ]))
        logger.info(f"Администратор {user_id} запросил экспорт контактов.")
    elif action == 'export' and len(parts) >= 3:
        period = parts[2]
        export_contacts(update, context, period)
    else:
        query.edit_message_text(text="❓ Неизвестная команда.")
        logger.warning(f"Администратор {user_id} выбрал неизвестную команду: {action}")

# Обработчик пользовательских кнопок
def user_button_handler(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    if data == 'user:select_prize':
        select_prize(update, context)
    elif data == 'menu:select_car':
        # Начинаем диалог по подбору авто
        query.edit_message_text(
            "Здравствуйте! Добро пожаловать в автосалон Aster auto. Меня зовут Асет, я ваш виртуальный менеджер.\n\n"
            "Готов помочь в подборе идеального варианта, учитывая ваши предпочтения и бюджет.\n\n"
            "Я здесь, чтобы сделать ваш опыт покупки максимально комфортным!\n\n"
            "Какой автомобиль вас интересует?"
        )
        # Инициализация истории диалога
        context.user_data['conversation_history'] = []
        # Устанавливаем таймер неактивности
        set_inactivity_timer(context, user_id)
    elif data == 'menu:my_prizes':
        # Показываем призы пользователя
        view_prizes(update, context)
    else:
        query.edit_message_text("❓ Неизвестная команда.")

# Обработчик нажатий на инлайн-кнопки
def button_handler(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    query.answer()

    # Разделяем обработку админских и пользовательских кнопок
    if data.startswith('admin:'):
        admin_button_handler(update, context)
    elif data.startswith('user:prize:'):
        prize_selection_handler(update, context)
    elif data.startswith('menu:') or data.startswith('user:'):
        user_button_handler(update, context)
    else:
        logger.error(f"Некорректные данные callback: {data}")
        query.edit_message_text(text="⚠️ Произошла ошибка. Попробуйте снова.")

# Функция для обработки сообщений администратора при рассылке
def admin_broadcast(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    sent = 0
    failed = 0

    try:
        conn = pool.get_connection()
        cursor = conn.cursor(buffered=True)
        cursor.execute('SELECT user_id FROM users')
        users = cursor.fetchall()
        cursor.close()
        conn.close()
    except mysql.connector.Error as err:
        logger.error(f"Ошибка при получении списка пользователей для рассылки: {err}")
        update.message.reply_text("⚠️ Произошла ошибка при получении списка пользователей.")
        return

    # Подготовка сообщения для рассылки
    message = update.message

    for user in users:
        try:
            if message.photo:
                # Отправка фото с подписью
                context.bot.send_photo(
                    chat_id=user[0],
                    photo=message.photo[-1].file_id,
                    caption=message.caption or ''
                )
            elif message.document:
                # Отправка документа с подписью
                context.bot.send_document(
                    chat_id=user[0],
                    document=message.document.file_id,
                    caption=message.caption or ''
                )
            elif message.text:
                # Отправка текстового сообщения
                context.bot.send_message(chat_id=user[0], text=message.text)
            else:
                logger.warning(f"Неизвестный тип сообщения от администратора {user_id}")
                continue
            sent += 1
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение пользователю {user[0]}: {e}")
            failed += 1

    update.message.reply_text(f"📬 **Рассылка завершена.**\n✅ Отправлено: {sent}\n❌ Не удалось отправить: {failed}")
    logger.info(f"Администратор {user_id} завершил рассылку: отправлено {sent}, не отправлено {failed}")
    del context.user_data['admin_action']
    context.user_data['admin_mode'] = False  # Выходим из режима администратора

# Функция для обработки пользовательских сообщений
def handle_message(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    user_input = update.message.text
    name = get_user_name(pool, user_id)
    city = get_user_city(pool, user_id)
    logger.info(f"Сообщение от пользователя {user_id}: {user_input}")

    # Проверка режима администратора
    if context.user_data.get('admin_action') and user_id in ADMIN_IDS:
        admin_broadcast(update, context)
        return

    # Проверка, есть ли 'conversation_history' в user_data
    if 'conversation_history' not in context.user_data:
        # Если нет, предлагаем пользователю выбрать действие
        main_menu_keyboard = [
            [InlineKeyboardButton("🔍 Подобрать авто", callback_data='menu:select_car')],
            [InlineKeyboardButton("🎁 Мои призы", callback_data='menu:my_prizes')],
            [InlineKeyboardButton("📞 Связаться с менеджером", url=WHATSAPP_LINK)]
        ]
        update.message.reply_text(
            "Пожалуйста, выберите действие:",
            reply_markup=InlineKeyboardMarkup(main_menu_keyboard)
        )
        return

    # Получение текущей истории диалога
    conversation_history = context.user_data.get('conversation_history', [])
    conversation_history.append({"role": "user", "content": user_input})

    # Формирование запроса к GPT
    prompt_messages = [
        {"role": "system", "content": (
            "Ты помощник для подбора автомобилей на сайте aster.kz. "
            "Веди диалог с пользователем на русском или казахском языке, задавай уточняющие вопросы, "
            "извлекай необходимые параметры для фильтрации автомобилей, такие как марка, модель, год выпуска, тип кузова, коробка передач, бюджет и т.д., "
            "но не спрашивай про тип топлива, пробег и не спрашивай новый или б/у, все автомобили б/у. "
            "Когда соберёшь достаточно информации, представь параметры фильтрации в формате JSON, "
            "начиная с ключевого слова 'Фильтры:'. "
            "Убедись, что все значения соответствуют ожидаемым форматам (например, числа для бюджета). "
            "Если какие-то данные некорректны или отсутствуют, автоматически исправь их или запроси уточнения. "
            "Если значение параметра не имеет значения, не включай его в фильтры. "
            "Затем предоставь пользователю ссылку на полный список подходящих автомобилей. "
            "Вот пример корректного ответа:\n"
            "Фильтры:\n```json\n{\n  \"priceTo\": 6000000,\n  \"bodyType\": \"sedan\",\n  \"brand\": \"bmw\"\n}\n```\n"
            "Ссылка: [Посмотреть все варианты](https://aster.kz/cars/sedan/bmw/autosalon-ads?yearFrom=2000&priceTo=6000000&transmission=AKPP)\n"
            "Пожалуйста, используй точные ключи и значения в фильтрах, как указано."
        )}
    ] + conversation_history

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",  # Исправлено на корректную модель
            messages=prompt_messages,
            max_tokens=150,
            temperature=0.7,
        )

        gpt_reply = response.choices[0].message['content'].strip()
        logger.info(f"Ответ GPT для пользователя {user_id}: {gpt_reply}")

        # Добавление ответа GPT в историю
        conversation_history.append({"role": "assistant", "content": gpt_reply})
        context.user_data['conversation_history'] = conversation_history

        # Логирование полного ответа GPT для отладки
        logger.debug(f"Полный ответ GPT для пользователя {user_id}: {gpt_reply}")

        # Проверка, содержит ли ответ JSON с фильтрами
        if re.search(r'фильтры\s*:', gpt_reply, re.IGNORECASE):
            # Извлечение JSON из ответа GPT
            try:
                # Попробуйте извлечь JSON из блока кода
                json_match = re.search(r'фильтры\s*:\s*```json\s*(\{.*?\})\s*```', gpt_reply, re.DOTALL | re.IGNORECASE)
                if json_match:
                    json_str = json_match.group(1)
                else:
                    # Альтернативный способ извлечения JSON
                    json_start = gpt_reply.lower().find('фильтры:') + len('фильтры:')
                    json_str = gpt_reply[json_start:].strip()

                    # Найти первый '{' и последний '}' для извлечения JSON
                    json_start_brace = json_str.find('{')
                    json_end_brace = json_str.rfind('}')
                    if json_start_brace != -1 and json_end_brace != -1:
                        json_str = json_str[json_start_brace:json_end_brace+1]
                    else:
                        raise ValueError("Невозможно найти корректный JSON в ответе GPT.")

                # Попытаться загрузить JSON
                filters = json.loads(json_str)
                logger.info(f"Извлечённые фильтры: {filters}")

                # Валидация фильтров
                filters = validate_filters(filters)

                # Сохранение предпочтений пользователя в базе данных
                preferences_str = json.dumps(filters, ensure_ascii=False)
                conn = pool.get_connection()
                cursor = conn.cursor(buffered=True)
                cursor.execute('''
                    INSERT INTO user_requests (user_id, preferences, timestamp)
                    VALUES (%s, %s, NOW())
                ''', (user_id, preferences_str))
                conn.commit()
                cursor.close()
                conn.close()

                # Формирование ссылки с фильтрами
                filtered_url = create_filtered_url(filters)
                logger.info(f"Сформированная ссылка для пользователя {user_id}: {filtered_url}")

                # Отправка ссылки с кнопкой
                send_filtered_link(update, context, filtered_url)

                # Обновление статистики
                update_statistics(pool, messages_sent=False, links_sent=True)

                # Завершение диалога
                del context.user_data['conversation_history']

            except (json.JSONDecodeError, ValueError, AttributeError) as e:
                logger.error(f"Ошибка при декодировании JSON для пользователя {user_id}: {e}")
        else:
            # Отправка ответа GPT пользователю
            update.message.reply_text(gpt_reply)

            # Обновление статистики сообщений
            update_statistics(pool, messages_sent=True, links_sent=False)

    except openai.error.OpenAIError as e:
        logger.error(f"OpenAI API ошибка для пользователя {user_id}: {e}")
        update.message.reply_text("⚠️ Произошла ошибка при обработке вашего запроса. Пожалуйста, попробуйте позже.")
    except Exception as e:
        logger.error(f"Неизвестная ошибка для пользователя {user_id}: {e}")
        update.message.reply_text("⚠️ Произошла ошибка. Пожалуйста, попробуйте позже.")

# Функция для обработки ошибок
def error_handler(update: object, context: CallbackContext) -> None:
    """Обработчик ошибок для логирования и уведомления пользователей."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    # Проверяем, является ли ошибка Conflict
    if isinstance(context.error, telegram_error.Conflict):
        # Это системная ошибка, не связанная с пользователем
        logger.warning("Получена ошибка Conflict. Возможно, запущен другой экземпляр бота.")
        return  # Не отправляем сообщение пользователю
    # Пытаемся определить, связано ли обновление с сообщением
    if isinstance(update, Update) and update.effective_message:
        update.effective_message.reply_text("⚠️ Произошла внутренняя ошибка. Пожалуйста, попробуйте позже.")
    else:
        logger.warning("Произошла ошибка, но невозможно определить источник для уведомления пользователя.")

# Функция для удаления призов после окончания акции
def delete_all_prizes(context: CallbackContext):
    try:
        conn = pool.get_connection()
        cursor = conn.cursor(buffered=True)
        cursor.execute('DELETE FROM user_prizes')
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("Все призы удалены после окончания акции.")
    except mysql.connector.Error as err:
        logger.error(f"Ошибка при удалении призов: {err}")

# Основная функция
def main():
    try:
        updater = Updater(TELEGRAM_API_TOKEN)  # Удален use_context=True, так как он по умолчанию True в новых версиях
        dispatcher = updater.dispatcher

        # Определение ConversationHandler для /start
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', start)],
            states={
                GET_CONTACT: [MessageHandler(Filters.contact | (Filters.text & ~Filters.command), get_contact)],
                GET_NAME: [MessageHandler(Filters.text & ~Filters.command, get_name)],
                GET_CITY: [MessageHandler(Filters.text & ~Filters.command, get_city)],
            },
            fallbacks=[CommandHandler('cancel', cancel)],
        )

        # Обработчики команд и сообщений
        dispatcher.add_handler(conv_handler)
        dispatcher.add_handler(CommandHandler('cancel', cancel))
        dispatcher.add_handler(CommandHandler('admin', admin_command))
        dispatcher.add_handler(CallbackQueryHandler(button_handler))
        dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
        dispatcher.add_handler(MessageHandler(Filters.photo | Filters.document, handle_message))

        # Обработчик ошибок
        dispatcher.add_error_handler(error_handler)

        # Планирование задачи для удаления призов после окончания акции
        job_queue = updater.job_queue
        # Устанавливаем время субботы в 12:00 дня
        now = datetime.datetime.now()
        days_ahead = (5 - now.weekday()) % 7  # 5 - суббота
        next_saturday = now + datetime.timedelta(days=days_ahead)
        next_saturday = next_saturday.replace(hour=12, minute=0, second=0, microsecond=0)
        if next_saturday < now:
            next_saturday += datetime.timedelta(weeks=1)
        delay = (next_saturday - now).total_seconds()
        job_queue.run_once(delete_all_prizes, when=delay, name="delete_all_prizes")
        logger.info(f"🕒 Задача на удаление призов запланирована через {int(delay)} секунд.")

        # Планирование задачи для удаления призов еженедельно в субботу
        job_queue.run_repeating(delete_all_prizes, interval=604800, first=delay)  # 604800 секунд = 1 неделя

        logger.info("🚀 Запуск бота...")
        # Запуск бота
        updater.start_polling()
        updater.idle()
    except Exception as e:
        logger.error(f"❌ Ошибка при запуске бота: {e}")

# Запуск основного функционала
if __name__ == '__main__':
    main()