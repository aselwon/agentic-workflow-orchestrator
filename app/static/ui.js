document.addEventListener("htmx:responseError", (event) => {
  const box = document.getElementById("request-error");
  box.textContent = `Request failed (${event.detail.xhr.status}). Refresh the run to check its latest state.`;
  box.hidden = false;
});
document.addEventListener("htmx:sendError", () => {
  const box = document.getElementById("request-error");
  box.textContent = "Connection lost. Check the server and refresh to reconnect.";
  box.hidden = false;
});
