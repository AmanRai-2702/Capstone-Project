from models import make_store


def load():
    return [1, 2, 3]


def clean(raw):
    return [x for x in raw if x]


def save(cleaned):
    return len(cleaned)


def run():
    raw = load()
    cleaned = clean(raw)
    return save(cleaned)


def run_nested():
    return save(clean(load()))


def seed():
    st = make_store()
    st.put("x")


class Base:
    def __init__(self):
        self.cache = {}

    def helper(self):
        return 1


class Child(Base):
    def lookup(self):
        self.helper()
        return self.cache

    def reset(self):
        self.count = 0

    def bump(self):
        self.count += 1
