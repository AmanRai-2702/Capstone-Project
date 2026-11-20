export function notify(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.add("visible");
}
