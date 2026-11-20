from pathlib import PurePosixPath


def _norm(path: str) -> str:
    """'./a\\b.py' -> 'a/b.py' (always forward slashes, no leading ./)"""
    return PurePosixPath(path.replace("\\", "/")).as_posix()


def make_entity_id(language: str, file_path: str, qualified_name: str) -> str:
    """e.g. python:services/auth_service.py:AuthService.authenticate"""
    return f"{language}:{_norm(file_path)}:{qualified_name}"


def make_file_id(file_path: str) -> str:
    return f"file:{_norm(file_path)}"


def make_endpoint_id(method: str, route: str) -> str:
    """Endpoints belong to no single language: api:POST:/api/login"""
    return f"api:{method.upper()}:{route}"