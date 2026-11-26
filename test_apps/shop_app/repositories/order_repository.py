class OrderRepository:
    def __init__(self):
        self.rows = []

    def save(self, order):
        self.rows.append(order)
        return order

    def list_all(self):
        return list(self.rows)

    def get(self, order_id):
        return self.rows[order_id]
