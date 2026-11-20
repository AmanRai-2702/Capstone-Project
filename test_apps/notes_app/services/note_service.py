from repositories.note_repository import NoteRepository


class NoteService:
    def __init__(self):
        self.repo = NoteRepository()

    def all_notes(self):
        return self.repo.list_all()

    def add(self, title):
        self._validate(title)
        return self.repo.insert(title)

    def remove(self, note_id):
        self.repo.delete(note_id)

    def _validate(self, title):
        if not title.strip():
            raise ValueError("empty title")
