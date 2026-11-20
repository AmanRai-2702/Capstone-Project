import { notify } from "./util.js";

const list = document.querySelector("#note-list");
const form = document.querySelector("#note-form");

async function loadNotes() {
  const res = await fetch("/api/notes");
  const notes = await res.json();
  render(notes);
}

function render(notes) {
  list.innerHTML = "";
  for (const note of notes) {
    const li = document.createElement("li");
    li.textContent = note.title;
    li.addEventListener("click", () => removeNote(note.id));
    list.appendChild(li);
  }
}

async function addNote(event) {
  event.preventDefault();
  const title = document.getElementById("note-title").value;
  await fetch("/api/notes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  notify("Note added");
  loadNotes();
}

async function removeNote(id) {
  await fetch(`/api/notes/${id}`, { method: "DELETE" });
  notify("Note removed");
  loadNotes();
}

form.addEventListener("submit", addNote);
loadNotes();
