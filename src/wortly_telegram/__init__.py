"""Wortly Telegram adapter: the bot role (polling, /start, owner actions) and the delivery role.

The learning core never imports this package (TC:30). Modules that a unit test or the
bot's handlers import load no database driver; only the process entries
(bot_main, delivery.main) need psycopg, and they import the core lazily.
"""
