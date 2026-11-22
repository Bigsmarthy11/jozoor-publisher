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

if [ "${2:-}" = "--from-clipboard" ]; then
  osascript -e 'display dialog "Save the Instagram token that is on your clipboard (copied from Meta → Generate token → Copy)?

It goes straight into encrypted storage. Nobody sees it, and the clipboard is cleared afterwards." with title "JOZOOR Publisher" buttons {"Cancel","Save token"} default button "Save token"' >/dev/null 2>&1 || { echo "cancelled"; exit 1; }
  TOKEN="$(pbpaste)"
else
  TOKEN="$(osascript -e 'text returned of (display dialog "Paste the Instagram access token for @jozoor.om (from Meta: Generate token → Copy)." default answer "" with hidden answer with title "JOZOOR Publisher" buttons {"Cancel","Save"} default button "Save")' 2>/dev/null)" || { echo "cancelled"; exit 1; }
fi
TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
[ -n "$TOKEN" ] || { echo "empty token"; exit 1; }

check() {  # $1 = API base; prints username or the API error message (never the token)
  curl -sG "$1" --data-urlencode fields=username --data-urlencode "access_token=$TOKEN" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print(d.get("username") or "ERR: "+d.get("error",{}).get("message","unknown"))'
}
USERNAME="$(check https://graph.facebook.com/v24.0/17841480898193113)"; HOST=facebook
if [ "${USERNAME#ERR}" != "$USERNAME" ]; then
  echo "graph.facebook.com: $USERNAME"
  USERNAME="$(check https://graph.instagram.com/v24.0/me)"; HOST=instagram
  [ "${USERNAME#ERR}" != "$USERNAME" ] && { echo "graph.instagram.com: $USERNAME"; exit 1; }
fi
echo "$HOST" > state/token_host
echo "token verified for @$USERNAME"
[ "$(printf '%s' "$USERNAME" | tr 'A-Z' 'a-z')" = "jozoor.om" ] || { echo "token is not for @jozoor.om; not saved"; exit 1; }

printf '%s' "$TOKEN" | TOKEN_KEY="$KEY" openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -a -A -salt -out state/token.enc -pass env:TOKEN_KEY
date -u +%FT%TZ > state/token_refreshed_at
printf '%s' "$KEY" | gh secret set TOKEN_KEY --repo "$REPO"
unset TOKEN KEY
[ "${2:-}" = "--from-clipboard" ] && printf "" | pbcopy
echo "saved: state/token.enc (encrypted) + TOKEN_KEY secret on $REPO"
