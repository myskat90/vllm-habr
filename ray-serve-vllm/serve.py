import os
from typing import Dict, Optional, List, Tuple, Union, Any, Tuple, Union, Any
import logging

from fastapi import FastAPI, Depends, HTTPException, status
from starlette.requests import Request
from starlette.responses import StreamingResponse, JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm, HTTPBearer, HTTPAuthorizationCredentials

from ray import serve

# ИЗ vllm
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.entrypoints.openai.serving_engine import BaseModelPath
from vllm.engine.async_llm_engine import AsyncLLMEngine
from vllm.entrypoints.openai.cli_args import make_arg_parser
from vllm.entrypoints.openai.protocol import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorResponse,
)
from vllm.entrypoints.openai.serving_chat import OpenAIServingChat
from vllm.entrypoints.openai.serving_engine import LoRAModulePath
from vllm.utils import FlexibleArgumentParser
from functools import wraps
from starlette.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta

# Наш файл с аутентификацией
from auth import (
    verify_jwt_token,
    check_role,
    create_access_token,
    authenticate_user,
    TokenResponse
)

# Логирование
logger = logging.getLogger("ray.serve")
logger.setLevel(logging.DEBUG)

# Схемы авторизации
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")
bearer_scheme = HTTPBearer()

# Инициализация FastAPI
app = FastAPI()

# Добавляем middleware для разрешений CORS, чтобы страницы могли получать данные из разных источников
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Серверный скрипт для развертывания VLLM с использованием Ray
@serve.deployment(name="VLLMDeployment")
@serve.ingress(app)
class VLLMDeployment:
    def __init__(
            self,
            engine_args: AsyncEngineArgs,
            response_role: str,
            lora_modules: Optional[List[LoRAModulePath]] = None,
            chat_template: Optional[str] = None,
            model_name: Optional[str] = None,
    ):
        logger.info(f"Starting with engine args: {engine_args}")
        self.openai_serving_chat = None
        self.engine_args = engine_args
        self.response_role = response_role
        self.lora_modules = lora_modules
        self.chat_template = chat_template
        # Создаем объект AsyncLLMEngine на основе аргументов для работы с VLLM
        self.engine = AsyncLLMEngine.from_engine_args(engine_args)
        self.model_name = model_name

    @app.post("/token", response_model=TokenResponse)
    async def login_for_access_token(
        self,
        form_data: OAuth2PasswordRequestForm = Depends()
    ):
        """
        Endpoint для получения токена.
        Принимает form-data (username, password).
        """
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
            credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    ):
        """
        OpenAI-совместимый endpoint для автоматических комплишенов (auto completions).
        Принимает JSON с полями, аналогичными ChatCompletionRequest
        и возвращает результат, совместимый с OpenAI API.
        """
        # Проверяем JWT и требуемую роль (например, "admin")
        payload = verify_jwt_token(credentials)
        check_role(payload, "admin")

        logger.info(f"/v1/tasks/auto/completions called by user={payload.get('sub')}")
        logger.debug(f"Request body: {request_body}")

        try:
            if not self.openai_serving_chat:
                model_config = await self.engine.get_model_config()
                if self.engine_args.served_model_name is not None:
                    served_model_names = self.engine_args.served_model_name
                else:
                    if self.model_name is None:
                        self.model_name = self.engine_args.model
                    served_model_names = [BaseModelPath(self.model_name, self.engine_args.model)]
                self.openai_serving_chat = OpenAIServingChat(
                    self.engine,
                    model_config,
                    base_model_paths=served_model_names,
                    response_role=self.response_role,
                    lora_modules=self.lora_modules,
                    chat_template=self.chat_template,
                    chat_template_content_format="auto",
                    prompt_adapters=None,
                    request_logger=None,
                )
            logger.info("Processing auto completions request")

            # Здесь можно добавить дополнительные параметры, если нужно.
            generator = await self.openai_serving_chat.create_chat_completion(
                request_body, raw_request
            )

            if isinstance(generator, ErrorResponse):
                logger.error(f"Error response: {generator}")
                return JSONResponse(
                    content=generator.model_dump(),
                    status_code=generator.code
                )

            if request_body.stream:
                logger.debug("Returning streaming response")
                return StreamingResponse(
                    content=generator,
                    media_type="text/event-stream"
                )
            else:
                assert isinstance(generator, ChatCompletionResponse)
                logger.debug("Returning JSON response")
                return JSONResponse(content=generator.model_dump())
        except Exception as e:
            logger.error(f"Error in auto completions: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/v1/chat/completions")
    async def create_chat_completion(
            self,
            request: ChatCompletionRequest,
            raw_request: Request,
            credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    ):
        """
        Принимает OpenAI-совместимый JSON
        Плюс заголовок Authorization: Bearer <TOKEN>.
        """
        # Проверяем токен
        payload = verify_jwt_token(credentials)
        # Проверяем роль, например 'admin'
        check_role(payload, "admin")

        logger.info(f"/v1/chat/completions called by user={payload.get('sub')}, role={payload.get('role')}")
        logger.debug(f"Request body: {request}")

        try:
            if not self.openai_serving_chat:
                model_config = await self.engine.get_model_config()
                # Определение имени served модели для OpenAI клиента
                if self.engine_args.served_model_name is not None:
                    served_model_names = self.engine_args.served_model_name
                else:
                    if self.model_name == None:
                        self.model_name = self.engine_args.model
                    served_model_names = [BaseModelPath(self.model_name, self.engine_args.model)]
                self.openai_serving_chat = OpenAIServingChat(
                    self.engine,
                    model_config,
                    base_model_paths=served_model_names,
                    response_role=self.response_role,
                    lora_modules=self.lora_modules,
                    chat_template=self.chat_template,
                    chat_template_content_format="auto",
                    prompt_adapters=None,
                    request_logger=None,
                )
            logger.info("Processing chat completion request")
            logger.debug(f"Request: {request}")
            # Получаем генератор для потоковой передачи данных
            generator = await self.openai_serving_chat.create_chat_completion(
                request, raw_request
            )

            if isinstance(generator, ErrorResponse):
                # Обработка ошибок в случае, если генератор вернул объект ErrorResponse
                logger.error(f"Error response: {generator}")
                return JSONResponse(
                    content=generator.model_dump(), status_code=generator.code
                )

            if request.stream:
                # Если запрос потоковый, возвращаем поток данных
                logger.debug("Returning streaming response")
                return StreamingResponse(content=generator, media_type="text/event-stream")
            else:
                # Если запрос не потоковый, возвращаем ответ в формате JSON
                assert isinstance(generator, ChatCompletionResponse)
                logger.debug("Returning JSON response")
                return JSONResponse(content=generator.model_dump())

        except Exception as e:
            # Обработка исключений и ошибок
            logger.error(f"Error in chat completion: {str(e)}", exc_info=True)
            return JSONResponse(
                content={"error": str(e)},
                status_code=500
            )

    # GET-запрос для получения списка доступных моделей
    @app.get("/v1/models")
    async def get_models(
        self,
        credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer())
    ):
        """
        Любая валидная JWT-аутентификация. Возвращаем список моделей.
        """
        payload = verify_jwt_token(credentials)
        logger.info(f"/v1/models called by user={payload.get('sub')} role={payload.get('role')}")
        if self.model_name is None:
            # Если имя модели не установлено, возвращаем пустой список
            return JSONResponse(content={"object": "list", "data": []})
        return JSONResponse(content={
            "object": "list",
            "data": [
                {
                    "id": self.model_name,
                    "object": "model",
                    "owned_by": "apiac.ru",
                    "permission": []
                }
            ]
        })


