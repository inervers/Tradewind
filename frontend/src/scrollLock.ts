const bodyLocks = new WeakMap<HTMLElement, { count: number; overflow: string }>();

/** Nested dialogs may unmount in either order; restore only after the last lock. */
export function acquireBodyScrollLock(): () => void {
  const body = document.body;
  const lock = bodyLocks.get(body) || { count: 0, overflow: body.style.overflow };
  bodyLocks.set(body, lock);
  lock.count++;
  body.style.overflow = "hidden";
  let released = false;
  return () => {
    if (released) return;
    released = true;
    if (--lock.count === 0) {
      body.style.overflow = lock.overflow;
      bodyLocks.delete(body);
    }
  };
}
