from telethon import TelegramClient

API_ID = 32750992
API_HASH = "9077fa6af8bd758c125421fe7e0f0c7f"

client = TelegramClient("test_session", API_ID, API_HASH)

async def main():
    await client.connect()
    print("Connected:", client.is_connected())

    if not await client.is_user_authorized():
        print("Login required - session not authorized")
    else:
        me = await client.get_me()
        print("Telegram API working:", me.username)

    await client.disconnect()

with client:
    client.loop.run_until_complete(main())