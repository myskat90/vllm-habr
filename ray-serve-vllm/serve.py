import os  # Стандартная библиотека: доступ к переменным окружения и файловой системе
import logging  # Логирование событий (уровни, формат, вывод)
from typing import Dict, Optional, List, Any  # Типизация для повышения читаемости и валидации IDE
from datetime import datetime, timedelta  # Работа с датой/временем и временными интервалами

from fastapi import FastAPI, Depends, HTTPException, status  # FastAPI: веб-фреймворк и вспомогательные классы
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm, HTTPBearer, HTTPAuthorizationCredentials  # Безопасность и схемы авторизации
from starlette.requests import Request  # Тип запроса Starlette (базис FastAPI)
from starlette.responses import StreamingResponse, JSONResponse  # Типы ответов (стриминг и JSON)
from starlette.middleware.cors import CORSMiddleware  # CORS-middleware для междоменного доступа

from ray import serve  # Ray Serve: декларативное развертывание и оркестрация Python-сервисов

# vLLM (проверено на 0.10.1.1)
from vllm.engine.arg_utils import AsyncEngineArgs  # Аргументы движка, создаваемые из CLI/конфига
from vllm.engine.async_llm_engine import AsyncLLMEngine  # Асинхронный фронт LLM для онлайн-сервинга
from vllm.entrypoints.openai.cli_args import make_arg_parser  # Построение CLI-парсера под OpenAI-совместимый сервер
from vllm.entrypoints.openai.protocol import (
    ChatCompletionRequest,  # Модель входного запроса OpenAI chat.completions
    ChatCompletionResponse,  # Модель успешного ответа OpenAI chat.completions
    ErrorResponse,  # Модель ошибки OpenAI-совместимого ответа
)
from vllm.entrypoints.openai.serving_chat import OpenAIServingChat  # Обработчик /v1/chat/completions поверх AsyncLLMEngine
from vllm.entrypoints.openai.serving_models import (
    BaseModelPath,           # dataclass: описывает имя модели и путь
    OpenAIServingModels,     # Менеджер базовых моделей/адаптеров; отдает /v1/models и LoRA-роуты
)
from vllm.utils import FlexibleArgumentParser  # Расширенный парсер аргументов (гибкая работа с CLI)

# Auth
from auth import (
    verify_jwt_token,  # Проверка и декодирование JWT
    check_role,  # Валидация роли пользователя
    create_access_token,  # Выпуск JWT токена
    authenticate_user,  # Проверка учетных данных пользователя
    TokenResponse,  # Pydantic-модель ответа с токеном
)

logger = logging.getLogger("ray.serve")  # Создаем логгер в неймспейсе Ray Serve
logger.setLevel(logging.DEBUG)  # Устанавливаем уровень логирования DEBUG для подробностей

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")  # Схема OAuth2 для получения токена по /token
bearer_scheme = HTTPBearer()  # Схема извлечения токена из Authorization: Bearer

app = FastAPI()  # Инициализация приложения FastAPI
app.add_middleware(
    CORSMiddleware,  # Подключаем CORS
    allow_origins=["*"],  # Разрешаем запросы с любых источников (на этапе разработки)
    allow_credentials=True,  # Разрешаем куки и креды
    allow_methods=["*"],  # Разрешаем все методы
    allow_headers=["*"],  # Разрешаем все заголовки
)

# -------------------------
# Разбор CLI vLLM до класса (чтобы символ был виден воркеру Ray)
# -------------------------
def parse_vllm_args(cli_args: Dict[str, Any]) -> Any:
    """
    Гибкий разбор CLI vLLM. Исключаем ключи, которые не идут в AsyncEngineArgs.
    Ключи должны быть в стиле vLLM CLI: 'tensor-parallel-size', 'max-model-len', и т.п.
    """
    exclude_args_from_engine = ["model_name", "chat_template"]  # Эти ключи не передаем в EngineArgs напрямую
    parser = FlexibleArgumentParser(description="vLLM CLI")  # Создаем парсер аргументов
    parser = make_arg_parser(parser)  # Добавляем стандартные vLLM OpenAI-CLI аргументы к парсеру

    arg_strings: List[str] = []  # Будущий список строковых аргументов для имитации CLI
    for key, value in cli_args.items():  # Идем по словарю конфигурации
        if key in exclude_args_from_engine:  # Пропускаем исключенные ключи
            continue
        if isinstance(value, bool):  # Булевы флаги передаем без значения
            if value:
                arg_strings.extend([f"--{key}"])  # Добавляем флаг, только если True
            continue
        if isinstance(value, list):  # Для списков повторяем ключ для каждого элемента
            for v in value:
                arg_strings.extend([f"--{key}", str(v)])  # Преобразуем в строковый формат CLI
            continue
        if value is not None:  # Для остальных — ключ и значение
            arg_strings.extend([f"--{key}", str(value)])  # Значение приводим к строке

    logger.info(f"Parsing CLI args: {arg_strings}")  # Логируем финальный набор CLI-аргументов
    parsed_args = parser.parse_args(args=arg_strings)  # Парсим аргументы в объект Namespace
    return parsed_args  # Возвращаем разобранные аргументы


