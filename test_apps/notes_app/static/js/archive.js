const archive = document.getElementById("archive-list");

async function loadArchive() {
  const res = await fetch("/api/notes");
  const notes = await res.json();
  archive.textContent = notes.length + " notes";
}

loadArchive();
