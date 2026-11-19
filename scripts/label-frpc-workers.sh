#!/bin/sh
set -eu

# Docker Swarm constraints support exact matches, not hostname globs.
# Mark only worker nodes whose hostname begins with the requested prefix.
PREFIX="${1:-worker}"
LABEL="ctfd_whale_frpc"

if ! command -v docker >/dev/null 2>&1; then
    echo "docker CLI is required; run this script on the Swarm manager." >&2
    exit 1
fi

MATCHED=0
for NODE_ID in $(docker node ls --filter role=worker -q); do
    HOSTNAME=$(docker node inspect --format '{{ .Description.Hostname }}' "$NODE_ID")
    case "$HOSTNAME" in
        "$PREFIX"*)
            docker node update --label-add "$LABEL=true" "$NODE_ID" >/dev/null
            echo "FRPC enabled: $HOSTNAME"
            MATCHED=$((MATCHED + 1))
            ;;
        *)
            # Remove this plugin-owned label from workers outside the prefix,
            # so a renamed/reclassified node does not keep receiving FRPC.
            docker node update --label-rm "$LABEL" "$NODE_ID" >/dev/null 2>&1 || true
            ;;
    esac
done

if [ "$MATCHED" -eq 0 ]; then
    echo "No worker hostname beginning with '$PREFIX' was found." >&2
    exit 1
fi

echo "Labelled $MATCHED worker node(s). Deploy or update the FRPC stack now."
