"""(Re)set PiKVM credentials.  Usage: python setup_auth.py <username> <password>"""
import sys
import auth

if len(sys.argv) != 3:
    print("usage: python setup_auth.py <username> <password>")
    sys.exit(1)

cfg = auth.make_config(sys.argv[1], sys.argv[2])
print("username:", cfg["username"])
print("api_key :", cfg["api_key"])
print("config  :", auth.CONFIG)
