import os
import logging
from typing import Dict, Optional, List, Any
from datetime import datetime, timedelta
from functools import wraps

# Настраиваем переменные окружения для V1 Engine и Pipeline Parallelism
os.environ["VLLM_USE_V1"] = "1"
os.environ["VLLM_USE_ASYNC_COMM"] = "1"  # Включаем асинхронную коммуникацию
os.environ["VLLM_USE_CUDA_GRAPH"] = "1"  # Включаем CUDA графы для оптимизации

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm, HTTPBearer, HTTPAuthorizationCredentials
from starlette.requests import Request
from starlette.responses import StreamingResponse, JSONResponse
from starlette.middleware.cors import CORSMiddleware

from ray import serve

# Импорты из vLLM (v0.8.3)
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.engine.async_llm_engine import AsyncLLMEngine
from vllm.entrypoints.openai.cli_args import make_arg_parser
from vllm.entrypoints.openai.protocol import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorResponse,
)
from vllm.entrypoints.openai.serving_chat import OpenAIServingChat
from vllm.utils import FlexibleArgumentParser

# Импорты для движка
from vllm.engine.llm_engine import LLMEngine
from vllm.engine.async_llm_engine import AsyncLLMEngine

# Попытка импортировать BaseModelPath; если импорт не проходит, определяем fallback-реализацию.
try:
    from vllm.entrypoints.openai.serving_engine import BaseModelPath
except ImportError:
    class BaseModelPath:
        def __init__(self, path: str, model: str) -> None:
            self.path = path
            self.model = model

        def is_base_model(self, model_name: str) -> bool:
            return self.path == model_name

# Импорт ваших модулей аутентификации (замените на свои реализации)
from auth import (
    verify_jwt_token,
    check_role,
    create_access_token,
    authenticate_user,
    TokenResponse
)

