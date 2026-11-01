// Apply the saved theme before first paint. The workspace starts in light mode.
try {
  const saved = localStorage.getItem("zhixu.theme");
  document.documentElement.classList.toggle("dark", saved === "dark");
} catch { /* Keep the light default when storage is unavailable. */ }
