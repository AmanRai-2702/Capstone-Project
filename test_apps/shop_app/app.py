from flask import Flask, jsonify, request

from services.order_service import OrderService

app = Flask(__name__)
service = OrderService()


@app.route("/api/orders", methods=["GET", "POST"])
def orders():
    if request.method == "POST":
        return jsonify(service.place(request.get_json()))
    return jsonify(service.all())


@app.get("/api/orders/<int:order_id>")
def get_order(order_id):
    return jsonify(service.find(order_id))
