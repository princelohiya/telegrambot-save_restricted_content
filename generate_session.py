import asyncio
from pyrogram import Client

async def _generate():
    print("=== Pyrogram Session String Generator ===")
    
    # We ask for your credentials upfront
    api_id = int(input("API_ID: ").strip())
    api_hash = input("API_HASH: ").strip()

    # Initialize the client in memory so it doesn't create local files
    client = Client(
        "my_account",
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True
    )

    print("\nConnecting to Telegram... Please follow the prompts below:")
    
    # 'async with' automatically triggers Pyrogram's built-in login process
    async with client:
        # Once you successfully log in through the terminal, it exports the string
        session_string = await client.export_session_string()
        print("\n================ COPY BELOW ================")
        print(session_string)
        print("================= COPY ABOVE ===============\n")
        print("Paste it into your `.env` file as: SESSION_STRING=<the_string>")

if __name__ == "__main__":
    asyncio.run(_generate())