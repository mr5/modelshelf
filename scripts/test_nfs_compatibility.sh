#!/usr/bin/env bash

set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${MODELSHELF_NFS_TEST_IMAGE:-modelshelf-nfs:compatibility-test}"
require_read_plus="${MODELSHELF_REQUIRE_READ_PLUS:-false}"
network="modelshelf-nfs-test-$RANDOM-$$"
exporter="modelshelf-nfs-test-$RANDOM-$$"
public_exporter="modelshelf-nfs-public-test-$RANDOM-$$"
volume="modelshelf-nfs-test-$RANDOM-$$"

cleanup() {
    docker rm -f "$exporter" >/dev/null 2>&1 || true
    docker rm -f "$public_exporter" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
    docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'docker logs "$exporter" >&2 || true' ERR

docker build -t "$image" "$repo_root/docker/nfs"
docker network create "$network" >/dev/null
docker volume create "$volume" >/dev/null
network_cidr="$(docker network inspect "$network" --format '{{(index .IPAM.Config 0).Subnet}}')"

docker run --detach --name "$exporter" --network "$network" --privileged \
    --env "MODELSHELF_NFS_CLIENTS=$network_cidr" \
    --volume "$volume:/export/artifacts:ro" \
    "$image" >/dev/null

for _ in $(seq 1 30); do
    if docker logs "$exporter" 2>&1 | grep -q "NFS SERVER INITIALIZED"; then
        break
    fi
    sleep 1
done
if ! docker logs "$exporter" 2>&1 | grep -q "NFS SERVER INITIALIZED"; then
    docker logs "$exporter" >&2
    exit 1
fi

# Publish after the exporter starts. This covers atomic-publication visibility,
# root-squashed traversal, ordinary READ, and sparse-file READ_PLUS behavior.
expected_hash="$(
    docker run --rm --entrypoint sh --volume "$volume:/artifacts" "$image" -eu -c '
        artifact=/artifacts/huggingface/example/sparse-model/revision
        mkdir -p "$artifact/.modelshelf"
        truncate -s 64M "$artifact/weights.bin"
        printf modelshelf-read-plus-start \
            | dd of="$artifact/weights.bin" conv=notrunc status=none
        printf modelshelf-read-plus-end \
            | dd of="$artifact/weights.bin" bs=1 seek=67108832 conv=notrunc status=none
        printf "%s\n" \
            "{\"schemaVersion\":1,\"test\":\"nfs-compatibility\"}" \
            > "$artifact/.modelshelf/manifest.json"
        chmod 0755 \
            /artifacts \
            /artifacts/huggingface \
            /artifacts/huggingface/example \
            /artifacts/huggingface/example/sparse-model
        chmod 0555 "$artifact" "$artifact/.modelshelf"
        chmod 0444 "$artifact/weights.bin" "$artifact/.modelshelf/manifest.json"
        sha256sum "$artifact/weights.bin" | awk "{print \$1}"
    '
)"

for version in 4.1 4.2; do
    docker run --rm --privileged --network "$network" --entrypoint sh \
        --env EXPECTED_HASH="$expected_hash" \
        --env NFS_VERSION="$version" \
        --env NFS_SERVER="$exporter" \
        --env REQUIRE_READ_PLUS="$require_read_plus" \
        "$image" -eu -c '
            mkdir -p /mnt/modelshelf
            cleanup_mount() {
                umount /mnt/modelshelf 2>/dev/null || true
            }
            trap cleanup_mount EXIT INT TERM
            mount -t nfs4 -o "ro,hard,vers=$NFS_VERSION,proto=tcp" \
                "$NFS_SERVER:/modelshelf" /mnt/modelshelf
            actual_hash=$(sha256sum \
                /mnt/modelshelf/huggingface/example/sparse-model/revision/weights.bin \
                | awk "{print \$1}")
            test "$actual_hash" = "$EXPECTED_HASH"
            test -r /mnt/modelshelf/huggingface/example/sparse-model/revision/.modelshelf/manifest.json
            if [ "$NFS_VERSION" = 4.2 ]; then
                cp --sparse=always \
                    /mnt/modelshelf/huggingface/example/sparse-model/revision/weights.bin \
                    /tmp/weights.bin
                read_plus_calls=$(awk "
                    /mounted on \/mnt\/modelshelf/ { mounted=1 }
                    mounted && /^[[:space:]]*READ_PLUS:/ { print \$2; exit }
                " /proc/self/mountstats)
                printf "NFSv4.2 READ_PLUS calls: %s\n" "${read_plus_calls:-0}"
                if [ "$REQUIRE_READ_PLUS" = true ]; then
                    test "${read_plus_calls:-0}" -gt 0
                elif [ "${read_plus_calls:-0}" -eq 0 ]; then
                    echo "READ_PLUS was not enabled by this Docker host kernel; content read still passed"
                fi
            fi
            printf "NFSv%s content hash verified\n" "$NFS_VERSION"
        '
done

# Public export remains a double opt-in. Verify that ModelShelf's public CIDR
# is normalized to the client expression expected by Ganesha 6.x.
docker run --detach --name "$public_exporter" --network "$network" --privileged \
    --env MODELSHELF_NFS_CLIENTS=0.0.0.0/0 \
    --env MODELSHELF_NFS_ALLOW_PUBLIC=true \
    --volume "$volume:/export/artifacts:ro" \
    "$image" >/dev/null
for _ in $(seq 1 30); do
    if docker logs "$public_exporter" 2>&1 | grep -q "NFS SERVER INITIALIZED"; then
        break
    fi
    sleep 1
done
if docker logs "$public_exporter" 2>&1 | grep -q "No export entries found"; then
    docker logs "$public_exporter" >&2
    exit 1
fi
docker run --rm --privileged --network "$network" --entrypoint sh \
    --env NFS_SERVER="$public_exporter" "$image" -eu -c '
        mkdir -p /mnt/modelshelf
        trap "umount /mnt/modelshelf 2>/dev/null || true" EXIT INT TERM
        mount -t nfs4 -o ro,hard,vers=4.1,proto=tcp \
            "$NFS_SERVER:/modelshelf" /mnt/modelshelf
        test -r /mnt/modelshelf/huggingface/example/sparse-model/revision/.modelshelf/manifest.json
    '

echo "Explicit public-CIDR export verified"

echo "NFS compatibility test: PASS"
