def start_order():
    user = load_user()
    return build_response(user)


def load_user():
    record = read_database()
    return normalize_user(record)


def read_database():
    return {"name": "Ada"}


def normalize_user(record):
    return {"name": record["name"].strip()}


def build_response(user):
    return render_profile(user)


def render_profile(user):
    return f"Profile: {user['name']}"


def retry_loop():
    return retry_loop()


def diamond_root():
    left_branch()
    right_branch()


def left_branch():
    shared_leaf()


def right_branch():
    shared_leaf()


def shared_leaf():
    return "shared"