@serve.deployment(name="VLLMDeployment")  # Декоратор Ray Serve: регистрирует деплой с указанным именем
@serve.ingress(app)  # Декоратор: подключает FastAPI-приложение как ingress для деплоя
class VLLMDeployment:
    """
    OpenAI-совместимый слой поверх vLLM с инициализацией на GPU-акторе.
    Поддерживает V1-engine, тензор/пайплайн параллелизм через Ray backend.
    """
    def __init__(
            self,
            cli_args: Dict[str, Any],  # Словарь CLI-аргументов для vLLM
            chat_template: Optional[str] = None,  # Необязательный шаблон чата
    ) -> None:
        logger.info(f"[init] VLLMDeployment on actor, cli_args={cli_args}")  # Логируем запуск конструктора на акторе

        # Парсим CLI уже НА акторе (где есть CUDA/ROCm), чтобы не упасть на драйвере.
        parsed_args = parse_vllm_args(cli_args)  # Превращаем словарь в объект аргументов
        engine_args: AsyncEngineArgs = AsyncEngineArgs.from_cli_args(parsed_args)  # Создаем EngineArgs из CLI

        # Для PP>1 обязателен Ray backend; при PP=1 тоже безопасно держать worker_use_ray=True.
        if getattr(engine_args, "pipeline_parallel_size", 1) and engine_args.pipeline_parallel_size > 1:
            engine_args.worker_use_ray = True  # Включаем Ray-бэкенд для воркеров
            engine_args.distributed_executor_backend = "ray"
        else:
            engine_args.worker_use_ray = True  # На единичном PP все равно используем Ray-воркеры (безопасно)

        self.engine_args = engine_args  # Сохраняем EngineArgs для последующего использования
        self.response_role = getattr(parsed_args, "response_role", "assistant")  # Роль сообщения по умолчанию
        self.chat_template = chat_template or getattr(parsed_args, "chat_template", None) or os.environ.get("CHAT_TEMPLATE")  # Определяем шаблон чата
        self.model_name = cli_args.get("model_name")  # Имя модели, под которым публикуем в /v1/models (если задано)

        # vLLM движок (асинхронный фронт)
        self.engine = AsyncLLMEngine.from_engine_args(engine_args)  # Инициализируем асинхронный LLM-движок

        # OpenAI-совместимые обёртки (лениво)
        self.openai_serving_models: Optional[OpenAIServingModels] = None  # Поздняя инициализация менеджера моделей
        self.openai_serving_chat: Optional[OpenAIServingChat] = None  # Поздняя инициализация чат-обработчика

        logger.info("[init] vLLM AsyncLLMEngine initialized")  # Подтверждаем успешную инициализацию движка

    async def _build_openai_models(self) -> OpenAIServingModels:
        """
        Создаёт OpenAIServingModels с базовыми моделями.
        В vLLM 0.10.1.* сигнатура: (engine_client, model_config, base_model_paths)
        """
        if self.openai_serving_models:
            return self.openai_serving_models  # Если уже создан — возвращаем кеш

        model_config = await self.engine.get_model_config()  # Получаем конфигурацию модели с воркеров

        # Если указаны served_model_name(ы), отразим их в BaseModelPath.name
        served_names = []  # Список имен, под которыми модель будет видна в /v1/models
        if getattr(self.engine_args, "served_model_name", None):  # Проверяем, заданы ли псевдонимы моделей
            names = self.engine_args.served_model_name  # Может быть строкой или списком
            if isinstance(names, str):
                served_names = [names]  # Приводим к списку
            else:
                served_names = list(names)  # Копируем в список
        else:
            # По умолчанию публикуем под self.model_name (если задан) либо исходным model id
            served_names = [self.model_name or self.engine_args.model]  # Если нет явного имени, используем исходный идентификатор

        base_model_paths = [
            BaseModelPath(name=n, model_path=self.engine_args.model) for n in served_names  # Соответствие "имя → путь модели"
        ]

        self.openai_serving_models = OpenAIServingModels(
            engine_client=self.engine,  # Ссылка на AsyncLLMEngine (EngineClient интерфейс)
            model_config=model_config,  # Конфиг модели (dtype, контекст, т.д.)
            base_model_paths=base_model_paths,  # Набор опубликованных имен и путей
        )

        # Если у OpenAIServingModels есть готовый FastAPI-router,
        # то добавим его — это даст /v1/models и (при VLLM_ALLOW_RUNTIME_LORA_UPDATING=1)
        # эндпоинты для LoRA (/v1/load_lora_adapter, /v1/unload_lora_adapter)
        if hasattr(self.openai_serving_models, "router"):
            try:
                app.include_router(self.openai_serving_models.router)  # Подключаем роуты менеджера моделей к FastAPI
                logger.info("[init] Included OpenAIServingModels.router into FastAPI app")  # Логируем успешную регистрацию
            except Exception as e:
                logger.warning(f"[init] Failed to include OpenAIServingModels.router: {e}")  # Логируем, но не падаем

        return self.openai_serving_models  # Возвращаем инициализированный менеджер

    async def _initialize_serving_chat(self) -> None:
        """Лениво создаём OpenAIServingChat поверх Engine + Models."""
        if self.openai_serving_chat is not None:
            return  # Если уже инициализировали — выходим
        model_config = await self.engine.get_model_config()  # Конфиг модели (для совместимости и валидации)
        models = await self._build_openai_models()  # Убеждаемся, что менеджер моделей существует

        self.openai_serving_chat = OpenAIServingChat(
            engine_client=self.engine,  # Асинхронный клиент движка
            model_config=model_config,  # Конфигурация модели
            models=models,  # Менеджер моделей (псевдонимы, LoRA и т.д.)
            response_role=self.response_role,  # Роль сообщений по умолчанию ("assistant")
            request_logger=None,  # Логгер запросов не используем (можно внедрить при необходимости)
            chat_template=self.chat_template,  # Пользовательский шаблон форматирования чата (если задан)
            chat_template_content_format="auto",  # Авто-детект формата шаблона
        )
        logger.info("OpenAIServingChat initialized")  # Подтверждаем готовность чат-обработчика

    # -------------------------
    # Аутентификация
    # -------------------------
    @app.post("/token", response_model=TokenResponse)  # Точка получения JWT по логину/паролю
    async def login_for_access_token(
            self,
            form_data: OAuth2PasswordRequestForm = Depends()  # Зависимость: форма OAuth2 (username/password)
    ) -> TokenResponse:
        user = authenticate_user(form_data.username, form_data.password)  # Проверяем учетные данные
        if not user:
            raise HTTPException(  # Если аутентификация неуспешна — 401
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password"
            )
        expire_minutes = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))  # TTL токена из ENV
        expire = timedelta(minutes=expire_minutes)  # Длительность жизни токена
        token_data = {"sub": user["username"], "role": user["role"]}  # Полезная нагрузка для JWT
        access_token = create_access_token(token_data, expire)  # Подписываем JWT
        return {"access_token": access_token, "token_type": "bearer"}  # Возвращаем стандартную форму ответа

    # -------------------------
    # OpenAI совместимые endpoint
    # -------------------------
    @app.post("/v1/tasks/auto/completions")
    async def auto_completions(
            self,
            request_body: ChatCompletionRequest,  # Запрос в формате OpenAI Chat Completions
            raw_request: Request,  # Оригинальный Request (может использоваться для логирования/метрик)
            credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer())  # Извлекаем Bearer токен из заголовка
    ) -> Any:
        payload = verify_jwt_token(credentials)  # Декодируем/проверяем JWT
        check_role(payload, "admin")  # Ограничиваем доступ по роли ("admin")
        logger.info(f"/v1/tasks/auto/completions by user={payload.get('sub')}")  # Логируем инициатора
        try:
            await self._initialize_serving_chat()  # Лениво инициализируем OpenAIServingChat
            generator = await self.openai_serving_chat.create_chat_completion(request_body, raw_request)  # Создаем комплишн

            if isinstance(generator, ErrorResponse):  # Если вернулась ошибка vLLM OpenAI-совместимого формата
                return JSONResponse(content=generator.model_dump(), status_code=generator.code)  # Отдаем как есть

            if request_body.stream:  # Если клиент запросил stream-ответ (SSE)
                return StreamingResponse(generator, media_type="text/event-stream")  # Проксируем генератор в SSE

            assert isinstance(generator, ChatCompletionResponse)  # В неблокирующем режиме — это финальный объект ответа
            choices = [{  # Приводим к упрощенному формату (совместимо с OpenAI объектом choices)
                "index": c.index,
                "message": {"role": c.message.role, "content": c.message.content},
                "finish_reason": c.finish_reason,
            } for c in generator.choices]

            return JSONResponse(content={  # Собираем финальный JSON-ответ
                "id": f"chatcmpl-{datetime.now().timestamp()}",  # Генерируем уникальный id ответа
                "object": "chat.completion",  # Тип объекта OpenAI
                "created": int(datetime.now().timestamp()),  # Временная метка создания
                "model": (self.model_name or self.engine_args.model),  # Имя опубликованной модели
                "choices": choices,  # Список вариантов ответа
                "usage": {  # Метрики токенов
                    "prompt_tokens": generator.usage.prompt_tokens,
                    "completion_tokens": generator.usage.completion_tokens,
                    "total_tokens": generator.usage.total_tokens,
                },
            })
        except Exception as e:
            logger.error(f"Error in auto completions: {e}", exc_info=True)  # Логируем стек при ошибке
            raise HTTPException(status_code=500, detail=str(e))  # Возвращаем 500 в случае исключения

    @app.post("/v1/chat/completions")  # Классический OpenAI-совместимый маршрут chat.completions
    async def create_chat_completion(
            self,
            request: ChatCompletionRequest,  # Входной запрос OpenAI формата
            raw_request: Request,  # Низкоуровневый Request (для логирования и пр.)
            credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer())  # Авторизация Bearer-токеном
    ) -> Any:
        payload = verify_jwt_token(credentials)  # Проверяем и декодируем JWT
        check_role(payload, "admin")  # Проверяем, что роль имеет доступ
        logger.info(f"/v1/chat/completions by user={payload.get('sub')} role={payload.get('role')}")  # Логируем контекст
        try:
            await self._initialize_serving_chat()  # Инициализация OpenAIServingChat по требованию
            generator = await self.openai_serving_chat.create_chat_completion(request, raw_request)  # Запускаем генерацию

            if isinstance(generator, ErrorResponse):  # Обработка ошибок OpenAI-совместимого уровня
                return JSONResponse(content=generator.model_dump(), status_code=generator.code)  # Возвращаем ошибку как есть

            if request.stream:  # Если запрошен потоковый ответ
                return StreamingResponse(generator, media_type="text/event-stream")  # SSE-стрим без буферизации

            assert isinstance(generator, ChatCompletionResponse)  # В неблокирующем режиме ожидаем финальный объект
            choices = [{  # Преобразуем choices к простому словарю
                "index": c.index,
                "message": {"role": c.message.role, "content": c.message.content},
                "finish_reason": c.finish_reason,
            } for c in generator.choices]

            return JSONResponse(content={  # Возвращаем OpenAI-совместимое тело ответа
                "id": f"chatcmpl-{datetime.now().timestamp()}",
                "object": "chat.completion",
                "created": int(datetime.now().timestamp()),
                "model": (self.model_name or self.engine_args.model),
                "choices": choices,
                "usage": {
                    "prompt_tokens": generator.usage.prompt_tokens,
                    "completion_tokens": generator.usage.completion_tokens,
                    "total_tokens": generator.usage.total_tokens,
                },
            })
        except Exception as e:
            logger.error(f"Error in chat completion: {str(e)}", exc_info=True)  # Логируем исключение с трассировкой
            return JSONResponse(content={"error": str(e)}, status_code=500)  # Возвращаем 500-ошибку в JSON

    @app.get("/v1/models")  # OpenAI-совместимый список моделей (дополняет/дублирует router от OpenAIServingModels)
    async def get_models(
            self,
            credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer())  # Проверка Bearer-токена
    ) -> Any:
        payload = verify_jwt_token(credentials)  # Верификация JWT
        logger.info(f"/v1/models by user={payload.get('sub')} role={payload.get('role')}")  # Логируем запрос
        # /v1/models уже отдаёт OpenAIServingModels.router, но оставим простой ответ совместимый с твоей логикой
        active_name = self.model_name or self.engine_args.model  # Определяем активное имя модели
        if not active_name:
            return JSONResponse(content={"object": "list", "data": []})  # Если имя не задано — пустой список
        return JSONResponse(content={  # Возвращаем список из одной модели
            "object": "list",
            "data": [
                {"id": active_name, "object": "model", "owned_by": "owner", "permission": []}
            ]
        })


