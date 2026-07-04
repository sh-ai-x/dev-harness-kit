"""Sample trap: parameterized query. /review-code MUST NOT flag (safe)."""
def find_user(name):
    cursor.execute("SELECT * FROM users WHERE name = %s", (name,))
    return cursor.fetchone()
