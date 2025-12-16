import sys
import aiogram
print(f"Python: {sys.version}")
print(f"Aiogram: {aiogram.__version__}")

try:
    from aiogram.client.default_bot_properties import DefaultBotProperties
    print("SUCCESS: Imported from aiogram.client.default_bot_properties")
except ImportError as e:
    print(f"FAIL: {e}")

try:
    from aiogram.client.bot import DefaultBotProperties
    print("SUCCESS: Imported from aiogram.client.bot")
except ImportError:
    pass

import aiogram.client
print("aiogram.client dir:", dir(aiogram.client))
