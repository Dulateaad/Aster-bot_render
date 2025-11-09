# main_bot.py

import logging
import os
import pandas as pd
import asyncio
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import MediaGroup, InputMediaPhoto, InputFile
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.dispatcher import FSMContext
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher.middlewares import BaseMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import utc
from config import MAIN_BOT_TOKEN, ADMIN_IDS, MANAGER_IDS
from database import Database
from aiogram.utils.exceptions import Throttled

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize bot and dispatcher
bot = Bot(token=MAIN_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
db = Database()
scheduler = AsyncIOScheduler(timezone=utc)

# Define FSM States
class ContactInfoState(StatesGroup):
    name = State()
    phone = State()
    city = State()

class DiscountState(StatesGroup):
    desired_price = State()

class MailingStates(StatesGroup):
    message = State()

class AdStates(StatesGroup):
    title = State()
    model = State()
    year = State()
    price = State()
    description = State()
    photos = State()
    inspection_photos = State()
    thickness_photos = State()

class SubscriptionStates(StatesGroup):
    model = State()
    price_min = State()
    price_max = State()
    year_min = State()
    year_max = State()

class SupportState(StatesGroup):
    waiting_for_message = State()
    chatting = State()

class PaymentState(StatesGroup):
    waiting_for_receipt = State()

# Middleware to update last_active timestamp
class LastActiveMiddleware(BaseMiddleware):
    async def on_pre_process_message(self, message: types.Message, data: dict):
        try:
            await db.update_last_active(message.from_user.id)
        except Exception as e:
            logger.error(f"Ошибка при обновлении last_active для пользователя {message.from_user.id}: {e}")

    async def on_pre_process_callback_query(self, callback_query: types.CallbackQuery, data: dict):
        try:
            await db.update_last_active(callback_query.from_user.id)
        except Exception as e:
            logger.error(f"Ошибка при обновлении last_active для пользователя {callback_query.from_user.id}: {e}")

# Middleware to check if the bot is open
class AccessMiddleware(BaseMiddleware):
    async def on_pre_process_message(self, message: types.Message, data: dict):
        user_id = message.from_user.id
        if user_id in ADMIN_IDS:
            return  # Администраторы всегда имеют доступ

        try:
            is_open = await db.is_bot_open()
        except Exception as e:
            logger.error(f"Ошибка при проверке состояния бота: {e}")
            await message.answer("Произошла ошибка. Пожалуйста, попробуйте позже.")
            raise Throttled()  # Прекратить дальнейшую обработку

        try:
            user = await db.get_user(user_id)
        except Exception as e:
            logger.error(f"Ошибка при получении пользователя {user_id}: {e}")
            await message.answer("Произошла ошибка. Пожалуйста, попробуйте позже.")
            raise Throttled()

        if is_open:
            return  # Бот открыт, доступ разрешен всем
        else:
            if user and user['status'] == 'approved':
                return  # Одобренные пользователи имеют доступ, даже если бот закрыт
            # Если бот закрыт и пользователь не одобрен
            if message.chat.type == 'private':
                await message.answer("Бот в данный момент закрыт для новых пользователей. Пожалуйста, попробуйте позже.")
                raise Throttled()  # Прекратить дальнейшую обработку

    async def on_pre_process_callback_query(self, callback_query: types.CallbackQuery, data: dict):
        user_id = callback_query.from_user.id
        if user_id in ADMIN_IDS:
            return  # Администраторы всегда имеют доступ

        try:
            is_open = await db.is_bot_open()
        except Exception as e:
            logger.error(f"Ошибка при проверке состояния бота: {e}")
            await callback_query.answer("Произошла ошибка. Пожалуйста, попробуйте позже.", show_alert=True)
            raise Throttled()

        try:
            user = await db.get_user(user_id)
        except Exception as e:
            logger.error(f"Ошибка при получении пользователя {user_id}: {e}")
            await callback_query.answer("Произошла ошибка. Пожалуйста, попробуйте позже.", show_alert=True)
            raise Throttled()

        if is_open:
            return  # Бот открыт, доступ разрешен всем
        else:
            if user and user['status'] == 'approved':
                return  # Одобренные пользователи имеют доступ, даже если бот закрыт
            # Если бот закрыт и пользователь не одобрен
            await callback_query.answer("Бот в данный момент закрыт для новых пользователей.", show_alert=True)
            raise Throttled()  # Прекратить дальнейшую обработку

# Setup middlewares
dp.middleware.setup(LastActiveMiddleware())
dp.middleware.setup(AccessMiddleware())

# Function to send daily notifications
async def send_daily_notifications():
    cutoff_time = datetime.utcnow() - timedelta(hours=24)
    try:
        users = await db.get_inactive_users(cutoff_time)
    except Exception as e:
        logger.error(f"Ошибка при получении неактивных пользователей: {e}")
        return

    if not users:
        logger.info("Нет неактивных пользователей для уведомления.")
        return

    try:
        new_ads_count = await db.get_new_ads_count(cutoff_time)
    except Exception as e:
        logger.error(f"Ошибка при получении количества новых объявлений: {e}")
        return

    if new_ads_count == 0:
        logger.info("Нет новых объявлений за последние 24 часа.")
        return

    for user in users:
        user_id = user['user_id']
        try:
            await bot.send_message(user_id, f"У нас появилось {new_ads_count} новых объявлений! Зайдите в бота, чтобы посмотреть.")
            logger.info(f"Уведомление отправлено пользователю {user_id}.")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление пользователю {user_id}: {e}")

# Function to run on startup
async def on_startup(dp):
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await db.connect()
        scheduler.add_job(send_daily_notifications, 'cron', hour=9, timezone=utc)
        scheduler.start()
        logger.info("Планировщик задач запущен")
    except Exception as e:
        logger.critical(f"Ошибка при запуске бота: {e}")

# Handler for /start command
@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message):
    user_id = message.from_user.id
    try:
        is_open = await db.is_bot_open()
    except Exception as e:
        logger.error(f"Ошибка при проверке состояния бота: {e}")
        await message.answer("Произошла ошибка. Пожалуйста, попробуйте позже.")
        return

    try:
        user = await db.get_user(user_id)
    except Exception as e:
        logger.error(f"Ошибка при получении данных пользователя {user_id}: {e}")
        await message.answer("Произошла ошибка. Пожалуйста, попробуйте позже.")
        return

    if is_open:
        if user:
            if user['status'] == 'approved':
                # Проверка контактной информации
                if user['name'] and user['phone'] and user['city']:
                    await message.answer("Ваш доступ уже подтвержден. Вы можете пользоваться ботом.", reply_markup=main_menu_keyboard())
                else:
                    await message.answer("Ваш доступ подтвержден. Пожалуйста, предоставьте вашу контактную информацию.")
                    await ContactInfoState.name.set()
                    await message.answer("Пожалуйста, введите ваше имя:")
            else:
                # Пользователь существует, но не одобрен (должно быть неактуально в открытом режиме)
                await message.answer("Ваш статус не позволяет использовать бота. Свяжитесь с администратором.")
        else:
            # Новый пользователь, бот открыт - регистрируем без оплаты
            try:
                await db.add_user(user_id, message.from_user.username, status='approved')
                await message.answer("Добро пожаловать! Пожалуйста, предоставьте вашу контактную информацию.")
                await ContactInfoState.name.set()
                await message.answer("Пожалуйста, введите ваше имя:")
            except Exception as e:
                logger.error(f"Ошибка при добавлении пользователя {user_id}: {e}")
                await message.answer("Произошла ошибка. Пожалуйста, попробуйте позже.")
    else:
        if user and user['status'] == 'approved':
            # Одобренный пользователь, доступ разрешен
            if user['name'] and user['phone'] and user['city']:
                await message.answer("Ваш доступ уже подтвержден. Вы можете пользоваться ботом.", reply_markup=main_menu_keyboard())
            else:
                await message.answer("Ваш доступ подтвержден. Пожалуйста, предоставьте вашу контактную информацию.")
                await ContactInfoState.name.set()
                await message.answer("Пожалуйста, введите ваше имя:")
        else:
            # Бот закрыт, пользователь не одобрен или новый
            if user and user['status'] == 'pending':
                await message.answer("Ваш запрос уже отправлен на одобрение. Ожидайте подтверждения администратора.")
            else:
                # Новый пользователь, бот закрыт - требуется оплата и отправка чека
                try:
                    await db.add_user(user_id, message.from_user.username, status='pending')
                except Exception as e:
                    logger.error(f"Ошибка при добавлении пользователя {user_id}: {e}")
                    await message.answer("Произошла ошибка. Пожалуйста, попробуйте позже.")
                    return
                await message.answer(
                    "Это закрытая платформа. Для доступа оплатите `10.000` Тенге на Kaspi Gold `+77028517037` (Гульбаршин.К).\nПосле оплаты нажмите 'Я оплатил' и отправьте чек.",
                    reply_markup=payment_keyboard()
                )