logger = logging.getLogger("ray.serve")
logger.setLevel(logging.DEBUG)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")
bearer_scheme = HTTPBearer()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@serve.deployment(name="VLLMDeployment")
@serve.ingress(app)
class VLLMDeployment:
    """
    Ray Serve deployment для обработки запросов к vLLM с поддержкой V1 Engine и Pipeline Parallelism.
    Формирует запрос к модели, вставляя вход пользователя в шаблон.
    """
    def __init__(
            self,
            engine_args: AsyncEngineArgs,
            response_role: str,
            lora_modules: Optional[List[Any]] = None,
            chat_template: Optional[str] = None,
            model_name: Optional[str] = None,
    ) -> None:
        logger.info(f"Starting with engine args: {engine_args}")
        self.engine_args = engine_args

        # Настраиваем параметры для V1 Engine и Pipeline Parallelism
        if hasattr(engine_args, "pipeline_parallel_size") and engine_args.pipeline_parallel_size > 1:
            # Настройки для Pipeline Parallelism
            engine_args.worker_use_ray = True
            engine_args.distributed_executor_backend = "ray"
            engine_args.enable_cuda_graph = True  # Включаем CUDA графы
            engine_args.async_comm = True  # Включаем асинхронную коммуникацию

            # Оптимизации для V1 Engine
            engine_args.use_v1 = True
            engine_args.enable_chunked_prefill = True  # Включаем чанкированный префилл
            engine_args.max_num_batched_tokens = 4096  # Увеличиваем размер батча

            logger.info(f"Configured for V1 Engine and Pipeline Parallelism with size {engine_args.pipeline_parallel_size}")

        self.response_role = response_role
        self.lora_modules = lora_modules
        self.chat_template = chat_template
        self.model_name = model_name

        # Инициализация движка
        self.engine = AsyncLLMEngine.from_engine_args(engine_args)

        self.openai_serving_chat = None  # Ленивое создание экземпляра OpenAIServingChat
        logger.info("Engine initialized successfully with V1 and Pipeline Parallelism")

    def _get_models(self) -> BaseModelPath:
        """
        Формирует объект models для OpenAIServingChat.
        Если engine_args.served_model_name задан, оборачивает его в BaseModelPath (при необходимости);
        иначе использует self.model_name (или self.engine_args.model).
        """
        if self.engine_args.served_model_name is not None:
            models = self.engine_args.served_model_name
            if isinstance(models, str):
                models = BaseModelPath(models, self.engine_args.model)
        else:
            if self.model_name is None:
                self.model_name = self.engine_args.model
            models = BaseModelPath(self.model_name, self.engine_args.model)
        return models

    def _build_prompt(self, request_body: ChatCompletionRequest) -> ChatCompletionRequest:
        """
        Если задан chat_template, форматирует поле запроса (ожидается наличие 'prompt')
        с подстановкой пользовательского ввода.
        Предполагается, что ChatCompletionRequest имеет атрибут 'prompt'.
        """
        if self.chat_template and hasattr(request_body, "prompt") and request_body.prompt:
            # Создаем копию запроса с форматированным prompt.
            formatted_prompt = self.chat_template.format(input=request_body.prompt)
            # Поскольку ChatCompletionRequest — Pydantic-модель, используем .copy(update={...})
            return request_body.copy(update={"prompt": formatted_prompt})
        return request_body

    async def _initialize_serving_chat(self):
        """Инициализирует OpenAIServingChat с поддержкой V1 Engine."""
        if not self.openai_serving_chat:
            model_config = await self.engine.get_model_config()
            models = self._get_models()
            self.openai_serving_chat = OpenAIServingChat(
                self.engine,
                model_config,
                models,
                self.response_role,
                request_logger=None,
                chat_template=self.chat_template,
                chat_template_content_format="auto"
            )
            logger.info("OpenAIServingChat initialized with V1 Engine")

    @app.post("/token", response_model=TokenResponse)
    async def login_for_access_token(
            self,
            form_data: OAuth2PasswordRequestForm = Depends()
    ) -> TokenResponse:
        user = authenticate_user(form_data.username, form_data.password)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password"
            )
        expire_minutes = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
        expire = timedelta(minutes=expire_minutes)
        token_data = {"sub": user["username"], "role": user["role"]}
        access_token = create_access_token(token_data, expire)
        return {"access_token": access_token, "token_type": "bearer"}

    @app.post("/v1/tasks/auto/completions")
    async def auto_completions(
            self,
            request_body: ChatCompletionRequest,
            raw_request: Request,
            credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer())
    ) -> Any:
        payload = verify_jwt_token(credentials)
        check_role(payload, "admin")
        logger.info(f"/v1/tasks/auto/completions called by user={payload.get('sub')}")
        try:
            # Форматируем запрос с использованием chat_template.
            formatted_request = self._build_prompt(request_body)

            # Инициализируем serving chat если нужно
            await self._initialize_serving_chat()

            logger.info("Processing auto completions request with V1 Engine")
            generator = await self.openai_serving_chat.create_chat_completion(formatted_request, raw_request)

            if isinstance(generator, ErrorResponse):
                logger.error(f"Error response: {generator}")
                return JSONResponse(content=generator.model_dump(), status_code=generator.code)

            if request_body.stream:
                logger.debug("Returning streaming response")
                return StreamingResponse(generator, media_type="text/event-stream")

            assert isinstance(generator, ChatCompletionResponse)

            # Преобразуем ответ в словарь для сериализации
            choices = []
            for choice in generator.choices:
                choices.append({
                    "index": choice.index,
                    "message": {
                        "role": choice.message.role,
                        "content": choice.message.content
                    },
                    "finish_reason": choice.finish_reason
                })

            # Форматируем ответ в соответствии с OpenAI API
            response_data = {
                "id": f"chatcmpl-{datetime.now().timestamp()}",
                "object": "chat.completion",
                "created": int(datetime.now().timestamp()),
                "model": self.model_name or self.engine_args.model,
                "choices": choices,
                "usage": {
                    "prompt_tokens": generator.usage.prompt_tokens,
                    "completion_tokens": generator.usage.completion_tokens,
                    "total_tokens": generator.usage.total_tokens
                }
            }

            logger.debug("Returning JSON response with OpenAI format")
            return JSONResponse(content=response_data)
        except Exception as e:
            logger.error(f"Error in auto completions: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/v1/chat/completions")
    async def create_chat_completion(
            self,
            request: ChatCompletionRequest,
            raw_request: Request,
            credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer())
    ) -> Any:
        payload = verify_jwt_token(credentials)
        check_role(payload, "admin")
        logger.info(f"/v1/chat/completions called by user={payload.get('sub')}, role={payload.get('role')}")
        try:
            # Форматируем входной запрос.
            formatted_request = self._build_prompt(request)

            # Инициализируем serving chat если нужно
            await self._initialize_serving_chat()

            logger.info("Processing chat completion request with V1 Engine")
            generator = await self.openai_serving_chat.create_chat_completion(formatted_request, raw_request)

            if isinstance(generator, ErrorResponse):
                logger.error(f"Error response: {generator}")
                return JSONResponse(content=generator.model_dump(), status_code=generator.code)

            if request.stream:
                logger.debug("Returning streaming response")
                return StreamingResponse(generator, media_type="text/event-stream")

            assert isinstance(generator, ChatCompletionResponse)

            # Преобразуем ответ в словарь для сериализации
            choices = []
            for choice in generator.choices:
                choices.append({
                    "index": choice.index,
                    "message": {
                        "role": choice.message.role,
                        "content": choice.message.content
                    },
                    "finish_reason": choice.finish_reason
                })

            # Форматируем ответ в соответствии с OpenAI API
            response_data = {
                "id": f"chatcmpl-{datetime.now().timestamp()}",
                "object": "chat.completion",
                "created": int(datetime.now().timestamp()),
                "model": self.model_name or self.engine_args.model,
                "choices": choices,
                "usage": {
                    "prompt_tokens": generator.usage.prompt_tokens,
                    "completion_tokens": generator.usage.completion_tokens,
                    "total_tokens": generator.usage.total_tokens
                }
            }

            logger.debug("Returning JSON response with OpenAI format")
            return JSONResponse(content=response_data)
        except Exception as e:
            logger.error(f"Error in chat completion: {str(e)}", exc_info=True)
            return JSONResponse(content={"error": str(e)}, status_code=500)

    @app.get("/v1/models")
    async def get_models(
            self,
            credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer())
    ) -> Any:
        payload = verify_jwt_token(credentials)
        logger.info(f"/v1/models called by user={payload.get('sub')} role={payload.get('role')}")
        if self.model_name is None:
            return JSONResponse(content={"object": "list", "data": []})
        return JSONResponse(content={
            "object": "list",
            "data": [
                {"id": self.model_name, "object": "model", "owned_by": "apiac.ru", "permission": []}
            ]
        })

