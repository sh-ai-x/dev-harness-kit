"""Sample real-bug: SQL injection. /dev-kit:review MUST flag this as security/major+."""
def find_user(name):
    query = "SELECT * FROM users WHERE name = '" + name + "'"
    cursor.execute(query)
    return cursor.fetchone()
