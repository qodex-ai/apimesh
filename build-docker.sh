#!/bin/bash
set -e

# Default values
DOCKER_USERNAME="${DOCKER_USERNAME:-qodex-ai}"
IMAGE_NAME="apimesh"
TAG="${TAG:-latest}"
FULL_IMAGE_NAME="${DOCKER_USERNAME}/${IMAGE_NAME}:${TAG}"

echo "Building Docker image: ${FULL_IMAGE_NAME}"
docker build --pull always -t "${FULL_IMAGE_NAME}" .

echo ""
echo "Build complete! Image: ${FULL_IMAGE_NAME}"
echo ""
echo "To push to Docker Hub, run:"
echo "  docker push ${FULL_IMAGE_NAME}"
echo ""
echo "Or set DOCKER_USERNAME and run:"
echo "  DOCKER_USERNAME=your-username ./build-docker.sh && docker push ${FULL_IMAGE_NAME}"

