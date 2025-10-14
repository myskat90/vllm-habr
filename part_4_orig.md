## Часть 4: Настройка Kuberay Cluster
#### Подготовка docker образа и сохранение его в registry

На данном этапе нам необходимо собрать докер образ который мы будем использовать при создании ray кластера
в самом простом варианте - мы берем за основу официальный образ ray (на момент написания статьи это rayproject/ray:2.42.0-py310-cu121) и добавляем наши скрипты serve.py и auth.py, а так же устанавливаем необходимые пакеты

dockerfile.ray:
ARG RAY_IMAGE_VERSION
FROM rayproject/ray:${RAY_IMAGE_VERSION}
ARG VLLM_VERSION
ARG home_dir="/home/ray"
WORKDIR ${home_dir}

# Install required packages
RUN sudo apt-get update && sudo apt-get install -y zip python3-pip

# Copy application files
ADD ./serve.zip .

# Install Python dependencies
RUN pip install vllm==${VLLM_VERSION} \
    && pip install httpx \
    && pip install python-multipart \
    && pip install PyJWT \
    && pip freeze > requirements-new.txt

Запускаем сборку через makefile командой:

make package-container


makefile:
ZIP_FILE = serve.zip
VLLM_VERSION=0.6.5
RAY_IMAGE_VERSION=2.42.0-py310-cu121
CONTAINER_VERSION=1.1.2
APP_DOCKER_IMAGE_VERSIONED = gitlab.ap.com:5050/it-operations/k8s-config/vllm-$(VLLM_VERSION)-ray-$(RAY_IMAGE_VERSION)-serve:$(CONTAINER_VERSION)

package-container:
	zip -r $(ZIP_FILE) . --exclude "venv/*" ".git/*" "*.pyc"
	docker build \
		-t $(APP_DOCKER_IMAGE_VERSIONED) \
		-f ./dockerfile.ray \
		--build-arg RAY_IMAGE_VERSION=$(RAY_IMAGE_VERSION) \
		--build-arg VLLM_VERSION=$(VLLM_VERSION) \
		.
	rm -f $(ZIP_FILE)

Получившийся образ (весит он порядка 20 Gb) выкладываем в удобный registry (в моем случае это частный container registry) командой вида:
docker push gitlab.ap.com:5050/it-operations/k8s-config/ray-diff-v0.32.2-ray-2.42.0-py310-cu121-serve:1.1.4

- **Настройка KubeRay cluster

Ну что ж - у нас всё готово для запуска нашего кластера и разворачивания распределенного инференса внутри него

Для того чтобы хранить модель - я буду использовать cephfs как единое распределенное хранилище, чтобы скачивание происходило один раз и модель была видна сразу всем подам ray кластера

вот values файл - основные моменты (Нужно расписать все настройки):


# Default values for ray-cluster.
# This is a YAML-formatted file.
# Declare variables to be passed into your templates.

# The KubeRay community welcomes PRs to expose additional configuration
# in this Helm chart.

image:
  repository: gitlab.ap.com:5050/it-operations/k8s-config/vllm-0.6.5-ray-2.42.0-py310-cu121-serve
  tag: 1.1.2
  pullPolicy: IfNotPresent

nameOverride: "deepseek-raycluster"
fullnameOverride: "deepseek-raycluster"

imagePullSecrets:
  - name: regcred

labels:
    prometheus.deckhouse.io/custom-target: deepseek-raycluster
annotations:
  ray.io/enable-serve-service: "true"
  prometheus.deckhouse.io/port: "8080"
  prometheus.deckhouse.io/query-param-format: "prometheus"  # По умолчанию ''.
  prometheus.deckhouse.io/sample-limit: "5000"              # По умолчанию принимается не больше 5000 метрик от одного пода.

# common defined values shared between the head and worker
common:
  # containerEnv specifies environment variables for the Ray head and worker containers.
  # Follows standard K8s container env schema.
  containerEnv:
    #  - name: BLAH
    #    value: VAL
    - name: HF_HOME
      value: "/data/model-cache"
    - name: DTYPE
      value: "float16" #для AWQ float16
    - name: GPU_MEMORY_UTIL
      value: "0.97"
    - name: MAX_MODEL_LEN
      value: "32768"
