from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from services.auth_service import AuthService

app = FastAPI()
auth_service = AuthService()


class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.post("/api/login")
def login(body: LoginRequest):
    user = auth_service.authenticate(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"message": f"Welcome, {user['username']}!"}


@app.get("/api/users/{user_id}")
def get_user(user_id: int):
    user = auth_service.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return {"message": f"Profile: {user['username']}"}


app.mount("/static", StaticFiles(directory="static"), name="static")