# -------------------------
# Сборка Serve приложения на драйвере (без инициализации vLLM)
# -------------------------
def build_app(cli_args: Dict[str, Any]) -> serve.Application:
    os.environ.setdefault("VLLM_ATTENTION_BACKEND", "FLASH_ATTN_VLLM_V1")  # Значение по умолчанию для бекенда внимания
    logger.info("Building Serve application (driver-side), vLLM init will happen on actor")  # Сообщаем, что инициализация vLLM будет на акторе
    return VLLMDeployment.bind(  # Формируем граф Ray Serve без немедленной инициализации движка
        cli_args=cli_args,  # Передаем конфигурацию CLI для vLLM
        chat_template=os.environ.get("CHAT_TEMPLATE"),  # Пробрасываем шаблон чата из ENV (если задан)
    )


# -------------------------
# Конфиг из ENV → CLI vLLM
# -------------------------
def _get_bool_env(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)  # Достаем значение переменной окружения
    if v is None:
        return default  # Если не задана — возвращаем значение по умолчанию
    return v.strip().lower() in ("1", "true", "yes", "y", "on")  # Нормализуем строку в булево

def _required_env(name: str) -> str:
    if name not in os.environ:
        raise RuntimeError(f"Environment variable {name} is required")  # Бросаем исключение, если переменная обязательна
    return os.environ[name]  # Возвращаем значение переменной окружения