#    - name: MAX_NUM_SEQS
#      value: "128"
#    - name: CPU_OFFLOAD_GB
#      value: "12.0"
#    - name: ENABLE_CHUNKED_PREFILL
#      value: "True"
#    - name: ENABLE_ENFORCE_EAGER
#      value: "True"
#    - name: PYTORCH_CUDA_ALLOC_CONF
#      value: "expandable_segments:True"
head:
  # rayVersion determines the autoscaler's image version.
  # It should match the Ray version in the image of the containers.
  rayVersion: "2.42.0"
  # If enableInTreeAutoscaling is true, the autoscaler sidecar will be added to the Ray head pod.
  # Ray autoscaler integration is supported only for Ray versions >= 1.11.0
  # Ray autoscaler integration is Beta with KubeRay >= 0.3.0 and Ray >= 2.0.0.
  # enableInTreeAutoscaling: true
  # autoscalerOptions is an OPTIONAL field specifying configuration overrides for the Ray autoscaler.
  # The example configuration shown below represents the DEFAULT values.
  # autoscalerOptions:
    # upscalingMode: Default
    # idleTimeoutSeconds is the number of seconds to wait before scaling down a worker pod which is not using Ray resources.
    # idleTimeoutSeconds: 60
    # imagePullPolicy optionally overrides the autoscaler container's default image pull policy (IfNotPresent).
    # imagePullPolicy: IfNotPresent
    # Optionally specify the autoscaler container's securityContext.
    # securityContext: {}
    # env: []
    # envFrom: []
    # resources specifies optional resource request and limit overrides for the autoscaler container.
    # For large Ray clusters, we recommend monitoring container resource usage to determine if overriding the defaults is required.
    # resources:
    #   limits:
    #     cpu: "500m"
    #     memory: "512Mi"
    #   requests:
    #     cpu: "500m"
    #     memory: "512Mi"
  labels:
    component: ray-head
    prometheus.deckhouse.io/custom-target: deepseek-raycluster
  # Note: From KubeRay v0.6.0, users need to create the ServiceAccount by themselves if they specify the `serviceAccountName`
  # in the headGroupSpec. See https://github.com/ray-project/kuberay/pull/1128 for more details.
  serviceAccountName: "sa-deepseek-cluster"
  restartPolicy: ""
  rayStartParams:
    dashboard-host: "0.0.0.0"
    num-cpus: "0"
    metrics-export-port: "8080"
  # containerEnv specifies environment variables for the Ray container,
  # Follows standard K8s container env schema.
  containerEnv:
#   - name: EXAMPLE_ENV
#     value: "1"
#    - name: RAY_GRAFANA_IFRAME_HOST
#      value: http://127.0.0.1:3000
    - name: RAY_GRAFANA_HOST
      value: https://prometheus.d8-monitoring:9090
    - name: RAY_PROMETHEUS_HOST
      value: https://prometheus.d8-monitoring:9090
  envFrom:
   - secretRef:
       name: auth-config
  # ports optionally allows specifying ports for the Ray container.
  # ports: []
  # resource requests and limits for the Ray head container.
  # Modify as needed for your application.
  # Note that the resources in this example are much too small for production;
  # we don't recommend allocating less than 8G memory for a Ray pod in production.
  # Ray pods should be sized to take up entire K8s nodes when possible.
  # Always set CPU and memory limits for Ray pods.
  # It is usually best to set requests equal to limits.
  # See https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/config.html#resources
  # for further guidance.
  resources:
    limits:
      cpu: "6"
      memory: "8G"
    requests:
      cpu: "3"
      memory: "4G"
  annotations:
    ray.io/enable-serve-service: "true"
    prometheus.deckhouse.io/port: "8080"
    prometheus.deckhouse.io/query-param-format: "prometheus"  # По умолчанию ''.
    prometheus.deckhouse.io/sample-limit: "5000"              # По умолчанию принимается не больше 5000 метрик от одного пода.
  nodeSelector: {}
  tolerations: []
  affinity:
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchExpressions:
              - key: component
                operator: In
                values: [ "ray-head" ]
          topologyKey: "kubernetes.io/hostname"
  # Pod security context.
  podSecurityContext: {}
  # Ray container security context.
  securityContext:
    runAsUser: 1000
    runAsGroup: 100
    runAsNonRoot: true
  # Optional: The following volumes/volumeMounts configurations are optional but recommended because
  # Ray writes logs to /tmp/ray/session_latests/logs instead of stdout/stderr.
  initContainers:
    - name: fix-permissions
      image: busybox
      command: ["sh", "-c", "chown -R 1000:100 /data/model-cache"]
      volumeMounts:
        - name: model-cache
          mountPath: /data/model-cache
  volumes:
    - name: log-volume
      emptyDir: {}
    - name: model-cache
      persistentVolumeClaim:
        claimName: model-cache-pvc
  volumeMounts:
    - mountPath: /data/model-cache
      name: model-cache
    - mountPath: /tmp/ray
      name: log-volume

  # sidecarContainers specifies additional containers to attach to the Ray pod.
  # Follows standard K8s container spec.
  sidecarContainers: []
  # See docs/guidance/pod-command.md for more details about how to specify
  # container command for head Pod.
  command: []
  args: []
  # Optional, for the user to provide any additional fields to the service.
  # See https://pkg.go.dev/k8s.io/Kubernetes/pkg/api/v1#Service
  headService:
    metadata:
      annotations:
        ray.io/enable-serve-service: "true"


