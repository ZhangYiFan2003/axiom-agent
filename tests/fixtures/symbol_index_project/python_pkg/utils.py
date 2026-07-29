class Helper:
    def validate(self, value: str) -> bool:
        return bool(value)


def load_config(path: str) -> dict[str, str]:
    return {"path": path}


def duplicate(value: str) -> str:
    return value
