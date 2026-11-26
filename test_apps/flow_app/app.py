from fastapi import FastAPI

from models import Session, Store


def create_app():
    app = FastAPI()
    current = Session(Store())

    def session(rid: str) -> Session:
        return current

    @app.post("/put/{rid}")
    def put(rid: str):
        return session(rid).store.put(rid)

    @app.get("/get/{rid}")
    def get(rid: str):
        s = session(rid)
        return s.store.get(rid)

    return app


app = create_app()
