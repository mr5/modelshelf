#!/usr/bin/env bash
# Real TCP/NFS integration: central exporter -> peer publisher -> follower CLI.
set -Eeuo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${MODELSHELF_NFS_TEST_IMAGE:-modelshelf-nfs:client-distribution-test}"
peer_port="${MODELSHELF_TEST_PEER_PORT:-12049}"
prefix="modelshelf-peer-test-$RANDOM-$$"
network="$prefix-net"
volume="$prefix-data"
peer="$prefix-peer"
central="$prefix-central"
client="$prefix-client"
test_dir="$(mktemp -d)"
cleanup() {
  if [ "${MODELSHELF_KEEP_TEST:-0}" = 1 ]; then echo "Kept test containers: $client $peer $central; network: $network; volume: $volume; binaries: $test_dir"; return; fi
  docker rm -f "$client" "$peer" "$central" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  rm -rf "$test_dir"
}
trap cleanup EXIT
trap 'docker logs "$peer" >&2 || true; docker logs "$central" >&2 || true' ERR
arch="$(docker info --format '{{.Architecture}}')"
case "$arch" in aarch64|arm64) arch=arm64;; x86_64|amd64) arch=amd64;; *) exit 1;; esac
(cd "$repo_root/packages/client" && CGO_ENABLED=0 GOOS=linux GOARCH="$arch" go test -c -o "$test_dir/syncer.test" ./internal/syncer && CGO_ENABLED=0 GOOS=linux GOARCH="$arch" go build -o "$test_dir/modelshelf" ./cmd/modelshelf)
docker build -t "$image" "$repo_root/docker/nfs"
docker network create "$network" >/dev/null
docker volume create "$volume" >/dev/null
cidr="$(docker network inspect "$network" --format '{{(index .IPAM.Config 0).Subnet}}')"
docker run --rm --network "$network" --entrypoint /syncer.test \
  -e MODELSHELF_NFS_INTEGRATION=1 -e "TEST_CIDR=$cidr" -e "PEER_PORT=$peer_port" \
  -v "$volume:/data" -v "$test_dir/syncer.test:/syncer.test:ro" \
  "$image" -test.run '^TestNFSIntegrationSeed$' -test.v
for role in central peer; do
  name="$central"; if [ "$role" = peer ]; then name="$peer"; fi
  docker run -d --name "$name" --privileged --network "$network" \
    -v "$volume:/data:ro" --entrypoint /usr/bin/ganesha.nfsd \
    "$image" -F -f "/data/$role.conf" -p /tmp/ganesha.pid -L STDERR >/dev/null
done
for name in "$central" "$peer"; do
  ready=false
  for _ in $(seq 1 30); do
    if docker logs "$name" 2>&1 | grep 'NFS SERVER INITIALIZED' >/dev/null; then ready=true; break; fi
    sleep 1
  done
  if [ "$ready" != true ]; then docker logs "$name"; exit 1; fi
done
peer_ip="$(docker inspect "$peer" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')"
central_ip="$(docker inspect "$central" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')"
docker run -d --name "$client" --privileged --network "$network" \
  -e MODELSHELF_NFS_INTEGRATION=1 -e "PEER_IP=$peer_ip" -e "PEER_PORT=$peer_port" -e "CENTRAL_IP=$central_ip" \
  -v "$volume:/data:ro" -v "$test_dir/syncer.test:/syncer.test:ro" -v "$test_dir/modelshelf:/modelshelf:ro" \
  --entrypoint sleep "$image" infinity >/dev/null
docker exec "$client" sh -eu -c '
 mkdir -p /mnt/peer /mnt/peer-fallback
 mount -t nfs4 -o "ro,softerr,timeo=50,retrans=2,vers=4.1,lookupcache=positive,port=$PEER_PORT" "$PEER_IP:/modelshelf" /mnt/peer
 mount -t nfs4 -o ro,softerr,timeo=50,retrans=2,vers=4.1,lookupcache=positive "$CENTRAL_IP:/modelshelf" /mnt/peer-fallback
'
docker exec "$client" /syncer.test -test.run '^TestNFSIntegration(Read|Publish)$' -test.v -test.timeout=120s
# Kill the peer while the follower still holds its mount. Central remains online.
docker stop -t 1 "$peer" >/dev/null
docker exec "$client" /syncer.test -test.run '^TestNFSIntegrationOutage$' -test.v -test.timeout=180s
echo 'Client distribution NFS integration: PASS'
