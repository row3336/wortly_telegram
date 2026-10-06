"""Service functions of the bot, one module each (design T-D §7.1, §7.3; ВП-21).

Every module here declares ``SERVICE_FUNCTIONS``, a tuple of router.ServiceFunction;
the router finds the modules itself, so a package adds a file and edits no shared list.
Which functions exist waits for the owner (Q1, Q12; T3-SVC). The package is empty now.
"""