# Payment keyboard
def payment_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("Я оплатил")
    return keyboard

# Handler for "Я оплатил" message
@dp.message_handler(lambda message: message.text == "Я оплатил")
async def process_payment(message: types.Message, state: FSMContext):
    await message.answer("Пожалуйста, отправьте чек одним сообщением (фото или файл).")
    await PaymentState.waiting_for_receipt.set()

# Handler to receive cheque (restricted to PaymentState)
@dp.message_handler(content_types=['photo', 'document'], state=PaymentState.waiting_for_receipt)
async def receive_cheque(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    # Сохраняем чек и обновляем статус пользователя
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id
    else:
        await message.answer("Неправильный формат чека.")
        return

    try:
        await db.update_user_cheque(user_id, file_id)
    except Exception as e:
        logger.error(f"Ошибка при обновлении чека пользователя {user_id}: {e}")
        await message.answer("Произошла ошибка при сохранении чека. Пожалуйста, попробуйте позже.")
        return

    await message.answer("Ваш чек отправлен на проверку. Ожидайте подтверждения.")

    # Уведомляем администраторов
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"Новая заявка от @{message.from_user.username}",
                reply_markup=admin_user_keyboard(user_id)
            )
            if message.photo:
                await bot.send_photo(admin_id, file_id)
            elif message.document:
                await bot.send_document(admin_id, file_id)
        except Exception as e:
            logger.error(f"Не удалось уведомить администратора {admin_id}: {e}")

    await state.finish()

# Admin user management keyboard
def admin_user_keyboard(user_id):
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton("Подтвердить", callback_data=f"approve_{user_id}"),
        types.InlineKeyboardButton("Отклонить", callback_data=f"reject_{user_id}")
    )
    return keyboard

# Handler for admin callbacks to approve or reject users
@dp.callback_query_handler(lambda c: c.data and c.data.startswith(('approve_', 'reject_')))
async def process_callback_admin_user(callback_query: types.CallbackQuery):
    user_id = int(callback_query.data.split('_')[1])
    action = callback_query.data.split('_')[0]

    if callback_query.from_user.id not in ADMIN_IDS:
        await callback_query.answer("У вас нет прав.", show_alert=True)
        return

    if action == 'approve':
        try:
            await db.update_user_status(user_id, 'approved')
            # Запрашиваем контактную информацию
            await bot.send_message(user_id, "Ваш доступ подтвержден. Пожалуйста, предоставьте вашу контактную информацию.")
            await ContactInfoState.name.set()
            await bot.send_message(user_id, "Пожалуйста, введите ваше имя:")
            await callback_query.answer("Пользователь подтвержден.")
        except Exception as e:
            logger.error(f"Ошибка при подтверждении пользователя {user_id}: {e}")
            await callback_query.answer("Произошла ошибка при подтверждении пользователя.", show_alert=True)
    elif action == 'reject':
        try:
            await db.update_user_status(user_id, 'rejected')
            await bot.send_message(user_id, "Ваш доступ отклонен. Обратитесь к администратору.")
            await callback_query.answer("Пользователь отклонен.")
        except Exception as e:
            logger.error(f"Ошибка при отклонении пользователя {user_id}: {e}")
            await callback_query.answer("Произошла ошибка при отклонении пользователя.", show_alert=True)

# Handlers to collect contact information
@dp.message_handler(state=ContactInfoState.name)
async def get_name(message: types.Message, state: FSMContext):
    if message.text.lower() == 'отмена':
        await state.finish()
        await message.answer("Действие отменено.", reply_markup=main_menu_keyboard())
        return
    if not message.text.strip():
        await message.answer("Имя не может быть пустым. Пожалуйста, введите ваше имя:")
        return
    await state.update_data(name=message.text.strip())
    await message.answer("Пожалуйста, введите ваш номер телефона:")
    await ContactInfoState.phone.set()

