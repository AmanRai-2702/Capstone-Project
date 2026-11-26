from models.base import Model


class Order(Model):
    def __init__(self, items):
        self.items = items
        self.total = 0

    def describe(self):
        self.validate()
        return f"{len(self.items)} items"