def parse_vllm_args(cli_args: Dict[str, Any]) -> Any:
    """Разбирает CLI-аргументы с использованием FlexibleArgumentParser."""
    exclude_args_from_engine = ["model_name", "chat_template"]
    parser = FlexibleArgumentParser(description="vLLM CLI")
    parser = make_arg_parser(parser)
    arg_strings = []
    for key, value in cli_args.items():
        if key in exclude_args_from_engine:
            continue
        if isinstance(value, list):
            for v in value:
                arg_strings.extend([f"--{key}", str(v)])
        elif isinstance(value, bool):
            if value:
                arg_strings.extend([f"--{key}"])
        else:
            arg_strings.extend([f"--{key}", str(value)])
    logger.info(f"Parsing CLI args: {arg_strings}")
    parsed_args = parser.parse_args(args=arg_strings)
    return parsed_args

def build_app(cli_args: Dict[str, Any]) -> serve.Application:
    """
    Собирает конфигурацию для Ray Serve приложения.
    Параметры model_name и chat_template не передаются напрямую в AsyncEngineArgs.
    Приоритет отдается значению из CLI-аргументов, если оно задано, иначе берется из окружения.
    """
    parsed_args = parse_vllm_args(cli_args)
    engine_args = AsyncEngineArgs.from_cli_args(parsed_args)
    engine_args.worker_use_ray = True

    # Устанавливаем chat_template: приоритет отдается CLI-значению, иначе берется из переменной окружения.
    chat_template = parsed_args.chat_template or os.environ.get("CHAT_TEMPLATE", None)
    logger.info("Building Serve application")
    logger.info(f"model_name={cli_args.get('model_name')}, chat_template={'[DEFINED]' if chat_template else 'None'}")

    return VLLMDeployment.bind(
        engine_args,
        parsed_args.response_role,
        parsed_args.lora_modules,
        chat_template,
        cli_args["model_name"]
    )

# Собираем конфигурацию из переменных окружения в одном месте
config = {
    "model": os.environ["MODEL_ID"],
    "model_name": os.environ.get("MODEL_NAME"),
    "tensor-parallel-size": os.environ["TENSOR_PARALLELISM"],
    "pipeline-parallel-size": os.environ["PIPELINE_PARALLELISM"],
    "gpu_memory_utilization": os.environ.get("GPU_MEMORY_UTIL", 0.97),
    "dtype": os.environ.get("DTYPE", "auto"),
    "enable-chunked-prefill": os.environ.get("ENABLE_CHUNKED_PREFILL", "False").strip().lower() in ["true", "1", "yes"],
    "cpu_offload_gb": os.environ.get("CPU_OFFLOAD_GB", 0),
    "max-model-len": int(os.environ.get("MAX_MODEL_LEN", 4096)),
    "max_num_seqs": int(os.environ.get("MAX_NUM_SEQS", 128)),
    "enforce-eager": os.environ.get("ENABLE_ENFORCE_EAGER", "False").strip().lower() in ["true", "1", "yes"]
}

model = build_app(config)