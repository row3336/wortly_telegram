"""The delivery role: the Telegram channel over the core's delivery loop (design T-D §3).

The channel, the classifier and the capacity gate import no database driver; the
process entry (delivery.main) imports the core's loop lazily.
"""
