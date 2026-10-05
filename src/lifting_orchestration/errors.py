"""储运与提油编排服务向 API 和 CLI 暴露的稳定错误。"""


class LiftingError(RuntimeError):
    code = "lifting_error"
    status = 400


class NotFound(LiftingError):
    code = "not_found"
    status = 404


class Conflict(LiftingError):
    code = "conflict"
    status = 409


class Forbidden(LiftingError):
    code = "forbidden"
    status = 403


class InvalidState(LiftingError):
    code = "invalid_state"
    status = 409


class ValidationFailed(LiftingError):
    code = "validation_failed"
    status = 422
