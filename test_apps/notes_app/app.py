from flask import Flask, jsonify, request

from services.note_service import NoteService

app = Flask(__name__)
service = NoteService()


@app.route("/")
def home():
    return app.send_static_file("index.html")


@app.route("/api/notes", methods=["GET"])
def list_notes():
    return jsonify(service.all_notes())


@app.route("/api/notes", methods=["POST"])
def create_note():
    data = request.get_json()
    return jsonify(service.add(data["title"]))


@app.route("/api/notes/<int:note_id>", methods=["DELETE"])
def delete_note(note_id):
    service.remove(note_id)
    return jsonify({"ok": True})
