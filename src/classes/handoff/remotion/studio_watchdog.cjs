// Preloaded into the Remotion Studio Zenvi starts (node --require studio_watchdog.cjs ...):
// the Studio exits when the Zenvi that started it is gone, even after a crash, so it never keeps
// serving the project on the network on its own. ZENVI_PARENT_PID names that Zenvi process.
'use strict';

const parent = Number(process.env.ZENVI_PARENT_PID || 0);
if (parent > 0 && parent !== process.pid) {
  const timer = setInterval(() => {
    try {
      process.kill(parent, 0);
    } catch (err) {
      if (err && err.code === 'ESRCH') {
        clearInterval(timer);
        process.exit(0);
      }
    }
  }, Number(process.env.ZENVI_WATCHDOG_MS || 2000));
  timer.unref();
}
