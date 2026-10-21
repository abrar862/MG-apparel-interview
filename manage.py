import getpass, sys
from argon2 import PasswordHasher
from db import cur

if len(sys.argv) != 3 or sys.argv[1] != "set-password":
    sys.exit("usage: python manage.py set-password <portal_email>")
email = sys.argv[2].strip().lower()
c = cur()
rows = c.execute("SELECT customer_id FROM users WHERE lower(email)=?", [email]).fetchall()
if len(rows) != 1:
    sys.exit("no unique user with that email (run a sync first)")
pw = input("New password (min 10 chars, visible): ")
if len(pw) < 10:
    sys.exit("too short")
c.execute("UPDATE users SET password_hash=? WHERE customer_id=?", [PasswordHasher().hash(pw), rows[0][0]])
print("ok")