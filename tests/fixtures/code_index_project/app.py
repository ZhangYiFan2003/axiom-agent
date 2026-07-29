class Greeter:
    def __init__(self, name: str):
        self.name = name

    def greet(self) -> str:
        return f"hello {self.name}"


async def load_user(user_id: str) -> dict[str, str]:
    return {"id": user_id}


def load_user_config(config_path: str) -> dict[str, str]:
    return {"path": config_path}


def 用户权限校验(user_id: str) -> bool:
    """用户权限校验。"""
    return bool(user_id)
