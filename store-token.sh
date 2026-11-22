#!/bin/bash
# Securely hand the Instagram token to the publisher.
# The token is typed or pasted by the owner into a hidden macOS dialog, checked
# against Instagram (only the username is printed), encrypted with a random key
# kept in the macOS Keychain, and written to state/token.enc. The key is sent to
# GitHub as the TOKEN_KEY secret. The token itself is never printed or logged.
# usage: ./store-token.sh <owner/repo>
set -euo pipefail
cd "$(dirname "$0")"
REPO="${1:?usage: store-token.sh owner/repo}"

KEY="$(security find-generic-password -a jozoor -s jozoor-token-key -w 2>/dev/null || true)"
if [ -z "$KEY" ]; then
  KEY="$(openssl rand -base64 48 | tr -d '\n')"
  security add-generic-password -a jozoor -s jozoor-token-key -w "$KEY" -U
fi

TOKEN="$(osascript -e 'text returned of (display dialog "Paste the Instagram access token for @jozoor.om (from Meta: Generate token → Copy).

It goes straight into encrypted storage and is not shown to anyone." default answer "" with hidden answer with title "JOZOOR Publisher" buttons {"Cancel","Save"} default button "Save")' 2>/dev/null)" || { echo "cancelled"; exit 1; }
TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
[ -n "$TOKEN" ] || { echo "empty token"; exit 1; }

USERNAME="$(curl -sfG https://graph.facebook.com/v24.0/17841480898193113 --data-urlencode fields=username \
            --data-urlencode "access_token=$TOKEN" | python3 -c 'import json,sys;print(json.load(sys.stdin)["username"])')" \
  || { echo "Instagram rejected the token (expired or wrong copy)"; exit 1; }
echo "token verified for @$USERNAME"
[ "$(printf '%s' "$USERNAME" | tr 'A-Z' 'a-z')" = "jozoor.om" ] || { echo "token is not for @jozoor.om; not saved"; exit 1; }

mkdir -p state
printf '%s' "$TOKEN" | TOKEN_KEY="$KEY" openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -a -A -salt -out state/token.enc -pass env:TOKEN_KEY
date -u +%FT%TZ > state/token_refreshed_at
printf '%s' "$KEY" | gh secret set TOKEN_KEY --repo "$REPO"
unset TOKEN KEY
echo "saved: state/token.enc (encrypted) + TOKEN_KEY secret on $REPO"
