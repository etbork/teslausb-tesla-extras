#!/bin/bash -eu

FILE_PATH="$1"

umask 077
echo "username=$shareuser" > "$FILE_PATH"
echo "password=$sharepassword" >> "$FILE_PATH"
chmod 600 "$FILE_PATH"