worker:
  # If you want to disable the default workergroup
  # uncomment the line below
  # disabled: true
  groupName: rtx-3090
  replicas: 2
  minReplicas: 2
  maxReplicas: 2
  labels:
    component: ray-worker
  serviceAccountName: ""
  restartPolicy: ""
  rayStartParams:
    node-ip-address: "$MY_POD_IP"
  # containerEnv specifies environment variables for the Ray container,
  # Follows standard K8s container env schema.
  containerEnv:
  - name: MY_POD_IP
    valueFrom:
      fieldRef:
        fieldPath: status.podIP
  envFrom:
  - secretRef:
      name: auth-config
    # - secretRef:
    #     name: my-env-secret
  # ports optionally allows specifying ports for the Ray container.
  # ports: []
  # resource requests and limits for the Ray head container.
  # Modify as needed for your application.
  # Note that the resources in this example are much too small for production;
  # we don't recommend allocating less than 8G memory for a Ray pod in production.
  # Ray pods should be sized to take up entire K8s nodes when possible.
  # Always set CPU and memory limits for Ray pods.
  # It is usually best to set requests equal to limits.
  # See https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/config.html#resources
  # for further guidance.
  resources:
    limits:
      cpu: "16"
      memory: "24G"
      nvidia.com/gpu: "1"
    requests:
      cpu: "8"
      memory: "12G"
      nvidia.com/gpu: "1"
  annotations: {}
  nodeSelector:
    node.deckhouse.io/group: "w-gpu"
  tolerations:
    - key: "dedicated.apiac.ru"
      operator: "Equal"
      value: "w-gpu"
      effect: "NoExecute"
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              - key: "node.deckhouse.io/group"
                operator: In
                values: ["w-gpu"]
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchExpressions:
              - key: component
                operator: In
                values: ["ray-worker"]
          topologyKey: "kubernetes.io/hostname"
  # Pod security context.
  podSecurityContext: {}
  # Ray container security context.
  securityContext:
    runAsUser: 1000
    runAsGroup: 100
    runAsNonRoot: true
  # Optional: The following volumes/volumeMounts configurations are optional but recommended because
  # Ray writes logs to /tmp/ray/session_latests/logs instead of stdout/stderr.
  initContainers:
    - name: fix-permissions
      image: busybox
      command: [ "sh", "-c", "chown -R 1000:100 /data/model-cache" ]
      volumeMounts:
        - name: model-cache
          mountPath: /data/model-cache
  volumes:
    - name: log-volume
      emptyDir: {}
    - name: model-cache
      persistentVolumeClaim:
        claimName: model-cache-pvc
  volumeMounts:
    - mountPath: /data/model-cache
      name: model-cache
    - mountPath: /tmp/ray
      name: log-volume
  # sidecarContainers specifies additional containers to attach to the Ray pod.
  # Follows standard K8s container spec.
  sidecarContainers: []
  # See docs/guidance/pod-command.md for more details about how to specify
  # container command for worker Pod.
  command: []
  args: []

# The map's key is used as the groupName.
# For example, key:small-group in the map below
# will be used as the groupName
additionalWorkerGroups:
  rtx-3060:
    # Disabled by default
    disabled: true
    replicas: 1
    minReplicas: 1
    maxReplicas: 1
    labels:
      component: ray-worker-3060
    serviceAccountName: ""
    restartPolicy: ""
    rayStartParams:
      node-ip-address: "$MY_POD_IP"
    # containerEnv specifies environment variables for the Ray container,
    # Follows standard K8s container env schema.
    containerEnv:
      - name: MY_POD_IP
        valueFrom:
          fieldRef:
            fieldPath: status.podIP
    envFrom:
      - secretRef:
          name: auth-config
    # ports optionally allows specifying ports for the Ray container.
    # ports: []
    # resource requests and limits for the Ray head container.
    # Modify as needed for your application.
    # Note that the resources in this example are much too small for production;
    # we don't recommend allocating less than 8G memory for a Ray pod in production.
    # Ray pods should be sized to take up entire K8s nodes when possible.
    # Always set CPU and memory limits for Ray pods.
    # It is usually best to set requests equal to limits.
    # See https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/config.html#resources
    # for further guidance.
    resources:
      limits:
        cpu: "16"
        memory: "24G"
        nvidia.com/gpu: "1"
      requests:
        cpu: "8"
        memory: "12G"
        nvidia.com/gpu: "1"
    annotations: {}
    nodeSelector:
      node.deckhouse.io/group: "w-gpu-3060"
