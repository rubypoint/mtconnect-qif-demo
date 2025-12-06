#!/bin/bash

# Set the repository root to the directory containing this script
export LINUXCNC_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Set SHDR environment variables with recommended values
export SHDR_HOST="0.0.0.0"
export SHDR_DUMP_FILE="$LINUXCNC_REPO_ROOT/logs/adapter_lcnc.log"
export SHDR_NOISE_FEED_STD="0.1"

# Display the set variables
echo "Environment variables set:"
echo "LINUXCNC_REPO_ROOT=$LINUXCNC_REPO_ROOT"
echo "SHDR_HOST=$SHDR_HOST"
echo "SHDR_DUMP_FILE=$SHDR_DUMP_FILE"
echo "SHDR_NOISE_FEED_STD=$SHDR_NOISE_FEED_STD"
