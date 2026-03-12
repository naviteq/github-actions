"""
This file intentionally contains insecure patterns to test CodeQL scanning.
DO NOT use in production.
"""

import sys
import os
import subprocess
import pickle
import sqlite3


# ------------------------------------------------------------
# 1. Command Injection
# ------------------------------------------------------------
def command_injection():
    user_input = sys.argv[1] if len(sys.argv) > 1 else "ls"
    subprocess.run(user_input, shell=True)


# ------------------------------------------------------------
# 2. Unsafe eval
# ------------------------------------------------------------
def unsafe_eval():
    user_input = sys.argv[1] if len(sys.argv) > 1 else "1+1"
    result = eval(user_input)
    print(result)


# ------------------------------------------------------------
# 3. Unsafe pickle deserialization
# ------------------------------------------------------------
def unsafe_pickle():
    data = sys.argv[1].encode() if len(sys.argv) > 1 else b"cos\nsystem\n(S'ls'\ntR."
    pickle.loads(data)


# ------------------------------------------------------------
# 4. SQL Injection
# ------------------------------------------------------------
def sql_injection():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    username = sys.argv[1] if len(sys.argv) > 1 else "admin"

    query = f"SELECT * FROM users WHERE name = '{username}'"
    cursor.execute(query)

    conn.close()


# ------------------------------------------------------------
# 5. Path traversal
# ------------------------------------------------------------
def path_traversal():
    filename = sys.argv[1] if len(sys.argv) > 1 else "../../etc/passwd"
    with open(filename, "r") as f:
        print(f.read())


# ------------------------------------------------------------
# 6. OS command via os.system
# ------------------------------------------------------------
def os_command():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ls"
    os.system(cmd)


if __name__ == "__main__":
    command_injection()
    unsafe_eval()
    unsafe_pickle()
    sql_injection()
    path_traversal()
    os_command()