#      kubernetes.io/hostname: "k8s-w4-gpu.apiac.ru"
    tolerations:
      - key: "dedicated.apiac.ru"
        operator: "Equal"
        value: "w-gpu"
        effect: "NoExecute"
    affinity:
      nodeAffinity:
        requiredDuringSchedulingIgnoredDuringExecution:
          nodeSelectorTerms:
            - matchExpressions:
                - key: "node.deckhouse.io/group"
                  operator: In
                  values: [ "w-gpu-3060" ]
      podAntiAffinity:
        requiredDuringSchedulingIgnoredDuringExecution:
          - labelSelector:
              matchExpressions:
                - key: component
                  operator: In
                  values: [ "ray-worker-3060" ]
            topologyKey: "kubernetes.io/hostname"
    # Pod security context.
    podSecurityContext: {}
    # Ray container security context.
    securityContext:
      runAsUser: 1000
      runAsGroup: 100
      runAsNonRoot: true
    # Optional: The following volumes/volumeMounts configurations are optional but recommended because
    # Ray writes logs to /tmp/ray/session_latests/logs instead of stdout/stderr.
    initContainers:
      - name: fix-permissions
        image: busybox
        command: [ "sh", "-c", "chown -R 1000:100 /data/model-cache" ]
        volumeMounts:
          - name: model-cache
            mountPath: /data/model-cache
    volumes:
      - name: log-volume
        emptyDir: {}
      - name: model-cache
        persistentVolumeClaim:
          claimName: model-cache-pvc
    volumeMounts:
      - mountPath: /data/model-cache
        name: model-cache
      - mountPath: /tmp/ray
        name: log-volume
    sidecarContainers: []
    # See docs/guidance/pod-command.md for more details about how to specify
    # container command for worker Pod.
    command: []
    args: []

# Configuration for Head's Kubernetes Service
service:
  # This is optional, and the default is ClusterIP.
  type: ClusterIP

Дальше запускаем helm chart (в моем случае с помощью argo cd)

в результате получаем запущенный кластер, готовый к работе

дальше необходимо создать ray application (можно с помощью задания конфигурации и crd) или же с помощью запроса к api (расписать что происходит в вызове):
{
          "applications":[
            {
                "import_path":"serve:model",
                "name":"Dolphin3.0",
                "route_prefix":"/",
                "autoscaling_config":{
                    "min_replicas":1,
                    "initial_replicas":1,
                    "max_replicas":1
                },
                "deployments":[
                    {
                      "name":"VLLMDeployment",
                      "num_replicas":1,
                      "ray_actor_options":{},
                      "deployment_ready_timeout_s": 1200
                    }
                ],
                "runtime_env":{
                    "working_dir":"file:///home/ray/serve.zip",
                    "env_vars":{
                      "MODEL_ID":"Valdemardi/Dolphin3.0-R1-Mistral-24B-AWQ",
                      "TENSOR_PARALLELISM":"1",
                      "PIPELINE_PARALLELISM":"2",
                      "MODEL_NAME":"Dolphin3.0"
                    }
                }
              }
          ]
        }

 Запускать мы будем модель Dolphin3.0 (она мне показалась более стабильной), которую возьмем с сайта https://huggingface.co/

 после запуска видим работающий кластер и в ray-dashboard видим успешно запущенное приложение

 давайте протестируем наш API:

1. Мы создали несколько пользователей с разными ролями ( разместили их в secrets k8s - для упрощения демонстрации)

auth.yaml:
apiVersion: v1
kind: Secret
metadata:
  name: auth-config
  namespace: kuberay-projects
type: Opaque
data:
  # JWT настройки
  JWT_KEY: ZGVmYXVsdF9qd3RfS2V5        # "default_jwt_key"
  ACCESS_TOKEN_EXPIRE_MINUTES: NjA=     # "60"
  SKIP_EXP_CHECK: ZmFsc2U=              # "false"
  HUGGING_FACE_HUB_TOKEN: ""
  # Список пользователей (перечисленные псевдонимы, разделенные запятой)
  USER_LIST: QUxJQ0UsIEJPQg==           # "ALICE, BOB"

  # Данные для пользователя ALICE
  ALICE_USERNAME: YWxpY2U=              # "alice"
  ALICE_HASHED_PASSWORD: ZmFrZWhhc2hlZGhhc2g=  # пример (хэшированное значение)
  ALICE_ROLE: YWRtaW4=                 # "admin"

  # Данные для пользователя BOB
  BOB_USERNAME: Ym9i                   # "bob"
  BOB_HASHED_PASSWORD: ZmFrZWhhc2hlZGhhc2g=     # пример (хэшированное значение)
  BOB_ROLE: Z3Vlc3Q=                   # "guest" или "user"

сгенерировали пароли и захэшировали их:
gen_pwd.py:
import hashlib
import secrets


def generate_salt(length: int = 16) -> str:
    return secrets.token_hex(length)