@dp.message_handler(state=ContactInfoState.phone, content_types=['text', 'contact'])
async def get_phone(message: types.Message, state: FSMContext):
    if message.text and message.text.lower() == 'отмена':
        await state.finish()
        await message.answer("Действие отменено.", reply_markup=main_menu_keyboard())
        return
    if message.contact:
        phone_number = message.contact.phone_number
    else:
        phone_number = message.text.strip()
        if not phone_number:
            await message.answer("Номер телефона не может быть пустым. Пожалуйста, введите ваш номер телефона:")
            return
    await state.update_data(phone=phone_number)
    await message.answer("Пожалуйста, введите ваш город:")
    await ContactInfoState.city.set()

@dp.message_handler(state=ContactInfoState.city)
async def get_city(message: types.Message, state: FSMContext):
    if message.text.lower() == 'отмена':
        await state.finish()
        await message.answer("Действие отменено.", reply_markup=main_menu_keyboard())
        return
    city = message.text.strip()
    if not city:
        await message.answer("Город не может быть пустым. Пожалуйста, введите ваш город:")
        return
    await state.update_data(city=city)
    data = await state.get_data()
    # Сохраняем контактную информацию в базе данных
    try:
        await db.update_user_contact(message.from_user.id, data['name'], data['phone'], data['city'])
    except Exception as e:
        logger.error(f"Ошибка при обновлении контактной информации пользователя {message.from_user.id}: {e}")
        await message.answer("Произошла ошибка при сохранении контактной информации. Пожалуйста, попробуйте позже.")
        return
    await message.answer("Спасибо! Вы можете пользоваться ботом.", reply_markup=main_menu_keyboard())
    await state.finish()

# Main menu keyboard
def main_menu_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("Список всех объявлений", "Избранные объявления", "Подписки")
    keyboard.add("Поддержка")
    if ADMIN_IDS:
        keyboard.add("Админ Панель")
    return keyboard

# Show main menu
async def show_menu(message: types.Message):
    await message.answer("Выберите действие:", reply_markup=main_menu_keyboard())

# Handler for main menu options
@dp.message_handler(lambda message: message.text in ["Список всех объявлений", "Избранные объявления", "Подписки", "Поддержка", "Админ Панель"])
async def process_main_menu(message: types.Message, state: FSMContext):
    try:
        user = await db.get_user(message.from_user.id)
    except Exception as e:
        logger.error(f"Ошибка при получении данных пользователя {message.from_user.id}: {e}")
        await message.answer("Произошла ошибка. Пожалуйста, попробуйте позже.")
        return

    if not user or user['status'] != 'approved':
        await message.answer("У вас нет доступа. Пожалуйста, оплатите доступ.")
        return

    if message.text == "Список всех объявлений":
        try:
            ads = await db.get_ads()
        except Exception as e:
            logger.error(f"Ошибка при получении объявлений: {e}")
            await message.answer("Произошла ошибка при получении объявлений. Пожалуйста, попробуйте позже.")
            return
        if not ads:
            await message.answer("Нет доступных объявлений.")
            return
        await state.update_data(ads=ads, current_ad_index=0)
        await show_ad_with_navigation(message, state)
    elif message.text == "Избранные объявления":
        await show_favorites(message, state)
    elif message.text == "Подписки":
        await manage_subscriptions(message)
    elif message.text == "Поддержка":
        await start_support(message)
    elif message.text == "Админ Панель":
        if message.from_user.id not in ADMIN_IDS:
            await message.answer("У вас нет доступа.")
            return
        await admin_panel(message)

# Handler to cancel any state
@dp.message_handler(lambda message: message.text.lower() == 'отмена', state='*')
async def cancel_handler(message: types.Message, state: FSMContext):
    await state.finish()
    await message.answer("Действие отменено.", reply_markup=main_menu_keyboard())

