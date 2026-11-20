class BaseRepository:
    def __init__(self):
        self.rows = []
        self.counter = 0

    def _next_id(self):
        self.counter += 1
        return self.counter


class NoteRepository(BaseRepository):
    def list_all(self):
        return list(self.rows)

    def insert(self, title):
        note = {"id": self._next_id(), "title": title}
        self.rows.append(note)
        return note

    def delete(self, note_id):
        self.rows = [r for r in self.rows if r["id"] != note_id]