def hash_password(password: str, salt: str) -> str:
    # Конкатенация соли и пароля, затем вычисление SHA-256
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


# Пример использования:
salt = generate_salt()  # Например, "3b1f2e..."
password = "my_password"
hashed = hash_password(password, salt)
print("Соль:", salt)
print("Хэш:", hashed)

Теперь давайте для начала авторизуемся и получим jwt токен:
я делаю вызовы для удобства в postman

curl --location 'https://openai-api.<наш домен>/token' \
--form 'username="admin"' \
--form 'password="password"'

получаем ответ:
{
    "access_token": "<токен>",
    "token_type": "bearer"
}

Запускать мы будем модель Dolphin3.0 (она мне показалась более стабильной), которую возьмем с сайта https://huggingface.co/

 после запуска видим работающий кластер и в ray-dashboard видим успешно запущенное приложение

 давайте протестируем наш API:

1. Мы создали несколько пользователей с разными ролями ( разместили их в secrets k8s - для упрощения демонстрации)

auth.yaml:
apiVersion: v1
kind: Secret
metadata:
  name: auth-config
  namespace: kuberay-projects
type: Opaque
data:
  # JWT настройки
  JWT_KEY: ZGVmYXVsdF9qd3RfS2V5        # "default_jwt_key"
  ACCESS_TOKEN_EXPIRE_MINUTES: NjA=     # "60"
  SKIP_EXP_CHECK: ZmFsc2U=              # "false"
  HUGGING_FACE_HUB_TOKEN: ""
  # Список пользователей (перечисленные псевдонимы, разделенные запятой)
  USER_LIST: QUxJQ0UsIEJPQg==           # "ALICE, BOB"

  # Данные для пользователя ALICE
  ALICE_USERNAME: YWxpY2U=              # "alice"
  ALICE_HASHED_PASSWORD: ZmFrZWhhc2hlZGhhc2g=  # пример (хэшированное значение)
  ALICE_ROLE: YWRtaW4=                 # "admin"

  # Данные для пользователя BOB
  BOB_USERNAME: Ym9i                   # "bob"
  BOB_HASHED_PASSWORD: ZmFrZWhhc2hlZGhhc2g=     # пример (хэшированное значение)
  BOB_ROLE: Z3Vlc3Q=                   # "guest" или "user"

сгенерировали пароли и захэшировали их:
gen_pwd.py:
import hashlib
import secrets


def generate_salt(length: int = 16) -> str:
    return secrets.token_hex(length)


def hash_password(password: str, salt: str) -> str:
    # Конкатенация соли и пароля, затем вычисление SHA-256
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


# Пример использования:
salt = generate_salt()  # Например, "3b1f2e..."
password = "my_password"
hashed = hash_password(password, salt)
print("Соль:", salt)
print("Хэш:", hashed)

Теперь давайте для начала авторизуемся и получим jwt токен:
я делаю вызовы для удобства в postman

curl --location 'https://openai-api.<наш домен>/token' \
--form 'username="admin"' \
--form 'password="password"'

получаем ответ:
{
    "access_token": "<токен>",
    "token_type": "bearer"
}


теперь пробуем вызвать через API сам инференс (задав один из самых сложных вопросов во вселенной):

curl --location 'https://openai-api.movme.ru/v1/chat/completions' \
--header 'Content-Type: application/json' \
--header 'Authorization: ••••••' \
--data '{
  "model": "Dolphin3.0",
  "messages": [
    {
      "role": "user",
      "content": "Сколько четвергов в марте 2025 года?"
    }
  ],
  "stream": false,
  "max_tokens": 2000,
  "temperature": 0.85,
  "top_p": 0.95,
  "top_k": 50,
  "repetition_penalty": 1.0
}
'

Получаем ответ (по которому видим, что даже ИИ еще не способен знать всё, но в этот раз он хотя бы посчитал правильное количество):
{
    "id": "chatcmpl-83b78eaa98155738c8c6c77b95d9bc10",
    "object": "chat.completion",
    "created": 1741762050,
    "model": "Dolphin3.0",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "В 2025 году Март будет иметь 31 день, как и в любом году. Чтобы определить количество четвергов в марте 2025 года, нам нужно узнать, на какой день падает начало каждого месяца и на какой день - начало следующего месяца.\n\nТак как 1 марта 2025 года попадает на среду, то первое повышение будет с субботы (4 марта) по вторник (7 марта). Таким образом, первый четверг будет с 4 по 7 марта.\n\nТеперь посчитаем количество дней во втором месяце (с 8 по 31 марта), чтобы выяснить, сколько полных четвертов будет в этом периоде. Всего в марте 2025 года будет 31 день, и если мы исключим первые 7 дней, то останется 24 дня. Количество полных недельных циклов в 24 днях составляет 3 полные недели (21 день) с двумя дополнительными днями. Таким образом, будет ещё 3 полных четверта в конце месяца.\n\nИтого у нас будет 1 (с 4 по 7 марта) + 3 (с 11 по 14 марта, 18 по 21 марта, 25 по 28 марта) = 4 четверга в марте 2025 года. Остатки не составляют четверга, так как после 28 марта остаётся меньше 7 дней до конца месяца, а 31 марта приходится на пятницу.",
                "tool_calls": []
            },
            "logprobs": null,
            "finish_reason": "stop",
            "stop_reason": null
        }
    ],
    "usage": {
        "prompt_tokens": 23,
        "total_tokens": 366,
        "completion_tokens": 343,
        "prompt_tokens_details": null
    },
    "prompt_logprobs": null
}