def parse_vllm_args(cli_args: Dict[str, str]):
    """Parses vLLM args based on CLI inputs."""
    # Аргументы, которые должны быть исключены из обработки
    exclude_args_from_engine = ["model_name"]
    # Создаем объект FlexibleArgumentParser для разбора аргументов командной строки
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



def build_app(cli_args: Dict[str, str]) -> serve.Application:
    # Разбор аргументов командной строки
    parsed_args = parse_vllm_args(cli_args)
    # Создание AsyncEngineArgs на основе аргументов командной строки
    engine_args = AsyncEngineArgs.from_cli_args(parsed_args)
    engine_args.worker_use_ray = True

    logger.info("Building Serve application")
    return VLLMDeployment.bind(
        engine_args,
        parsed_args.response_role,
        parsed_args.lora_modules,
        parsed_args.chat_template,
        cli_args["model_name"]
    )


model = build_app(
    {
        "model": os.environ['MODEL_ID'],
        "model_name": os.environ.get('MODEL_NAME', None),
        "tensor-parallel-size": os.environ['TENSOR_PARALLELISM'],
        "pipeline-parallel-size": os.environ['PIPELINE_PARALLELISM'],
        "gpu_memory_utilization": os.environ.get('GPU_MEMORY_UTIL', 0.95),
        "dtype": os.environ.get('DTYPE', 'auto'),
#         "quantization": "bitsandbytes",
#         "load-format": "bitsandbytes",
        "enable-chunked-prefill": os.environ.get('ENABLE_CHUNKED_PREFILL', 'False').strip().lower() in ['true', '1', 'yes'],
        "cpu_offload_gb": os.environ.get('CPU_OFFLOAD_GB', 0),
        "max-model-len": os.environ.get('MAX_MODEL_LEN', 4096),
        "max-model-len": int(os.environ.get('MAX_MODEL_LEN', 4096)),
        "max_num_seqs": int(os.environ.get('MAX_NUM_SEQS', 128)),  # количество последовательностей для обработки в одном батче
        "enforce-eager": os.environ.get('ENABLE_ENFORCE_EAGER', 'False').strip().lower() in ['true', '1', 'yes']  # Добавляем этот параметр, чтобы включить eager mode
    }
)