class Greeter:
    def __init__(self, name: str):
        self.name = name

    def greet(self) -> str:
        return f"hello {self.name}"


async def load_user(user_id: str) -> dict[str, str]:
    return {"id": user_id}
