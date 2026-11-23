#!/bin/sh
# Run the server as the owner of /data, so a NAS shared folder mounted there
# works without matching UIDs by hand. PUID/PGID override the detected owner.
# A fresh named volume inherits the image's /data owner (568:568, the common
# NAS "apps" account). When started with `user:`, nothing here applies.
set -e
if [ "$(id -u)" = "0" ]; then
  uid="${PUID:-$(stat -c %u /data)}"
  gid="${PGID:-$(stat -c %g /data)}"
  if [ "$uid" = "0" ]; then
    # Docker created a missing bind-mount folder as root: hand it to the default account.
    uid=568
    [ "$gid" = "0" ] && gid=568
  fi
  if [ "$(stat -c %u:%g /data)" != "$uid:$gid" ]; then
    chown "$uid:$gid" /data || echo "warning: could not chown /data to $uid:$gid" >&2
  fi
  exec setpriv --reuid="$uid" --regid="$gid" --clear-groups -- "$@"
fi
exec "$@"
