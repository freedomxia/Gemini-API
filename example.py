import asyncio
from gemini_webapi import GeminiClient

# 替换为你的 Cookie 值
Secure_1PSID = "你的__Secure-1PSID值"
Secure_1PSIDTS = "你的__Secure-1PSIDTS值"

async def main():
    client = GeminiClient(Secure_1PSID, Secure_1PSIDTS, proxy=None)
    await client.init(timeout=30, auto_close=False, auto_refresh=True)

    response = await client.generate_content("你好，请用中文介绍一下你自己")
    print(response.text)

asyncio.run(main())
