class Item:
    def __init__(self, ident):
        self.ident = ident


class Store:
    def __init__(self):
        self.rows = {}

    def put(self, item):
        self.rows[item.ident] = item

    def get(self, ident):
        return self.rows.get(ident)


class Session:
    store: Store

    def __init__(self, store):
        self.store = store


def make_store() -> Store:
    return Store()


def build():
    return Item(1)