# Function to display ads with navigation
async def show_ad_with_navigation(message_or_callback, state: FSMContext, edit=False):
    data = await state.get_data()
    ads = data.get('ads', [])
    current_ad_index = data.get('current_ad_index', 0)
    if not ads:
        if isinstance(message_or_callback, types.Message):
            await message_or_callback.answer("Нет объявлений для отображения.")
        elif isinstance(message_or_callback, types.CallbackQuery):
            await message_or_callback.answer("Нет объявлений для отображения.")
        return

    ad = ads[current_ad_index]
    # Unpack ad details
    ad_id = ad['ad_id']
    title = ad['title']
    price = ad['price']
    description = ad['description']
    photos = ad['photos']
    inspection_photos = ad['inspection_photos']
    thickness_photos = ad['thickness_photos']
    model = ad['model']
    year = ad['year']
    added_date = ad['added_date']
    caption = f"{title}\nМодель: {model}\nГод выпуска: {year}\nЦена: {price} KZT"

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton("Купить", callback_data=f"buy_{ad_id}"),
        types.InlineKeyboardButton("Запросить скидку", callback_data=f"discount_{ad_id}"),
    )
    keyboard.add(
        types.InlineKeyboardButton("Полное описание", callback_data=f"description_{ad_id}"),
        types.InlineKeyboardButton("Акт осмотра", callback_data=f"inspection_{ad_id}"),
        types.InlineKeyboardButton("Толщиномер", callback_data=f"thickness_{ad_id}")
    )

    # Check if ad is in favorites
    try:
        is_fav = await db.is_favorite(message_or_callback.from_user.id, ad_id)
    except Exception as e:
        logger.error(f"Ошибка при проверке избранного для пользователя {message_or_callback.from_user.id}: {e}")
        is_fav = False

    if is_fav:
        fav_button = types.InlineKeyboardButton("Убрать из избранного ❤️", callback_data=f"remove_fav_{ad_id}")
    else:
        fav_button = types.InlineKeyboardButton("Добавить в избранное 🤍", callback_data=f"add_fav_{ad_id}")
    keyboard.add(fav_button)

    # Add "Show all photos" button
    keyboard.add(types.InlineKeyboardButton("Показать все фото", callback_data=f"show_photos_{ad_id}"))

    # Navigation buttons
    navigation_buttons = []
    if current_ad_index > 0:
        navigation_buttons.append(types.InlineKeyboardButton("« Предыдущее", callback_data="prev_ad"))
    if current_ad_index < len(ads) - 1:
        navigation_buttons.append(types.InlineKeyboardButton("Следующее »", callback_data="next_ad"))
    if navigation_buttons:
        keyboard.row(*navigation_buttons)

    if photos:
        file_id = photos[0]  # Показываем только первое фото
        if edit and isinstance(message_or_callback, types.CallbackQuery):
            try:
                media = InputMediaPhoto(media=file_id, caption=caption)
                await message_or_callback.message.edit_media(media=media, reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Ошибка при редактировании медиа: {e}")
        else:
            try:
                await bot.send_photo(chat_id=message_or_callback.from_user.id, photo=file_id, caption=caption, reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Ошибка при отправке фото: {e}")
    else:
        if edit and isinstance(message_or_callback, types.CallbackQuery):
            try:
                await message_or_callback.message.edit_text(caption, reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Ошибка при редактировании текста: {e}")
        else:
            try:
                await message_or_callback.answer(caption, reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Ошибка при отправке сообщения: {e}")

# Handlers to navigate through ads
@dp.callback_query_handler(lambda c: c.data in ["prev_ad", "next_ad"])
async def navigate_ads(callback_query: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    ads = data.get('ads', [])
    current_ad_index = data.get('current_ad_index', 0)
    if not ads:
        await callback_query.answer("Нет объявлений для отображения.")
        return

    if callback_query.data == "next_ad" and current_ad_index < len(ads) - 1:
        current_ad_index += 1
    elif callback_query.data == "prev_ad" and current_ad_index > 0:
        current_ad_index -= 1
    else:
        await callback_query.answer("Больше объявлений нет.")
        return

    await state.update_data(current_ad_index=current_ad_index)
    await show_ad_with_navigation(callback_query, state, edit=True)
    await callback_query.answer()

# Handlers for favorite ads
async def show_favorites(message: types.Message, state: FSMContext):
    try:
        ads = await db.get_favorite_ads(message.from_user.id)
    except Exception as e:
        logger.error(f"Ошибка при получении избранных объявлений пользователя {message.from_user.id}: {e}")
        await message.answer("Произошла ошибка при получении избранных объявлений. Пожалуйста, попробуйте позже.")
        return
    if not ads:
        await message.answer("У вас нет избранных объявлений.")
        return
    await state.update_data(ads=ads, current_ad_index=0)
    await show_ad_with_navigation(message, state)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('add_fav_'))
async def add_to_favorites(callback_query: types.CallbackQuery, state: FSMContext):
    ad_id = int(callback_query.data.split('_')[2])
    try:
        await db.add_to_favorites(callback_query.from_user.id, ad_id)
        await callback_query.answer("Добавлено в избранное.")
        await show_ad_with_navigation(callback_query, state, edit=True)
    except Exception as e:
        logger.error(f"Ошибка при добавлении в избранное пользователя {callback_query.from_user.id}: {e}")
        await callback_query.answer("Произошла ошибка при добавлении в избранное.", show_alert=True)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('remove_fav_'))
async def remove_from_favorites(callback_query: types.CallbackQuery, state: FSMContext):
    ad_id = int(callback_query.data.split('_')[2])
    try:
        await db.remove_from_favorites(callback_query.from_user.id, ad_id)
        await callback_query.answer("Удалено из избранного.")
        await show_ad_with_navigation(callback_query, state, edit=True)
    except Exception as e:
        logger.error(f"Ошибка при удалении из избранного пользователя {callback_query.from_user.id}: {e}")
        await callback_query.answer("Произошла ошибка при удалении из избранного.", show_alert=True)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('show_photos_'))
async def show_all_photos(callback_query: types.CallbackQuery):
    ad_id = int(callback_query.data.split('_')[2])
    try:
        ad = await db.get_ad(ad_id)
    except Exception as e:
        logger.error(f"Ошибка при получении объявления {ad_id}: {e}")
        await callback_query.answer("Произошла ошибка при получении объявления.", show_alert=True)
        return

    if not ad:
        await callback_query.answer("Объявление не найдено.")
        return
    photos = ad['photos']  # Список file_id
    if photos:
        media_group = []
        for index, file_id in enumerate(photos):
            if index == 0:
                media_group.append(InputMediaPhoto(media=file_id, caption=f"Все фото объявления: {ad['title']}"))
            else:
                media_group.append(InputMediaPhoto(media=file_id))
        try:
            await bot.send_media_group(chat_id=callback_query.from_user.id, media=media_group)
            await callback_query.answer()
        except Exception as e:
            logger.error(f"Ошибка при отправке медиа группы: {e}")
            await callback_query.answer("Произошла ошибка при отправке фотографий.", show_alert=True)
    else:
        await callback_query.answer("Нет дополнительных фотографий.")

# Handlers for subscriptions
async def manage_subscriptions(message: types.Message):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("Создать подписку", "Мои подписки", "Отмена")
    await message.answer("Выберите действие:", reply_markup=keyboard)

@dp.message_handler(lambda message: message.text == "Создать подписку")
async def create_subscription_start(message: types.Message):
    await message.answer("Введите модель автомобиля или '-' для пропуска:")
    await SubscriptionStates.model.set()

@dp.message_handler(state=SubscriptionStates.model)
async def subscription_model(message: types.Message, state: FSMContext):
    if message.text.lower() == 'отмена':
        await cancel_handler(message, state)
        return
    model = message.text.strip()
    await state.update_data(model=None if model == '-' else model)
    await message.answer("Введите минимальную цену или отправьте 0 для пропуска:")
    await SubscriptionStates.price_min.set()

@dp.message_handler(state=SubscriptionStates.price_min)
async def subscription_price_min(message: types.Message, state: FSMContext):
    if message.text.lower() == 'отмена':
        await cancel_handler(message, state)
        return
    try:
        price_min = int(message.text)
        await state.update_data(price_min=price_min if price_min > 0 else None)
        await message.answer("Введите максимальную цену или отправьте 0 для пропуска:")
        await SubscriptionStates.price_max.set()
    except ValueError:
        await message.answer("Пожалуйста, введите число.")

@dp.message_handler(state=SubscriptionStates.price_max)
async def subscription_price_max(message: types.Message, state: FSMContext):
    if message.text.lower() == 'отмена':
        await cancel_handler(message, state)
        return
    try:
        price_max = int(message.text)
        await state.update_data(price_max=price_max if price_max > 0 else None)
        await message.answer("Введите минимальный год выпуска или отправьте 0 для пропуска:")
        await SubscriptionStates.year_min.set()
    except ValueError:
        await message.answer("Пожалуйста, введите число.")

@dp.message_handler(state=SubscriptionStates.year_min)
async def subscription_year_min(message: types.Message, state: FSMContext):
    if message.text.lower() == 'отмена':
        await cancel_handler(message, state)
        return
    try:
        year_min = int(message.text)
        await state.update_data(year_min=year_min if year_min > 0 else None)
        await message.answer("Введите максимальный год выпуска или отправьте 0 для пропуска:")
        await SubscriptionStates.year_max.set()
    except ValueError:
        await message.answer("Пожалуйста, введите число.")

@dp.message_handler(state=SubscriptionStates.year_max)
async def subscription_year_max(message: types.Message, state: FSMContext):
    if message.text.lower() == 'отмена':
        await cancel_handler(message, state)
        return
    try:
        year_max = int(message.text)
        data = await state.get_data()
        year_min = data.get('year_min')
        if year_max != 0 and year_min != 0 and year_max < year_min:
            await message.answer("Максимальный год не может быть меньше минимального. Попробуйте снова.")
            return
        await state.update_data(year_max=year_max if year_max > 0 else None)
        # Сохраняем подписку
        data = await state.get_data()
        await db.add_subscription(
            user_id=message.from_user.id,
            model=data.get('model'),
            price_min=data.get('price_min'),
            price_max=data.get('price_max'),
            year_min=data.get('year_min'),
            year_max=data.get('year_max')
        )
        await message.answer("Подписка создана.", reply_markup=main_menu_keyboard())
        await state.finish()
    except ValueError:
        await message.answer("Пожалуйста, введите число.")
        return

@dp.message_handler(lambda message: message.text == "Мои подписки")
async def my_subscriptions(message: types.Message):
    try:
        subscriptions = await db.get_subscriptions(message.from_user.id)
    except Exception as e:
        logger.error(f"Ошибка при получении подписок пользователя {message.from_user.id}: {e}")
        await message.answer("Произошла ошибка при получении подписок. Пожалуйста, попробуйте позже.")
        return
    if not subscriptions:
        await message.answer("У вас нет активных подписок.")
        return
    for sub in subscriptions:
        rowid = sub['rowid']
        model, price_min, price_max, year_min, year_max = sub['model'], sub['price_min'], sub['price_max'], sub['year_min'], sub['year_max']
        text = f"Модель: {model or 'Любая'}\nЦена: от {price_min if price_min else 0} до {price_max if price_max else '∞'}\nГод: от {year_min if year_min else 0} до {year_max if year_max else '∞'}"
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("Удалить", callback_data=f"del_sub_{rowid}"))
        await message.answer(text, reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('del_sub_'))
async def delete_subscription(callback_query: types.CallbackQuery):
    rowid = int(callback_query.data.split('_')[2])
    try:
        await db.delete_subscription(rowid)
        await callback_query.answer("Подписка удалена.")
        await callback_query.message.delete()
    except Exception as e:
        logger.error(f"Ошибка при удалении подписки {rowid}: {e}")
        await callback_query.answer("Произошла ошибка при удалении подписки.", show_alert=True)

# Handlers for support
@dp.message_handler(lambda message: message.text == "Поддержка" or message.text == "/support")
async def start_support(message: types.Message):
    await message.answer("Вы можете задать свой вопрос, и менеджер скоро свяжется с вами. Напишите ваше сообщение или 'Отмена' для отмены:")
    await SupportState.waiting_for_message.set()

@dp.message_handler(state=SupportState.waiting_for_message)
async def forward_to_manager(message: types.Message, state: FSMContext):
    if message.text.lower() == 'отмена':
        await cancel_handler(message, state)
        return
    await state.update_data(user_id=message.from_user.id)
    # Пересылаем сообщение менеджерам
    for manager_id in MANAGER_IDS:
        try:
            await bot.send_message(manager_id, f"Сообщение от пользователя @{message.from_user.username}:")
            # Пересылаем сообщение
            if message.photo:
                await bot.send_photo(manager_id, message.photo[-1].file_id)
            elif message.document:
                await bot.send_document(manager_id, message.document.file_id)
            else:
                await bot.send_message(manager_id, message.text)
            # Добавляем кнопку для ответа
            await bot.send_message(manager_id, f"Ответьте, используя кнопку ниже.", reply_markup=manager_reply_keyboard(message.from_user.id))
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения менеджеру {manager_id}: {e}")
    await message.answer("Ваше сообщение отправлено менеджеру. Ожидайте ответа.")
    await state.finish()

# Manager reply keyboard
def manager_reply_keyboard(user_id):
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("Ответить", callback_data=f"reply_{user_id}"))
    return keyboard

