#!/usr/bin/env bash
set -euo pipefail

# Run the sdk-test-suite integration tests locally.
#
# Prerequisites:
#   - Docker running
#
# Usage:
#   ./etc/run-integration-tests.sh                          # test original test-services
#   ./etc/run-integration-tests.sh --cls                    # test class-based test-services
#   ./etc/run-integration-tests.sh --skip-build             # reuse existing image
#   ./etc/run-integration-tests.sh --cls --skip-build       # combine flags

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SDK_TEST_SUITE_VERSION="v3.4"
JAR_URL="https://github.com/restatedev/sdk-test-suite/releases/download/${SDK_TEST_SUITE_VERSION}/restate-sdk-test-suite.jar"
JAR_PATH="${REPO_ROOT}/tmp/restate-sdk-test-suite.jar"
RESTATE_IMAGE="${RESTATE_CONTAINER_IMAGE:-ghcr.io/restatedev/restate:main}"
REPORT_DIR="${REPO_ROOT}/tmp/test-report"

SKIP_BUILD=false
USE_CLS=false
for arg in "$@"; do
  case "$arg" in
    --skip-build) SKIP_BUILD=true ;;
    --cls) USE_CLS=true ;;
  esac
done

if [ "$USE_CLS" = true ]; then
  SERVICE_IMAGE="restatedev/test-services-python-cls"
  DOCKERFILE="${REPO_ROOT}/test-services-cls/Dockerfile"
  EXCLUSIONS="${REPO_ROOT}/test-services-cls/exclusions.yaml"
  ENV_FILE="${REPO_ROOT}/test-services-cls/.env"
  echo "==> Using class-based test-services (test-services-cls/)"
else
  SERVICE_IMAGE="restatedev/test-services-python"
  DOCKERFILE="${REPO_ROOT}/test-services/Dockerfile"
  EXCLUSIONS="${REPO_ROOT}/test-services/exclusions.yaml"
  ENV_FILE="${REPO_ROOT}/test-services/.env"
  echo "==> Using original test-services (test-services/)"
fi

# 1. Build the test-services Docker image
if [ "$SKIP_BUILD" = false ]; then
  echo "==> Building test-services Docker image..."
  docker build -f "${DOCKERFILE}" -t "${SERVICE_IMAGE}" "${REPO_ROOT}"
fi

# 2. Download the test suite JAR (if not cached)
mkdir -p "$(dirname "$JAR_PATH")"
if [ ! -f "$JAR_PATH" ]; then
  echo "==> Downloading sdk-test-suite ${SDK_TEST_SUITE_VERSION}..."
  curl -fSL -o "$JAR_PATH" "$JAR_URL"
fi

# 3. Pull restate image
echo "==> Pulling Restate image: ${RESTATE_IMAGE}"
docker pull "${RESTATE_IMAGE}"

# 4. Run the test suite via Docker (no local Java needed)
echo "==> Running integration tests..."
rm -rf "${REPORT_DIR}"
mkdir -p "${REPORT_DIR}"

docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "${JAR_PATH}:/opt/test-suite.jar:ro" \
  -v "${EXCLUSIONS}:/opt/exclusions.yaml:ro" \
  -v "${ENV_FILE}:/opt/service.env:ro" \
  -v "${REPORT_DIR}:/opt/test-report" \
  -e RESTATE_CONTAINER_IMAGE="${RESTATE_IMAGE}" \
  --network host \
  eclipse-temurin:21-jre \
  java -jar /opt/test-suite.jar run \
    --exclusions-file=/opt/exclusions.yaml \
    --service-container-env-file=/opt/service.env \
    --report-dir=/opt/test-report \
    "${SERVICE_IMAGE}"

echo "==> Done. Test report: ${REPORT_DIR}"
