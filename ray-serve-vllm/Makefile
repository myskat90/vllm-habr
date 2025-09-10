PLATFORM ?= linux/amd64
ZIP_FILE = serve.zip
VLLM_VERSION=0.10.1.1
RAY_IMAGE_VERSION=2.49.0.66438d-py312-cu128
CONTAINER_VERSION=1.2.0
APP_DOCKER_IMAGE_VERSIONED = gitlab.ap.com:5050/it-operations/k8s-config/vllm-$(VLLM_VERSION)-ray-$(RAY_IMAGE_VERSION)-serve:$(CONTAINER_VERSION)

package-container:
	zip -r $(ZIP_FILE) . --exclude "venv/*" ".git/*" "*.pyc"
	docker build \
		--platform $(PLATFORM) \
		-t $(APP_DOCKER_IMAGE_VERSIONED) \
		-f ./dockerfile.ray \
		--build-arg RAY_IMAGE_VERSION=$(RAY_IMAGE_VERSION) \
		--build-arg VLLM_VERSION=$(VLLM_VERSION) \
		.
	rm -f $(ZIP_FILE)
