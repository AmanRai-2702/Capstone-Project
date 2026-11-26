from fastapi import FastAPI

app = FastAPI()


@app.get("/api/stats")
def stats():
    return {"visits": 1}


@app.post("/api/items")
def add_item(item: dict):
    return item


@app.delete("/api/items/{item_id}")
def delete_item(item_id: int):
    return {"deleted": item_id}
