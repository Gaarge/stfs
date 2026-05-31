import asyncio
import random
from getpass import getpass

from telethon import TelegramClient, functions, types
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneNumberInvalidError,
    FloodWaitError,
)


SESSION_NAME = "my_telegram_session"


async def main():
    print("Создайте api_id и api_hash на https://my.telegram.org")
    api_id = int(35318463)
    api_hash = str("9804068f2ef198ec838edc2bf81e3819")

    my_phone = str("+79162040241")

    client = TelegramClient(SESSION_NAME, api_id, api_hash)

    await client.connect()

    try:
        if not await client.is_user_authorized():
            await client.send_code_request(my_phone)
            code = input("Введите код из Telegram/SMS: ").strip()

            try:
                await client.sign_in(my_phone, code)
            except SessionPasswordNeededError:
                password = getpass("Включена 2FA. Введите пароль Telegram: ")
                await client.sign_in(password=password)

        target_phone = input(
            "Введите номер получателя в формате +79990000000 "
            "(только ваш номер или номер с согласием): "
        ).strip()

        if not target_phone.startswith("+"):
            print("Номер должен быть в международном формате, например +79990000000")
            return

        # Импортируем ОДИН контакт, чтобы Telegram мог сопоставить номер с аккаунтом.
        # Это не гарантирует успех: пользователь может скрывать номер или не быть зарегистрирован.
        contact = types.InputPhoneContact(
            client_id=random.randrange(1, 10_000_000),
            phone=target_phone,
            first_name="Temporary",
            last_name="Contact",
        )

        result = await client(
            functions.contacts.ImportContactsRequest([contact])
        )

        if not result.users:
            print("Пользователь с таким номером не найден или недоступен по настройкам приватности.")
            return

        user = result.users[0]

        await client.send_message(user, "привет")
        print("Сообщение отправлено.")

        # Необязательно: удаляем временный контакт после отправки.
        await client(functions.contacts.DeleteContactsRequest(id=[user]))
        print("Временный контакт удалён из контактов Telegram.")

    except PhoneNumberInvalidError:
        print("Неверный формат номера телефона.")
    except FloodWaitError as e:
        print(f"Telegram временно ограничил запросы. Подождите {e.seconds} секунд.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
