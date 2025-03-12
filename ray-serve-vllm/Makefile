ZIP_FILE = serve.zip
VLLM_VERSION=0.6.5
RAY_IMAGE_VERSION=2.42.0-py310-cu121
CONTAINER_VERSION=1.1.2
APP_DOCKER_IMAGE_VERSIONED = gitlab.example.com:5050/it-operations/k8s-config/vllm-$(VLLM_VERSION)-ray-$(RAY_IMAGE_VERSION)-serve:$(CONTAINER_VERSION)

package-container:
	zip -r $(ZIP_FILE) . --exclude "venv/*" ".git/*" "*.pyc"
	docker build \
		-t $(APP_DOCKER_IMAGE_VERSIONED) \
		-f ./dockerfile.ray \
		--build-arg RAY_IMAGE_VERSION=$(RAY_IMAGE_VERSION) \
		--build-arg VLLM_VERSION=$(VLLM_VERSION) \
		.
	rm -f $(ZIP_FILE)
