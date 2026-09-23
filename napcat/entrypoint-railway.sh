#!/bin/bash
set -e

mkdir -p /app/napcat/config

# QQ session data is the Railway volume. Recreate network settings on each boot.
if [ -n "${NAPCAT_ONEBOT_TOKEN:-}" ]; then
    if [[ ! "$NAPCAT_ONEBOT_TOKEN" =~ ^[A-Za-z0-9_-]{32,}$ ]]; then
        echo "Invalid NAPCAT_ONEBOT_TOKEN" >&2
        exit 1
    fi
    cat > /app/napcat/config/onebot11.json <<EOF
{
  "enable": true,
  "network": {
    "httpServers": [],
    "httpClients": [],
    "websocketServers": [{
      "name": "railway-private-ws",
      "enable": true,
      "host": "0.0.0.0",
      "port": 3001,
      "messagePostFormat": "array",
      "reportSelfMessage": false,
      "token": "$NAPCAT_ONEBOT_TOKEN",
      "enableForcePushEvent": true,
      "debug": false,
      "heartInterval": 30000
    }],
    "websocketClients": []
  },
  "musicSignUrl": "",
  "enableLocalFile2Url": false,
  "parseMultMsg": false
}
EOF
fi

exec /bin/bash /app/entrypoint.sh
