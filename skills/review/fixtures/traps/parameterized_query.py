"""Sample trap: parameterized query. /dev-kit:review MUST NOT flag (safe)."""
def find_user(name):
    cursor.execute("SELECT * FROM users WHERE name = %s", (name,))
    return cursor.fetchone()
