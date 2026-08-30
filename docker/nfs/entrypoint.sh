#!/bin/sh
set -eu

clients="${MODELSHELF_NFS_CLIENTS:-10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fd00::/8}"

attempt=0
while [ ! -d /export/artifacts ]; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "Artifact directory /export/artifacts was not initialized by the server" >&2
    exit 70
  fi
  sleep 1
done

case "$clients" in
  *[!0-9a-fA-F:.,/]*|'')
    echo "MODELSHELF_NFS_CLIENTS must be a comma-separated CIDR list" >&2
    exit 64
    ;;
esac

old_ifs="$IFS"
IFS=,
for cidr in $clients; do
  case "$cidr" in
    10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*|fd*|fc*) ;;
    *)
      if [ "${MODELSHELF_NFS_ALLOW_PUBLIC:-false}" != "true" ]; then
        echo "Public CIDR $cidr requires MODELSHELF_NFS_ALLOW_PUBLIC=true" >&2
        exit 64
      fi
      ;;
  esac
done
IFS="$old_ifs"

# Ganesha 6.x parses an all-address CIDR as a path token rather than a client
# expression. Preserve ModelShelf's explicit CIDR interface and normalize only
# these two universal networks after the public-export opt-in check above.
ganesha_clients="$clients"
case ",$clients," in
  *,0.0.0.0/0,*|*,::/0,*) ganesha_clients="*" ;;
esac

mkdir -p /run/dbus /run/ganesha /var/lib/nfs/ganesha
dbus-daemon --system --fork --nopidfile
/usr/sbin/rpcbind -w
sed "s|__CLIENTS__|$ganesha_clients|g" /etc/ganesha/ganesha.conf.template > /run/ganesha.conf
exec /usr/bin/ganesha.nfsd -F -L STDERR -f /run/ganesha.conf