Ура! наш инференс работает
В интерфейсе dashboard видим нагрузку на GPU а так же скорость работы модели - порядка 40 t/s
![dashboard_cluster](img/dashboard_cluster.png)
![dashboard_logs](img/dashboard_logs.png)


Отлично! API протестировали - давайте теперь подключим удобный интерфейс
теперь пробуем вызвать через API сам инференс (задав один из самых сложных вопросов во вселенной):

curl --location 'https://openai-api.movme.ru/v1/chat/completions' \
--header 'Content-Type: application/json' \
--header 'Authorization: ••••••' \
--data '{
  "model": "Dolphin3.0",
  "messages": [
    {
      "role": "user",
      "content": "Сколько четвергов в марте 2025 года?"
    }
  ],
  "stream": false,
  "max_tokens": 2000,
  "temperature": 0.85,
  "top_p": 0.95,
  "top_k": 50,
  "repetition_penalty": 1.0
}
'

Получаем ответ (по которому видим, что даже ИИ еще не способен знать всё, но в этот раз он хотя бы посчитал правильное количество):
{
    "id": "chatcmpl-83b78eaa98155738c8c6c77b95d9bc10",
    "object": "chat.completion",
    "created": 1741762050,
    "model": "Dolphin3.0",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "В 2025 году Март будет иметь 31 день, как и в любом году. Чтобы определить количество четвергов в марте 2025 года, нам нужно узнать, на какой день падает начало каждого месяца и на какой день - начало следующего месяца.\n\nТак как 1 марта 2025 года попадает на среду, то первое повышение будет с субботы (4 марта) по вторник (7 марта). Таким образом, первый четверг будет с 4 по 7 марта.\n\nТеперь посчитаем количество дней во втором месяце (с 8 по 31 марта), чтобы выяснить, сколько полных четвертов будет в этом периоде. Всего в марте 2025 года будет 31 день, и если мы исключим первые 7 дней, то останется 24 дня. Количество полных недельных циклов в 24 днях составляет 3 полные недели (21 день) с двумя дополнительными днями. Таким образом, будет ещё 3 полных четверта в конце месяца.\n\nИтого у нас будет 1 (с 4 по 7 марта) + 3 (с 11 по 14 марта, 18 по 21 марта, 25 по 28 марта) = 4 четверга в марте 2025 года. Остатки не составляют четверга, так как после 28 марта остаётся меньше 7 дней до конца месяца, а 31 марта приходится на пятницу.",
                "tool_calls": []
            },
            "logprobs": null,
            "finish_reason": "stop",
            "stop_reason": null
        }
    ],
    "usage": {
        "prompt_tokens": 23,
        "total_tokens": 366,
        "completion_tokens": 343,
        "prompt_tokens_details": null Запускать мы будем модель Dolphin3.0 (она мне показалась более стабильной), которую возьмем с сайта https://huggingface.co/

 после запуска видим работающий кластер и в ray-dashboard видим успешно запущенное приложение

 давайте протестируем наш API:

1. Мы создали несколько пользователей с разными ролями ( разместили их в secrets k8s - для упрощения демонстрации)

auth.yaml:
apiVersion: v1
kind: Secret
metadata:
  name: auth-config
  namespace: kuberay-projects
type: Opaque
data:
  # JWT настройки
  JWT_KEY: ZGVmYXVsdF9qd3RfS2V5        # "default_jwt_key"
  ACCESS_TOKEN_EXPIRE_MINUTES: NjA=     # "60"
  SKIP_EXP_CHECK: ZmFsc2U=              # "false"
  HUGGING_FACE_HUB_TOKEN: ""
  # Список пользователей (перечисленные псевдонимы, разделенные запятой)
  USER_LIST: QUxJQ0UsIEJPQg==           # "ALICE, BOB"

  # Данные для пользователя ALICE
  ALICE_USERNAME: YWxpY2U=              # "alice"
  ALICE_HASHED_PASSWORD: ZmFrZWhhc2hlZGhhc2g=  # пример (хэшированное значение)
  ALICE_ROLE: YWRtaW4=                 # "admin"

  # Данные для пользователя BOB
  BOB_USERNAME: Ym9i                   # "bob"
  BOB_HASHED_PASSWORD: ZmFrZWhhc2hlZGhhc2g=     # пример (хэшированное значение)
  BOB_ROLE: Z3Vlc3Q=                   # "guest" или "user"

сгенерировали пароли и захэшировали их:
gen_pwd.py:
import hashlib
import secrets


def generate_salt(length: int = 16) -> str:
    return secrets.token_hex(length)


def hash_password(password: str, salt: str) -> str:
    # Конкатенация соли и пароля, затем вычисление SHA-256
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


# Пример использования:
salt = generate_salt()  # Например, "3b1f2e..."
password = "my_password"
hashed = hash_password(password, salt)
print("Соль:", salt)
print("Хэш:", hashed)

Теперь давайте для начала авторизуемся и получим jwt токен:
я делаю вызовы для удобства в postman

curl --location 'https://openai-api.<наш домен>/token' \
--form 'username="admin"' \
--form 'password="password"'

получаем ответ:
{
    "access_token": "<токен>",
    "token_type": "bearer"
}

Запускать мы будем модель Dolphin3.0 (она мне показалась более стабильной), которую возьмем с сайта https://huggingface.co/

 после запуска видим работающий кластер и в ray-dashboard видим успешно запущенное приложение

 давайте протестируем наш API:

1. Мы создали несколько пользователей с разными ролями ( разместили их в secrets k8s - для упрощения демонстрации)

auth.yaml:
apiVersion: v1
kind: Secret
metadata:
  name: auth-config
  namespace: kuberay-projects
type: Opaque
data:
  # JWT настройки
  JWT_KEY: ZGVmYXVsdF9qd3RfS2V5        # "default_jwt_key"
  ACCESS_TOKEN_EXPIRE_MINUTES: NjA=     # "60"
  SKIP_EXP_CHECK: ZmFsc2U=              # "false"
  HUGGING_FACE_HUB_TOKEN: ""
  # Список пользователей (перечисленные псевдонимы, разделенные запятой)
  USER_LIST: QUxJQ0UsIEJPQg==           # "ALICE, BOB"

  # Данные для пользователя ALICE
  ALICE_USERNAME: YWxpY2U=              # "alice"
  ALICE_HASHED_PASSWORD: ZmFrZWhhc2hlZGhhc2g=  # пример (хэшированное значение)
  ALICE_ROLE: YWRtaW4=                 # "admin"

  # Данные для пользователя BOB
  BOB_USERNAME: Ym9i                   # "bob"
  BOB_HASHED_PASSWORD: ZmFrZWhhc2hlZGhhc2g=     # пример (хэшированное значение)
  BOB_ROLE: Z3Vlc3Q=                   # "guest" или "user"

сгенерировали пароли и захэшировали их:
gen_pwd.py:
import hashlib
import secrets


def generate_salt(length: int = 16) -> str:
    return secrets.token_hex(length)


def hash_password(password: str, salt: str) -> str:
    # Конкатенация соли и пароля, затем вычисление SHA-256
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


# Пример использования:
salt = generate_salt()  # Например, "3b1f2e..."
password = "my_password"
hashed = hash_password(password, salt)
print("Соль:", salt)
print("Хэш:", hashed)

Теперь давайте для начала авторизуемся и получим jwt токен:
я делаю вызовы для удобства в postman

curl --location 'https://openai-api.<наш домен>/token' \
--form 'username="admin"' \
--form 'password="password"'

получаем ответ:
{
    "access_token": "<токен>",
    "token_type": "bearer"
}


теперь пробуем вызвать через API сам инференс (задав один из самых сложных вопросов во вселенной):

curl --location 'https://openai-api.movme.ru/v1/chat/completions' \
--header 'Content-Type: application/json' \
--header 'Authorization: ••••••' \
--data '{
  "model": "Dolphin3.0",
  "messages": [
    {
      "role": "user",
      "content": "Сколько четвергов в марте 2025 года?"
    }
  ],
  "stream": false,
  "max_tokens": 2000,
  "temperature": 0.85,
  "top_p": 0.95,
  "top_k": 50,
  "repetition_penalty": 1.0
}
'

