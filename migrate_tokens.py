import asyncio
import database as db


async def main():
    await db.init_db()
    migrated = await db.migrate_client_bot_tokens()
    print(f"migrated_tokens={migrated}")


if __name__ == "__main__":
    asyncio.run(main())