# Handler for managers to reply
@dp.callback_query_handler(lambda c: c.data and c.data.startswith('reply_'))
async def start_reply(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = int(callback_query.data.split('_')[1])
    await state.update_data(reply_to=user_id)
    await bot.send_message(callback_query.from_user.id, "Введите сообщение для пользователя:")
    await SupportState.chatting.set()
    await callback_query.answer()

@dp.message_handler(state=SupportState.chatting)
async def send_reply_to_user(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get('reply_to')
    try:
        await bot.send_message(user_id, f"Ответ от менеджера:\n{message.text}")
        await message.answer("Сообщение отправлено пользователю.")
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
        await message.answer(f"Не удалось отправить сообщение пользователю: {e}")
    await state.finish()

# Handlers for administrator panel
@dp.message_handler(commands=['admin'])
async def admin_panel_command(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("У вас нет доступа.")
        return
    await admin_panel(message)

@dp.message_handler(lambda message: message.text == "Админ Панель")
async def admin_panel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("У вас нет доступа.")
        return
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("Добавить объявление", "Управление объявлениями")
    keyboard.add("Статистика", "Рассылка")
    keyboard.add("Экспорт контактов", "Открыть/Закрыть Бот")
    await message.answer("Панель администратора", reply_markup=keyboard)

@dp.message_handler(lambda message: message.text == "Добавить объявление")
async def add_ad_start(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("У вас нет доступа.")
        return
    await message.answer("Введите название автомобиля:")
    await AdStates.title.set()

@dp.message_handler(state=AdStates.title)
async def ad_title(message: types.Message, state: FSMContext):
    if not message.text.strip():
        await message.answer("Название автомобиля не может быть пустым. Пожалуйста, введите название:")
        return
    await state.update_data(title=message.text.strip())
    await message.answer("Введите модель автомобиля:")
    await AdStates.model.set()

@dp.message_handler(state=AdStates.model)
async def ad_model(message: types.Message, state: FSMContext):
    if not message.text.strip():
        await message.answer("Модель автомобиля не может быть пустой. Пожалуйста, введите модель:")
        return
    await state.update_data(model=message.text.strip())
    await message.answer("Введите год выпуска:")
    await AdStates.year.set()

@dp.message_handler(state=AdStates.year)
async def ad_year(message: types.Message, state: FSMContext):
    try:
        year = int(message.text)
        current_year = datetime.utcnow().year
        if year < 1900 or year > current_year + 1:
            await message.answer(f"Пожалуйста, введите год выпуска между 1900 и {current_year + 1}:")
            return
        await state.update_data(year=year)
        await message.answer("Введите цену:")
        await AdStates.price.set()
    except ValueError:
        await message.answer("Пожалуйста, введите числовое значение для года.")

@dp.message_handler(state=AdStates.price)
async def ad_price(message: types.Message, state: FSMContext):
    try:
        price = int(message.text)
        if price <= 0:
            await message.answer("Цена должна быть положительным числом. Пожалуйста, введите цену:")
            return
        await state.update_data(price=price)
        await message.answer("Введите полное описание:")
        await AdStates.description.set()
    except ValueError:
        await message.answer("Пожалуйста, введите числовое значение для цены.")

@dp.message_handler(state=AdStates.description)
async def ad_description(message: types.Message, state: FSMContext):
    if not message.text.strip():
        await message.answer("Описание не может быть пустым. Пожалуйста, введите описание:")
        return
    await state.update_data(description=message.text.strip())
    await message.answer("Загрузите фото автомобиля (по одному). Когда закончите, отправьте команду /done")
    await AdStates.photos.set()
    # Инициализируем список для хранения фото
    await state.update_data(photos=[])

@dp.message_handler(state=AdStates.photos, content_types=['photo'])
async def ad_photos(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get('photos', [])

    # Сохраняем file_id
    file_id = message.photo[-1].file_id
    photos.append(file_id)
    await state.update_data(photos=photos)
    await message.answer("Фото добавлено. Загрузите следующее или отправьте /done, если закончите.")

@dp.message_handler(lambda message: message.text == '/done', state=AdStates.photos)
async def ad_photos_done(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get('photos', [])
    if not photos:
        await message.answer("Пожалуйста, загрузите хотя бы одно фото автомобиля или отмените действие командой 'Отмена'.")
        return
    await message.answer("Загрузите акт осмотра (фото). Когда закончите, отправьте команду /done")
    await AdStates.inspection_photos.set()
    # Инициализируем список для фото акта осмотра
    await state.update_data(inspection_photos=[])

@dp.message_handler(state=AdStates.inspection_photos, content_types=['photo'])
async def ad_inspection_photos(message: types.Message, state: FSMContext):
    data = await state.get_data()
    inspection_photos = data.get('inspection_photos', [])

    file_id = message.photo[-1].file_id
    inspection_photos.append(file_id)
    await state.update_data(inspection_photos=inspection_photos)
    await message.answer("Фото добавлено. Загрузите следующее или отправьте /done, если закончите.")

@dp.message_handler(lambda message: message.text == '/done', state=AdStates.inspection_photos)
async def ad_inspection_photos_done(message: types.Message, state: FSMContext):
    data = await state.get_data()
    inspection_photos = data.get('inspection_photos', [])
    if not inspection_photos:
        await message.answer("Пожалуйста, загрузите хотя бы одно фото акта осмотра или отмените действие командой 'Отмена'.")
        return
    await message.answer("Загрузите фото толщиномера (по одному). Когда закончите, отправьте команду /done")
    await AdStates.thickness_photos.set()
    # Инициализируем список для фото толщиномера
    await state.update_data(thickness_photos=[])

@dp.message_handler(state=AdStates.thickness_photos, content_types=['photo'])
async def ad_thickness_photos(message: types.Message, state: FSMContext):
    data = await state.get_data()
    thickness_photos = data.get('thickness_photos', [])

    file_id = message.photo[-1].file_id
    thickness_photos.append(file_id)
    await state.update_data(thickness_photos=thickness_photos)
    await message.answer("Фото добавлено. Загрузите следующее или отправьте /done, если закончите.")

@dp.message_handler(lambda message: message.text == '/done', state=AdStates.thickness_photos)
async def ad_thickness_photos_done(message: types.Message, state: FSMContext):
    data = await state.get_data()
    thickness_photos = data.get('thickness_photos', [])
    if not thickness_photos:
        await message.answer("Пожалуйста, загрузите хотя бы одно фото толщиномера или отмените действие командой 'Отмена'.")
        return
    try:
        ad_id = await db.add_ad(
            title=data['title'],
            price=data['price'],
            description=data['description'],
            photos=data['photos'],
            inspection_photos=data['inspection_photos'],
            thickness_photos=data['thickness_photos'],
            model=data['model'],
            year=data['year']
        )
    except Exception as e:
        logger.error(f"Ошибка при добавлении объявления: {e}")
        await message.answer("Произошла ошибка при сохранении объявления. Пожалуйста, попробуйте позже.")
        await state.finish()
        return

    await message.answer("Объявление сохранено.")
    # Уведомляем подписчиков
    try:
        ad = await db.get_ad(ad_id)
        await notify_subscribers(ad)
    except Exception as e:
        logger.error(f"Ошибка при уведомлении подписчиков: {e}")
    await state.finish()

# Function to notify subscribers when a new ad is added
async def notify_subscribers(ad):
    try:
        subscriptions = await db.get_all_subscriptions()
    except Exception as e:
        logger.error(f"Ошибка при получении подписок: {e}")
        return

    for sub in subscriptions:
        user_id = sub['user_id']
        model = sub['model']
        price_min = sub['price_min']
        price_max = sub['price_max']
        year_min = sub['year_min']
        year_max = sub['year_max']
        # Проверяем соответствие объявления подписке
        if model and model.lower() not in ad['model'].lower():
            continue
        if price_min and ad['price'] < price_min:
            continue
        if price_max and ad['price'] > price_max:
            continue
        if year_min and ad['year'] < year_min:
            continue
        if year_max and ad['year'] > year_max:
            continue
        # Отправляем уведомление
        try:
            await bot.send_message(user_id, f"Появилось новое объявление, соответствующее вашей подписке: {ad['title']}")
            logger.info(f"Уведомление отправлено пользователю {user_id} о новом объявлении.")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление пользователю {user_id}: {e}")

@dp.message_handler(lambda message: message.text == "Управление объявлениями")
async def manage_ads(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("У вас нет доступа.")
        return
    try:
        ads = await db.get_ads()
    except Exception as e:
        logger.error(f"Ошибка при получении объявлений для управления: {e}")
        await message.answer("Произошла ошибка при получении объявлений. Пожалуйста, попробуйте позже.")
        return
    if not ads:
        await message.answer("Нет доступных объявлений.")
        return
    for ad in ads:
        ad_id = ad['ad_id']
        title = ad['title']
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton("Редактировать", callback_data=f"edit_{ad_id}"),
            types.InlineKeyboardButton("Удалить", callback_data=f"delete_{ad_id}")
        )
        await message.answer(f"{ad_id}. {title}", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith(('edit_', 'delete_')))
async def process_ad_management(callback_query: types.CallbackQuery, state: FSMContext):
    ad_id = int(callback_query.data.split('_')[1])
    action = callback_query.data.split('_')[0]

    if callback_query.from_user.id not in ADMIN_IDS:
        await callback_query.answer("У вас нет прав.", show_alert=True)
        return

    if action == 'delete':
        try:
            ad = await db.get_ad(ad_id)
            if ad:
                await db.delete_ad(ad_id)
                await callback_query.answer("Объявление удалено.", show_alert=True)
                await callback_query.message.delete()
                logger.info(f"Объявление {ad_id} удалено администратором {callback_query.from_user.id}.")
            else:
                await callback_query.answer("Объявление не найдено.", show_alert=True)
        except Exception as e:
            logger.error(f"Ошибка при удалении объявления {ad_id}: {e}")
            await callback_query.answer("Произошла ошибка при удалении объявления.", show_alert=True)
    elif action == 'edit':
        await callback_query.answer("Редактирование пока не реализовано.", show_alert=True)

# Handlers for admin commands
@dp.message_handler(lambda message: message.text in ["Статистика", "Рассылка", "Экспорт контактов", "Открыть/Закрыть Бот"])
async def process_admin_commands(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("У вас нет доступа.")
        return

    if message.text == "Статистика":
        try:
            users_count, ads_count = await db.get_statistics()
            active_users = await db.get_active_users_count()
            await message.answer(f"Всего пользователей: {users_count}\nАктивных пользователей: {active_users}\nКоличество объявлений: {ads_count}")
        except Exception as e:
            logger.error(f"Ошибка при получении статистики: {e}")
            await message.answer("Произошла ошибка при получении статистики. Пожалуйста, попробуйте позже.")
    elif message.text == "Рассылка":
        await message.answer("Пожалуйста, введите сообщение для рассылки:")
        await MailingStates.message.set()
    elif message.text == "Экспорт контактов":
        await export_contacts(message)
    elif message.text == "Открыть/Закрыть Бот":
        await toggle_bot_state(message)

# Handler for sending mailing
@dp.message_handler(state=MailingStates.message)
async def process_mailing(message: types.Message, state: FSMContext):
    mailing_message = message.text
    await state.finish()

    try:
        users = await db.get_approved_users()
    except Exception as e:
        logger.error(f"Ошибка при получении списка одобренных пользователей для рассылки: {e}")
        await message.answer("Произошла ошибка при получении списка пользователей. Пожалуйста, попробуйте позже.")
        return

    if not users:
        await message.answer("Нет одобренных пользователей для рассылки.")
        return

    total_users = len(users)
    success_count = 0

    for user_id in users:
        try:
            await bot.send_message(user_id, mailing_message)
            success_count += 1
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения пользователю {user_id}: {e}")

    await message.answer(f"Рассылка завершена.\nУспешно отправлено: {success_count}/{total_users}")

# Function to export contacts to Excel
async def export_contacts(message: types.Message):
    try:
        users = await db.get_user_contacts()
    except Exception as e:
        logger.error(f"Ошибка при получении контактов пользователей: {e}")
        await message.answer("Произошла ошибка при получении контактов. Пожалуйста, попробуйте позже.")
        return
    if not users:
        await message.answer("Нет зарегистрированных пользователей.")
        return
    # Создаем DataFrame
    df = pd.DataFrame(users, columns=['Имя', 'Город', 'Телефон'])
    # Сохраняем в Excel
    file_path = 'contacts.xlsx'
    try:
        df.to_excel(file_path, index=False)
    except Exception as e:
        logger.error(f"Ошибка при сохранении контактов в Excel: {e}")
        await message.answer("Произошла ошибка при сохранении контактов. Пожалуйста, попробуйте позже.")
        return
    # Отправляем файл администратору
    try:
        await bot.send_document(message.chat.id, InputFile(file_path))
        logger.info(f"Файл контактов отправлен администратору {message.from_user.id}.")
    except Exception as e:
        logger.error(f"Ошибка при отправке файла контактов: {e}")
        await message.answer("Произошла ошибка при отправке файла. Пожалуйста, попробуйте позже.")
    finally:
        # Удаляем файл после отправки
        if os.path.exists(file_path):
            os.remove(file_path)

# Function to toggle bot state (open/close)
async def toggle_bot_state(message: types.Message):
    try:
        current_state = await db.is_bot_open()
        new_state = not current_state
        await db.set_bot_state(new_state)
        state_text = "открыт" if new_state else "закрыт"
        await message.answer(f"Бот теперь {state_text} для новых пользователей.")
        logger.info(f"Состояние бота изменено на {state_text}.")
    except Exception as e:
        logger.error(f"Ошибка при переключении состояния бота: {e}")
        await message.answer("Произошла ошибка при изменении состояния бота. Пожалуйста, попробуйте позже.")

# Handlers for buying and discount requests
@dp.callback_query_handler(lambda c: c.data and c.data.startswith(('description_', 'inspection_', 'thickness_', 'buy_', 'discount_')))
async def process_callback_ad(callback_query: types.CallbackQuery, state: FSMContext):
    ad_id = int(callback_query.data.split('_')[1])
    action = callback_query.data.split('_')[0]
    try:
        ad = await db.get_ad(ad_id)
    except Exception as e:
        logger.error(f"Ошибка при получении объявления {ad_id}: {e}")
        await callback_query.answer("Произошла ошибка при получении объявления.", show_alert=True)
        return

    if not ad:
        await callback_query.answer("Объявление не найдено.")
        return
    title = ad['title']
    price = ad['price']
    description = ad['description']
    photos = ad['photos']
    inspection_photos = ad['inspection_photos']
    thickness_photos = ad['thickness_photos']
    model = ad['model']
    year = ad['year']
    added_date = ad['added_date']
    caption = f"{title}\nМодель: {model}\nГод выпуска: {year}\nЦена: {price} KZT"

    if action == 'description':
        if description:
            try:
                await bot.send_message(callback_query.from_user.id, description)
            except Exception as e:
                logger.error(f"Ошибка при отправке описания объявления {ad_id} пользователю {callback_query.from_user.id}: {e}")
                await callback_query.answer("Произошла ошибка при отправке описания.", show_alert=True)
        else:
            await bot.send_message(callback_query.from_user.id, "Описание отсутствует.")
        await callback_query.answer()
    elif action == 'inspection':
        if inspection_photos:
            media = MediaGroup()
            for index, file_id in enumerate(inspection_photos):
                if index == 0:
                    media.attach(InputMediaPhoto(media=file_id, caption=f"Акт осмотра: {title}"))
                else:
                    media.attach(InputMediaPhoto(media=file_id))
            try:
                await bot.send_media_group(chat_id=callback_query.from_user.id, media=media)
            except Exception as e:
                logger.error(f"Ошибка при отправке акта осмотра объявления {ad_id} пользователю {callback_query.from_user.id}: {e}")
                await callback_query.answer("Произошла ошибка при отправке акта осмотра.", show_alert=True)
        else:
            await bot.send_message(callback_query.from_user.id, "Нет фотографий акта осмотра.")
        await callback_query.answer()
    elif action == 'thickness':
        if thickness_photos:
            media = MediaGroup()
            for index, file_id in enumerate(thickness_photos):
                if index == 0:
                    media.attach(InputMediaPhoto(media=file_id, caption=f"Фото толщиномера: {title}"))
                else:
                    media.attach(InputMediaPhoto(media=file_id))
            try:
                await bot.send_media_group(chat_id=callback_query.from_user.id, media=media)
            except Exception as e:
                logger.error(f"Ошибка при отправке фото толщиномера объявления {ad_id} пользователю {callback_query.from_user.id}: {e}")
                await callback_query.answer("Произошла ошибка при отправке фото толщиномера.", show_alert=True)
        else:
            await bot.send_message(callback_query.from_user.id, "Нет фотографий толщиномера.")
        await callback_query.answer()
    elif action == 'buy':
        await callback_query.answer()
        data = {'ad_id': ad_id}
        await notify_manager_with_contact(callback_query.from_user.id, data)
        await bot.send_message(callback_query.from_user.id, "Ваш запрос отправлен менеджеру. Ожидайте обратной связи.")
    elif action == 'discount':
        await callback_query.answer()
        ad_id = ad_id
        await state.update_data(ad_id=ad_id)
        min_price = int(ad['price'] * 0.8)
        await DiscountState.desired_price.set()
        await bot.send_message(callback_query.from_user.id, f"Введите желаемую цену или 'Отмена' для отмены:")
    else:
        await callback_query.answer()

# Function to notify managers about buying requests
async def notify_manager_with_contact(user_id, data):
    try:
        ad = await db.get_ad(data['ad_id'])
        user_contact = await db.get_user(user_id)
        if user_contact:
            name = user_contact['name'] or "Не указано"
            phone = user_contact['phone'] or "Не указано"
            city = user_contact['city'] or "Не указано"
        else:
            name = phone = city = "Не указано"

        message_text = f"Новый запрос от пользователя:\n\nИмя: {name}\nТелефон: {phone}\nГород: {city}\nАвтомобиль: {ad['title']}\nЗапрос: Купить"
        for manager_id in MANAGER_IDS:
            try:
                await bot.send_message(manager_id, message_text)
                logger.info(f"Запрос на покупку объявления {ad['ad_id']} отправлен менеджеру {manager_id}.")
            except Exception as e:
                logger.error(f"Не удалось отправить запрос на покупку менеджеру {manager_id}: {e}")
    except Exception as e:
        logger.error(f"Ошибка при уведомлении менеджеров о запросе на покупку: {e}")

# Handlers for discount requests
@dp.message_handler(state=DiscountState.desired_price)
async def process_desired_price(message: types.Message, state: FSMContext):
    if message.text.lower() == 'отмена':
        await cancel_handler(message, state)
        return
    try:
        desired_price = int(message.text)
        data = await state.get_data()
        ad_id = data['ad_id']
        ad = await db.get_ad(ad_id)
        min_price = int(ad['price'] * 0.8)
        if desired_price < min_price:
            await message.answer(f"Цена не может быть ниже {min_price} KZT. Пожалуйста, введите корректную цену:")
            return
        await state.update_data(desired_price=desired_price)
        # Уведомляем менеджеров
        data = await state.get_data()
        await notify_manager_with_contact_discount(message.from_user.id, data)
        await message.answer("Ваш запрос на скидку отправлен менеджеру. Ожидайте обратной связи.")
        await state.finish()
    except ValueError:
        await message.answer("Пожалуйста, введите числовое значение для цены.")
        return

# Function to notify managers about discount requests
async def notify_manager_with_contact_discount(user_id, data):
    try:
        ad = await db.get_ad(data['ad_id'])
        user_contact = await db.get_user(user_id)
        if user_contact:
            name = user_contact['name'] or "Не указано"
            phone = user_contact['phone'] or "Не указано"
            city = user_contact['city'] or "Не указано"
        else:
            name = phone = city = "Не указано"

        desired_price = data.get('desired_price')
        message_text = f"Новый запрос на скидку от пользователя:\n\nИмя: {name}\nТелефон: {phone}\nГород: {city}\nАвтомобиль: {ad['title']}\nЖелаемая цена: {desired_price} KZT"
        for manager_id in MANAGER_IDS:
            try:
                await bot.send_message(manager_id, message_text)
                logger.info(f"Запрос на скидку объявления {ad['ad_id']} отправлен менеджеру {manager_id}.")
            except Exception as e:
                logger.error(f"Не удалось отправить запрос на скидку менеджеру {manager_id}: {e}")
    except Exception as e:
        logger.error(f"Ошибка при уведомлении менеджеров о запросе на скидку: {e}")

# Function to notify managers about new ad notifications (optional)
async def notify_managers_new_ad(ad):
    # Дополнительная функция, если требуется
    pass

# Run the bot
if __name__ == '__main__':
    try:
        executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
    except Exception as e:
        logger.critical(f"Бот завершился с ошибкой: {e}")