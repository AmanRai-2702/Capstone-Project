from repositories.user_repository import UserRepository


class AuthService:
    def __init__(self):
        self.users = UserRepository()

    def authenticate(self, username, password):
        user = self.users.find_by_username(username)
        if user and user["password"] == password:
            return user
        return None

    def get_user(self, user_id):
        return self.users.find_by_id(user_id)