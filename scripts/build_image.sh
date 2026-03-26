#!/bin/bash
# 构建 fisco-routing Docker 镜像
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "Building fisco-routing Docker image..."
docker build -t fisco-routing:latest "$PROJECT_DIR"
echo "Done. Image: fisco-routing:latest"
