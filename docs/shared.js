// === Scroll reveal ===
const revealObs = new IntersectionObserver((entries) => {
  entries.forEach(e => {
    if (!e.isIntersecting) return;
    e.target.classList.add('visible');
    // Stagger compare-card list items
    e.target.querySelectorAll('.compare-card li').forEach((li, i) =>
      setTimeout(() => li.classList.add('typed'), i * 150)
    );
    revealObs.unobserve(e.target);
  });
}, { threshold: 0.12 });
document.querySelectorAll('.reveal').forEach(el => revealObs.observe(el));

// === Terminal typing engine ===
const CHAR_MS = 35;
const LINE_MS = 80;
const CMD_PAUSE = 350;
const GROUP_PAUSE = 250;
const LOOP_HOLD = 10000;
const LOOP_FADE = 600;
const LOOP_GAP = 800;

const typingObs = new IntersectionObserver((entries) => {
  entries.forEach(e => {
    if (!e.isIntersecting) return;
    startTerminal(e.target);
    typingObs.unobserve(e.target);
  });
}, { threshold: 0.15 });
document.querySelectorAll('[data-typing]').forEach(el => typingObs.observe(el));

function stopTerminal(terminal) {
  // Bump generation so all pending timeouts become no-ops
  terminal._gen = (terminal._gen || 0) + 1;
}

function startTerminal(terminal) {
  stopTerminal(terminal);
  const body = terminal.querySelector('.terminal-body');
  if (!body) return;
  if (!terminal._origHTML) terminal._origHTML = body.innerHTML;
  body.innerHTML = terminal._origHTML;
  body.classList.remove('fade-out');
  runTerminal(terminal, terminal._gen);
}

function runTerminal(terminal, gen) {
  const body = terminal.querySelector('.terminal-body');
  const lines = body.querySelectorAll('.tl');
  if (!lines.length) return;

  // Guard: if generation changed, abort
  const guard = () => terminal._gen === gen;

  const cursor = document.createElement('span');
  cursor.className = 'tcur';
  let delay = 200;

  lines.forEach((line, idx) => {
    const cmdText = line.dataset.cmd;
    if (cmdText !== undefined) {
      const cmdSpan = line.querySelector('.cmd');
      if (!cmdSpan) return;
      const showAt = delay;
      setTimeout(() => { if (!guard()) return; line.classList.add('show'); cmdSpan.after(cursor); }, showAt);
      delay += 120;
      for (let c = 0; c < cmdText.length; c++) {
        const ch = cmdText[c];
        setTimeout(() => { if (!guard()) return; cmdSpan.textContent += ch; }, delay + c * CHAR_MS);
      }
      delay += cmdText.length * CHAR_MS + CMD_PAUSE;
    } else {
      setTimeout(() => { if (!guard()) return; line.classList.add('show'); }, delay);
      delay += LINE_MS;
    }
    if (idx < lines.length - 1 && lines[idx + 1].dataset.cmd !== undefined && cmdText === undefined) {
      delay += GROUP_PAUSE;
    }
  });

  const totalDuration = delay;
  setTimeout(() => { if (!guard()) return; cursor.remove(); }, totalDuration + 100);

  if (terminal.hasAttribute('data-loop')) {
    setTimeout(() => {
      if (!guard()) return;
      body.classList.add('fade-out');
      setTimeout(() => {
        if (!guard()) return;
        body.innerHTML = terminal._origHTML;
        body.classList.remove('fade-out');
        runTerminal(terminal, gen);
      }, LOOP_FADE + LOOP_GAP);
    }, totalDuration + LOOP_HOLD);
  }
}

// === Tabs ===
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    const group = tab.closest('.tab-group');
    // Stop all terminals in this tab-group before switching
    group.querySelectorAll('[data-typing]').forEach(t => stopTerminal(t));
    group.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    group.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    const target = group.querySelector(`#${tab.dataset.target}`);
    target.classList.add('active');
    // Start terminals in newly active tab
    target.querySelectorAll('[data-typing]').forEach(t => startTerminal(t));
  });
});
