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
