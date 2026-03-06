import sqlite3

def get_user(username):
    conn = sqlite3.connect("users.db")

    # vulnerable SQL query
    query = "SELECT * FROM users WHERE name = '%s'" % username

    cursor = conn.cursor()
    cursor.execute(query)

    return cursor.fetchall()