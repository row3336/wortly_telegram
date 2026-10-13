"""Service functions of the bot, one module each (design T-D §7.1, §7.3; ВП-21).

Every module here declares ``SERVICE_FUNCTIONS``, a tuple of router.ServiceFunction;
the router finds the modules itself, so a package adds a file and edits no shared list.
The owner chose the composition (Q07, Q08, Q11, R2-02, R2-12, R4-01; DEV-08 manifest §3
row 7). Only access.py exists now; each other function waits for its core route (DEV-15)
and gets its own module here (DEV-17). Excluded functions (Q07) get none; a function the
owner brings back later, such as support with stars, is a new module found the same way.
"""