Получаем ответ (по которому видим, что даже ИИ еще не способен знать всё, но в этот раз он хотя бы посчитал правильное количество):
{
    "id": "chatcmpl-83b78eaa98155738c8c6c77b95d9bc10",
    "object": "chat.completion",
    "created": 1741762050,
    "model": "Dolphin3.0",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "В 2025 году Март будет иметь 31 день, как и в любом году. Чтобы определить количество четвергов в марте 2025 года, нам нужно узнать, на какой день падает начало каждого месяца и на какой день - начало следующего месяца.\n\nТак как 1 марта 2025 года попадает на среду, то первое повышение будет с субботы (4 марта) по вторник (7 марта). Таким образом, первый четверг будет с 4 по 7 марта.\n\nТеперь посчитаем количество дней во втором месяце (с 8 по 31 марта), чтобы выяснить, сколько полных четвертов будет в этом периоде. Всего в марте 2025 года будет 31 день, и если мы исключим первые 7 дней, то останется 24 дня. Количество полных недельных циклов в 24 днях составляет 3 полные недели (21 день) с двумя дополнительными днями. Таким образом, будет ещё 3 полных четверта в конце месяца.\n\nИтого у нас будет 1 (с 4 по 7 марта) + 3 (с 11 по 14 марта, 18 по 21 марта, 25 по 28 марта) = 4 четверга в марте 2025 года. Остатки не составляют четверга, так как после 28 марта остаётся меньше 7 дней до конца месяца, а 31 марта приходится на пятницу.",
                "tool_calls": []
            },
            "logprobs": null,
            "finish_reason": "stop",
            "stop_reason": null
        }
    ],
    "usage": {
        "prompt_tokens": 23,
        "total_tokens": 366,
        "completion_tokens": 343,
        "prompt_tokens_details": null
    },
    "prompt_logprobs": null
}


