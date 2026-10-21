document.querySelectorAll(".copy-id").forEach((button) => {
  const feedback = button.parentElement.querySelector(".copy-feedback");
  const runId = button.querySelector(".mono").textContent.trim();

  button.addEventListener("click", () => {
    if (!navigator.clipboard?.writeText) return;

    navigator.clipboard.writeText(runId).then(() => {
      feedback.hidden = false;
      window.setTimeout(() => {
        feedback.hidden = true;
      }, 1600);
    }).catch(() => {});
  });
});
