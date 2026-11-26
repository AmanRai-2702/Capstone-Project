TAX = 0.2


def with_tax(amount):
    return amount * (1 + TAX)


def total(items):
    return sum(with_tax(i["price"]) for i in items)
