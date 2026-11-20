const form = document.getElementById("login-form");
const messageEl = document.querySelector("#message");

function showMessage(text, isError) {
  messageEl.textContent = text;
  messageEl.classList.toggle("error", isError);
}

async function handleLogin(event) {
  event.preventDefault();
  const username = document.getElementById("username").value;
  const password = document.getElementById("password").value;
  const response = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const data = await response.json();
  if (response.ok) {
    showMessage(data.message, false);
  } else {
    showMessage(data.detail, true);
  }
}

async function loadProfile(userId) {
  const response = await fetch(`/api/users/${userId}`);
  const data = await response.json();
  showMessage(data.message || data.detail, !response.ok);
}

form.addEventListener("submit", handleLogin);
document
  .getElementById("profile-btn")
  .addEventListener("click", () => loadProfile(1));