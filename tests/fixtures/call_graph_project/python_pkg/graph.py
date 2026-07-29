from .helpers import imported_target


def entry():
    alpha()
    beta()
    imported_target()


def alpha():
    gamma()


def beta():
    gamma()


def gamma():
    return "done"


def recursive():
    recursive()


def mutual_a():
    mutual_b()


def mutual_b():
    mutual_a()


def weak_entry():
    weak_target()  # noqa: F821


class Worker:
    def run(self):
        self.member()

    def member(self):
        return "member"


def module_call_target():
    return "module"


module_call_target()
