
## Часть 2: Настройка Kubernetes и добавление GPU-узлов

В этом разделе мы устанавливаем Kubernetes-кластер с помощью Deckhouse и настраиваем узлы с поддержкой GPU.
Более того, здесь даны подробные комментарии к YAML-файлам конфигурации (config.yml и resources.yml).


### Общий обзор установки
1. Вы логинитесь в приватный Docker Registry Deckhouse, используя предоставленный лицензионный ключ.
2. Запускаете контейнер Deckhouse Installer, где выполняется команда dhctl bootstrap.
3. По завершении развёртывания у вас будет готовый Kubernetes-кластер, который можно масштабировать и дополнять.


### Пример запуска:

#### Шаг 1: Аутентификация и запуск контейнера
```bash
base64 -d <<< <LicenseKey> | docker login -u license-token --password-stdin registry.deckhouse.ru
docker run --pull=always -it \
-v "$PWD/config.yml:/config.yml" \
-v "$HOME/.ssh/:/tmp/.ssh/" \
-v "$PWD/resources.yml:/resources.yml" \
-v "$PWD/dhctl-tmp:/tmp/dhctl" \
--network=host \
registry.deckhouse.ru/deckhouse/ee/install:stable bash
```

#### Шаг 2: Внутри контейнера запускаем установку
```bash
dhctl bootstrap --ssh-user=root --ssh-host=192.168.3.51 --ssh-agent-private-keys=/tmp/.ssh/id_ed25519 \
--config=/config.yml \
--config=/resources.yml \
--ask-become-pass
```

### Ниже следующие файлы:
1. [config.yml](deckhouse_config/config.yml) (основные настройки кластера и Deckhouse)
2. [resources.yml](deckhouse_config/resources.yml) (настройка хранилищ, NodeGroup, GPU и прочих ресурсов)

---
#### config.yml
Первые два ресурса (ClusterConfiguration, InitConfiguration) задают базовые параметры кластера:
 - podSubnetCIDR / serviceSubnetCIDR — сетевое пространство для подов и сервисов.
 - kubernetesVersion — целевая версия Kubernetes.
 - clusterDomain — доменное имя кластера, которое не должно совпадать с публичным доменом.

Объект InitConfiguration определяет:
 - imagesRepo — откуда Deckhouse будет скачивать образы.
 - registryDockerCfg — токен или ключ для аутентификации.

Далее идёт ModuleConfig для модуля Deckhouse с параметрами:
 - bundle, releaseChannel, logLevel (уровень логирования).

Модуль global задаёт:
 - publicDomainTemplate — шаблон для публичных доменов приложений (Grafana, Prometheus и т.д.).
 - ingressClass — класс ingress по умолчанию.

Потом идут настройки user-authn (аутентификация) и публикация API (publishAPI).
Также включаются другие модули:
 - csi-ceph (хранилище Ceph)
 - cni-cilium (сетевой плагин)
 - cert-manager
 - multitenancy-manager
 - runtime-audit-engine
 - operator-trivy
 - metallb (L2-балансировка)
 - console (веб-консоль Deckhouse)
 - ingress-nginx (контроллер Ingress)

В конце config.yml описан StaticClusterConfiguration:
 - internalNetworkCIDRs — список подсетей, используемых узлами кластера.
---
#### resources.yml
Здесь — подробная настройка различных ресурсов:

1) CephClusterConnection / CephClusterAuthentication / CephStorageClass
  - Указываются адреса мониторов (monitors) Ceph, ключи (userKey).
  - Создаются StorageClass для RBD и CephFS (разные типы хранения).

2) LocalPathProvisioner
  - Организует локальное хранилище в определённом каталоге (напр., /data/zdata-relational) на узлах w-db.

3) NodeGroup (один из ключевых ресурсов Deckhouse)
  - w-gpu, w-gpu-3060: GPU-ноды. Лейблы и таинты указывают, что эти узлы предназначены для ML/AI-задач.
  - w-std: обычные worker-ноды (6 штук).
  - w-db: ноды с локальным хранилищем для БД (3 штуки).

4) NodeGroupConfiguration (скрипты .sh)
  - containerd-additional-config.sh: добавляет конфиг, позволяющий containerd использовать nvidia-container-runtime.
  - install-cuda.sh: устанавливает CUDA, nvidia-container-toolkit и драйверы NVIDIA.
  - add-gitlab-registry-cert: добавляет кастомный сертификат (например, для приватного GitLab Registry).

5) StaticInstance
  - Описывает статические узлы по IP-адресам и SSH-доступу.
  - Deckhouse будет использовать эти адреса, чтобы настроить узлы (установка пакетов, драйверов и т.д.).

6) IngressNginxController
  - Создаёт ingress-контроллеры (внешний и внутренний).
  - Метки MetalLB обеспечивают L2-балансировку и выделение IP.

7) ClusterAuthorizationRule и User
  - Настраивают пользователей (admin), их пароли и уровни доступа в кластере (RBAC).

8) Secret/TLS
  - Хранит внутренние сертификаты для CA и ClusterIssuer.


### Общая структура кластера после установки:
- 3 Master-ноды
- 6 Worker-нод (w-std)
- 3 Worker-нод для баз данных (w-db)
- 4 GPU-нод (w-gpu, w-gpu-3060)

### Итог:
1. Настроен Deckhouse для управления кластером (включая Ceph, Ingress и другие модули).
2. Определены и задействованы узлы с GPU (CUDA, nvidia-container-runtime).
3. Установлены сетевые плагины (Cilium), балансировщик MetalLB и прочие компоненты.

Кластер готов к развертыванию решений для распределённого инференса, таких как Ray Serve и vLLM.