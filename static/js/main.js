document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector('button[type="submit"]');
      if (button && form.enctype === "multipart/form-data") {
        button.disabled = true;
        button.textContent = "UPLOADING...";
      }
    });
  });
});
