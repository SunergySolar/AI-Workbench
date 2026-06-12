#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE_PATH="${1:-$SCRIPT_DIR/Neighborhood.jpeg}"
CRITERIA="${2:-'[{"name":"document legibility","type":"quality"},{"name":"image sharpness","type":"quality"},{"name":"proper exposure","type":"quality"},{"name":"absence of artifacts","type":"quality"}]'}"
BASE_URL="${3:-http://192.168.5.233:4001}"

# Load API key from .env
ENV_FILE="$SCRIPT_DIR/../../.env"
API_KEY=$(grep "^DEFAULT_LITELLM_MASTER_KEY=" "$ENV_FILE" | cut -d'=' -f2)
if [ -z "$API_KEY" ]; then
    echo "ERROR: DEFAULT_LITELLM_MASTER_KEY not found in .env" >&2
    exit 1
fi

SIZE_KB=$(du -k "$IMAGE_PATH" | cut -f1)
echo "Sending ${SIZE_KB} KB image to quality-checker..."
echo ""

curl -s -X POST "$BASE_URL/v1/quality-check/assess" \
    -H "Authorization: Bearer $API_KEY" \
    -F "image=@$IMAGE_PATH" \
    -F "criteria=$CRITERIA" \
    | python3 -m json.tool