config: Dict[str, Any] = {
    # Базовые
    "model": _required_env("MODEL_ID"),  # Идентификатор/путь модели (обязателен)
    "model_name": os.environ.get("MODEL_NAME"),  # Публикуемое имя модели в /v1/models (опционально)
    "served-model-name": os.environ.get("SERVED_MODEL_NAME", os.environ.get("MODEL_NAME")),  # Альтернативное(ые) имя(мена) сервинга
    # device можно не указывать — vLLM сам определит; если надо, добавь VLLM_DEVICE
    "device": os.environ.get("VLLM_DEVICE", None),  # Принудительный выбор устройства ("cuda", "cpu", "rocm") при необходимости

    # Параллелизм
    "tensor-parallel-size": int(os.environ.get("TENSOR_PARALLELISM", "1")),  # Тензорный параллелизм (TP)
    "pipeline-parallel-size": int(os.environ.get("PIPELINE_PARALLELISM", "1")),  # Параллелизм по конвейеру (PP)

    # Память/DTYPE/KV-cache
    "gpu-memory-utilization": os.environ.get("GPU_MEMORY_UTIL", "0.97"),  # Доля GPU-памяти, доступной движку
    "dtype": os.environ.get("DTYPE", "auto"),  # Тип тензоров (auto/bfloat16/float16/float32 и т.д.)
    "kv-cache-dtype": os.environ.get("KV_CACHE_DTYPE", "auto"),  # Тип KV-кеша (влияет на память/скорость)

    # Размерности и батчинг
    "max-model-len": int(os.environ.get("MAX_MODEL_LEN", "4096")),  # Максимальная длина контекста модели
    "max-num-seqs": int(os.environ.get("MAX_NUM_SEQS", "128")),  # Верхняя граница одновременных последовательностей
    "max-num-batched-tokens": int(os.environ.get("MAX_NUM_BATCHED_TOKENS", "4096")),  # Лимит токенов в батче
    "max-seq-len-to-capture": int(os.environ.get("MAX_SEQ_LEN_TO_CAPTURE", "8192")),  # Порог для CUDA графов/захвата

    # Поведение
    "enable-chunked-prefill": _get_bool_env("ENABLE_CHUNKED_PREFILL", False),  # Разрешить чанковый префилл
    "enforce-eager": _get_bool_env("ENABLE_ENFORCE_EAGER", False),  # Включить eager-режим (для отладки/совместимости)

    # Swap/Offload
    "swap-space": int(os.environ.get("SWAP_SPACE", "4")),  # Размер swap-пространства (ГБ) для offload
    "cpu-offload-gb": os.environ.get("CPU_OFFLOAD_GB", None),  # Принудительный offload на CPU (ГБ), если задан
}

# Создаём Serve граф
model = build_app(config)  # Биндим деплой и возвращаем Application — точка входа Ray Serve (serve run python_file:model)