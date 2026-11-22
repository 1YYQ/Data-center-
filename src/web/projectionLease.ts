/** A display deadline is independent of a stalled authorization request. */
export function createProjectionLease(expire: () => void, milliseconds = 15000) {
  let timer: ReturnType<typeof setTimeout> | null = null;
  function dispose() { if (timer !== null) clearTimeout(timer); timer = null; }
  return {
    renew(requestStarted: number) {
      dispose();
      const remaining = milliseconds - (performance.now() - requestStarted);
      if (remaining <= 0) { expire(); return false; }
      timer = setTimeout(expire, remaining);
      return true;
    },
    dispose,
    invalidate() { dispose(); expire(); },
  };
}
