"""
Change a server's CURRENT status in the local sample database, to demo / test the critical-server e-mail.

    python set_server_status.py IFSAPP02 CRITICAL     -> app e-mails you within ~30 seconds (app must be running)
    python set_server_status.py IFSAPP02 HEALTHY      -> back to normal (a later CRITICAL will e-mail again)
    python set_server_status.py                        -> just list the servers
"""
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent / "monitoring.db"
con = sqlite3.connect(DB)
if len(sys.argv) == 3:
    name, status = sys.argv[1], sys.argv[2].upper()
    if con.execute("UPDATE server_health SET status = ? WHERE server_name = ?", (status, name)).rowcount == 0:
        sys.exit(f"No server named {name!r}")
    con.commit()
    print(f"{name} is now {status}")
for row in con.execute("SELECT server_name, status, cpu_percent, memory_percent, disk_percent FROM server_health ORDER BY server_name"):
    print("  %-10s %-9s CPU %5.1f%%  Memory %5.1f%%  Disk %5.1f%%" % row)
