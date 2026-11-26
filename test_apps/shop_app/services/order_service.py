import services.pricing as pricing
from models.order import Order
from repositories.order_repository import OrderRepository


class OrderService:
    def __init__(self, repo=None):
        self.repo = repo or OrderRepository()

    def place(self, payload):
        order = Order(payload["items"])
        order.validate()
        order.total = pricing.total(order.items)
        return self.repo.save(order)

    def all(self):
        return self.repo.list_all()

    def find(self, order_id):
        repo = self.repo
        return repo.get(order_id)
