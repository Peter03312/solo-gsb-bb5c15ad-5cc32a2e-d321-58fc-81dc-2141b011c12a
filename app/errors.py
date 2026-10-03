"""统一的 API 错误类型与异常处理器。"""
from fastapi import Request
from fastapi.responses import JSONResponse


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}

    def to_response(self) -> JSONResponse:
        body: dict = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            body["error"]["details"] = self.details
        return JSONResponse(status_code=self.status_code, content=body)


def not_found(message: str, **details) -> APIError:
    return APIError(404, "not_found", message, details or None)


def bad_request(code: str, message: str, **details) -> APIError:
    return APIError(400, code, message, details or None)


def validation_error(message: str, **details) -> APIError:
    return APIError(422, "validation_error", message, details or None)


def conflict(message: str, **details) -> APIError:
    return APIError(409, "conflict", message, details or None)


async def api_error_handler(_: Request, exc: APIError) -> JSONResponse:
    return exc.to_response()
