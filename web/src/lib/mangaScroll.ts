/** One lifetime for viewer input and deferred positioning. Geometry stays in the viewer. */
export function createMangaScroll(options: {
  horizontal: () => boolean;
  readingSign: () => number;
  manual: () => void;
  dragEnd: (el: HTMLElement) => void;
}) {
  let element: HTMLElement | null = null;
  let originalSnap = '';
  let generation = 0;
  let pending = 0, wheelFrame = 0, lastFrame = 0;
  let drag: { x: number; scroll: number; moved: boolean } | null = null;
  const frames = new Set<number>();
  const timers = new Set<ReturnType<typeof setTimeout>>();
  const interactive = 'button, input, textarea, select, a, video, [contenteditable], .ov-tags';
  const navigationKeys = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End'];

  function cancelDeferred() {
    generation++;
    for (const id of frames) cancelAnimationFrame(id);
    for (const id of timers) clearTimeout(id);
    frames.clear(); timers.clear();
  }
  function stopWheel() {
    cancelAnimationFrame(wheelFrame);
    pending = wheelFrame = lastFrame = 0;
  }
  function restoreSnap() { if (element) element.style.scrollSnapType = originalSnap; }
  function takeover(snap: boolean) {
    cancelDeferred(); stopWheel(); drag = null;
    if (snap) restoreSnap();
  }
  function manual(snap = false) { takeover(snap); options.manual(); }
  function frame(callback: () => void) {
    const epoch = generation;
    const id = requestAnimationFrame(() => {
      frames.delete(id);
      if (element && epoch === generation) callback();
    });
    frames.add(id);
  }
  function later(callback: () => void, ms: number) {
    const epoch = generation;
    const id = setTimeout(() => {
      timers.delete(id);
      if (element && epoch === generation) callback();
    }, ms);
    timers.add(id);
  }
  function animate(now: number) {
    wheelFrame = 0;
    const el = element;
    if (!el) return;
    const dt = lastFrame ? Math.min(now - lastFrame, 32) : 16;
    lastFrame = now;
    const step = Math.sign(pending) * Math.min(Math.abs(pending),
      Math.max(1, Math.abs(pending) * (1 - Math.exp(-dt / 45))), el.clientWidth / 8);
    const before = el.scrollLeft;
    el.scrollTo({ left: before + step, behavior: 'instant' });
    const moved = el.scrollLeft - before;
    pending -= moved;
    if (!moved || Math.abs(pending) < 0.5 || Math.sign(pending) !== Math.sign(step)) {
      pending = lastFrame = 0;
      return;
    }
    wheelFrame = requestAnimationFrame(animate);
  }
  function wheel(e: WheelEvent) {
    if (e.ctrlKey || e.metaKey) {
      manual();
      // Leave zoom to the browser, without an old alignment or layout-triggered
      // mandatory snap repositioning the reader during that gesture.
      if (element && options.horizontal()) element.style.scrollSnapType = 'none';
      return;
    }
    if ((!e.deltaX && !e.deltaY) || (e.target as Element).closest(interactive)) return;
    cancelDeferred(); drag = null; options.manual();
    const el = element;
    if (!el || !options.horizontal()) { stopWheel(); return; }
    e.preventDefault();
    const axis = Math.abs(e.deltaX) > Math.abs(e.deltaY)
      ? e.deltaX : e.deltaY * (e.shiftKey ? 1 : options.readingSign());
    const distance = axis * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? el.clientWidth : 1);
    if (Math.sign(distance) !== Math.sign(pending)) pending = 0;
    pending += distance;
    // Wheel reading remains unsnapped after idle, including tiny movements.
    el.style.scrollSnapType = 'none';
    if (!wheelFrame) wheelFrame = requestAnimationFrame(animate);
  }
  function pointerDown() { manual(); }
  function touchStart() { manual(true); }
  function keyDown(e: KeyboardEvent) {
    if ((e.target as Element).closest('input, textarea, select, [contenteditable]')) return;
    if (navigationKeys.includes(e.key)) manual(true);
  }
  function mouseDown(e: MouseEvent) {
    if (!element || !options.horizontal() || e.button !== 0 || (e.target as Element).closest(interactive)) return;
    manual();
    drag = { x: e.clientX, scroll: element.scrollLeft, moved: false };
    e.preventDefault();
  }
  function mouseMove(e: MouseEvent) {
    if (!drag || !element) return;
    const dx = e.clientX - drag.x;
    if (Math.abs(dx) > 5) drag.moved = true;
    if (!drag.moved) return;
    element.style.scrollSnapType = 'none';
    const requested = drag.scroll - dx;
    element.scrollTo({ left: requested, behavior: 'instant' });
    // At a window boundary, do not bank the clamped distance for a later load
    // (or make the pointer retrace that distance before a reversal can move).
    drag.scroll += element.scrollLeft - requested;
  }
  function mouseUp() {
    const moved = drag?.moved;
    drag = null;
    if (moved && element) { restoreSnap(); options.dragEnd(element); }
  }
  // Losing focus ends input, not reading: tab switching must retain a partial pan.
  function blur() { manual(); }

  return {
    get generation() { return generation; },
    get dragging() { return drag !== null; },
    cancelDeferred,
    frame,
    later,
    navigation(callback: () => void) { takeover(true); frame(callback); },
    compensate(dx: number, dy: number, beforeMutation: number) {
      if (!element) return;
      if (dx || dy) element.scrollTo({ left: element.scrollLeft + dx, top: element.scrollTop + dy, behavior: 'instant' });
      // Include browser scroll anchoring as well as our explicit adjustment.
      if (drag) drag.scroll += element.scrollLeft - beforeMutation;
    },
    attach(el: HTMLElement) {
      element = el; originalSnap = el.style.scrollSnapType;
      el.addEventListener('wheel', wheel, { passive: false });
      el.addEventListener('touchstart', touchStart, { passive: true });
      el.addEventListener('mousedown', mouseDown);
      window.addEventListener('pointerdown', pointerDown);
      // Capture before the viewer's navigation handler schedules its new operation.
      window.addEventListener('keydown', keyDown, true);
      window.addEventListener('mousemove', mouseMove);
      window.addEventListener('mouseup', mouseUp);
      window.addEventListener('blur', blur);
      return () => {
        takeover(true); element = null;
        el.removeEventListener('wheel', wheel);
        el.removeEventListener('touchstart', touchStart);
        el.removeEventListener('mousedown', mouseDown);
        window.removeEventListener('pointerdown', pointerDown);
        window.removeEventListener('keydown', keyDown, true);
        window.removeEventListener('mousemove', mouseMove);
        window.removeEventListener('mouseup', mouseUp);
        window.removeEventListener('blur', blur);
      };
    }
  };
}
