/** Delegated pointer feedback: one listener and at most one update per frame. */
export function attachSpotlight(root: HTMLElement): () => void {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  const fine = window.matchMedia("(hover: hover) and (pointer: fine)");
  let active: HTMLElement | null = null;
  let frame = 0;
  let x = 0;
  let y = 0;
  function reset() {
    cancelAnimationFrame(frame);
    frame = 0;
    if (active) {
      delete active.dataset.spotlightActive;
      active.style.setProperty("--tilt-x", "0deg");
      active.style.setProperty("--tilt-y", "0deg");
    }
    active = null;
  }
  function move(event: PointerEvent) {
    if (reduce.matches || !fine.matches || event.pointerType === "touch") {
      reset();
      return;
    }
    const card =
      event.target instanceof Element
        ? event.target.closest<HTMLElement>("[data-spotlight]")
        : null;
    if (!card || !root.contains(card)) {
      reset();
      return;
    }
    if (card !== active) {
      reset();
      active = card;
    }
    x = event.clientX;
    y = event.clientY;
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      if (!active || !active.isConnected || reduce.matches) return;
      const bounds = active.getBoundingClientRect();
      const px = Math.max(0, Math.min(bounds.width, x - bounds.left));
      const py = Math.max(0, Math.min(bounds.height, y - bounds.top));
      active.style.setProperty("--pointer-x", `${px}px`);
      active.style.setProperty("--pointer-y", `${py}px`);
      active.style.setProperty(
        "--tilt-x",
        `${-(py / Math.max(1, bounds.height) - 0.5) * 3}deg`,
      );
      active.style.setProperty(
        "--tilt-y",
        `${(px / Math.max(1, bounds.width) - 0.5) * 3}deg`,
      );
      active.dataset.spotlightActive = "true";
    });
  }
  root.addEventListener("pointermove", move, { passive: true });
  root.addEventListener("pointerleave", reset);
  return () => {
    reset();
    root.removeEventListener("pointermove", move);
    root.removeEventListener("pointerleave", reset);
  };
}
