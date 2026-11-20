USERS = [
    {"id": 1, "username": "alice", "password": "secret123"},
    {"id": 2, "username": "bob", "password": "hunter2"},
]


class UserRepository:
    def find_by_username(self, username):
        for user in USERS:
            if user["username"] == username:
                return user
        return None

    def find_by_id(self, user_id):
        for user in USERS:
            if user["id"] == user_id:
                return user
        return None