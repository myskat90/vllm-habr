import os
import hashlib
import jwt
from datetime import datetime, timedelta
from typing import Optional, Dict
from fastapi import HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

# JWT-настройки: берем из переменных окружения или используем значения по умолчанию.
JWT_KEY = os.environ.get("JWT_KEY", "default_jwt_key")
JWT_ALGORITHM = "HS256"
SKIP_EXP_CHECK = os.environ.get("SKIP_EXP_CHECK", "false").lower() in ["true", "1", "yes"]

def load_users() -> Dict[str, Dict[str, str]]:
    """
    Загружает пользователей из переменной окружения USER_LIST.
    Для каждого псевдонима (ALICE, BOB и т.д.) ожидаются:
        ALICE_USERNAME, ALICE_HASHED_PASSWORD, ALICE_ROLE
    """
    user_list_str = os.environ.get("USER_LIST", "")
    users_db = {}
    if user_list_str:
        user_names = [u.strip().upper() for u in user_list_str.split(",") if u.strip()]
        for user_upper in user_names:
            username = os.environ.get(f"{user_upper}_USERNAME")
            hashed_password = os.environ.get(f"{user_upper}_HASHED_PASSWORD")
            role = os.environ.get(f"{user_upper}_ROLE")
            if username and hashed_password and role:
                users_db[username] = {
                    "username": username,
                    "hashed_password": hashed_password,
                    "role": role
                }
    return users_db

# При импорте модуля сразу загружаем пользователей
USERS_DB = load_users()

def hash_password(plain_password: str) -> str:
    """
    Хэшируем пароль, используя JWT_KEY как «соль».
    (В реальном приложении лучше использовать соль индивидуальную для каждого пользователя)
    """
    to_hash = JWT_KEY + plain_password
    return hashlib.sha256(to_hash.encode("utf-8")).hexdigest()

def authenticate_user(username: str, plain_password: str) -> Optional[Dict[str, str]]:
    """
    Проверяем, есть ли пользователь в USERS_DB и совпадает ли хэш.
    """
    user = USERS_DB.get(username)
    if not user:
        return None
    hashed_input = hash_password(plain_password)
    if hashed_input == user["hashed_password"]:
        return user
    return None

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Создаём JWT-токен с полем exp (время жизни).
    """
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_KEY, algorithm=JWT_ALGORITHM)

def verify_jwt_token(credentials: HTTPAuthorizationCredentials) -> dict:
    """
    Проверка заголовка Authorization: Bearer <TOKEN>.
    """
    token = credentials.credentials
    try:
        options = {"verify_exp": not SKIP_EXP_CHECK}
        payload = jwt.decode(token, JWT_KEY, algorithms=[JWT_ALGORITHM], options=options)
        username = payload.get("sub")
        if username is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid token payload"
            )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired"
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials"
        )

def check_role(payload: dict, required_role: str = "admin"):
    """
    Если у пользователя нет required_role, выбрасываем HTTPException(403).
    """
    user_role = payload.get("role")
    if user_role != required_role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Required role: {required_role}, but got: {user_role}"
        )

# Pydantic-модель для ответа при успешной авторизации.
class TokenResponse(BaseModel):
    access_token: str
    token_type: str