Ура! наш инференс работает
В интерфейсе dashboard видим нагрузку на GPU а так же скорость работы модели - порядка 40 t/s
![dashboard_cluster](img/dashboard_cluster.png)
![dashboard_logs](img/dashboard_logs.png)


Отлично! API протестировали - давайте теперь подключим удобный интерфейс
теперь пробуем вызвать через API сам инференс (задав один из самых сложных вопросов во вселенной):

curl --location 'https://openai-api.movme.ru/v1/chat/completions' \
--header 'Content-Type: application/json' \
--header 'Authorization: ••••••' \
--data '{
  "model": "Dolphin3.0",
  "messages": [
    {
      "role": "user",
      "content": "Сколько четвергов в марте 2025 года?"
    }
  ],
  "stream": false,
  "max_tokens": 2000,
  "temperature": 0.85,
  "top_p": 0.95,
  "top_k": 50,
  "repetition_penalty": 1.0
}
'

Получаем ответ (по которому видим, что даже ИИ еще не способен знать всё, но в этот раз он хотя бы посчитал правильное количество):
{
    "id": "chatcmpl-83b78eaa98155738c8c6c77b95d9bc10",
    "object": "chat.completion",
    "created": 1741762050,
    "model": "Dolphin3.0",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "В 2025 году Март будет иметь 31 день, как и в любом году. Чтобы определить количество четвергов в марте 2025 года, нам нужно узнать, на какой день падает начало каждого месяца и на какой день - начало следующего месяца.\n\nТак как 1 марта 2025 года попадает на среду, то первое повышение будет с субботы (4 марта) по вторник (7 марта). Таким образом, первый четверг будет с 4 по 7 марта.\n\nТеперь посчитаем количество дней во втором месяце (с 8 по 31 марта), чтобы выяснить, сколько полных четвертов будет в этом периоде. Всего в марте 2025 года будет 31 день, и если мы исключим первые 7 дней, то останется 24 дня. Количество полных недельных циклов в 24 днях составляет 3 полные недели (21 день) с двумя дополнительными днями. Таким образом, будет ещё 3 полных четверта в конце месяца.\n\nИтого у нас будет 1 (с 4 по 7 марта) + 3 (с 11 по 14 марта, 18 по 21 марта, 25 по 28 марта) = 4 четверга в марте 2025 года. Остатки не составляют четверга, так как после 28 марта остаётся меньше 7 дней до конца месяца, а 31 марта приходится на пятницу.",
                "tool_calls": []
            },
            "logprobs": null,
            "finish_reason": "stop",
            "stop_reason": null
        }
    ],
    "usage": {
        "prompt_tokens": 23,
        "total_tokens": 366,
        "completion_tokens": 343,
        "prompt_tokens_details": null
    },
    "prompt_logprobs": null
}


Ура! наш инференс работает
В интерфейсе dashboard видим нагрузку на GPU а так же скорость работы модели - порядка 40 t/s
![dashboard_cluster](img/dashboard_cluster.png)
![dashboard_logs](img/dashboard_logs.png)


Отлично! API протестировали - давайте теперь подключим удобный интерфейс
    },
    "prompt_logprobs": null
}


Ура! наш инференс работает
В интерфейсе dashboard видим нагрузку на GPU а так же скорость работы модели - порядка 40 t/s
![dashboard_cluster](img/dashboard_cluster.png)
![dashboard_logs](img/dashboard_logs.png)


Отлично! API протестировали - давайте теперь подключим удобный интерфейс