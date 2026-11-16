(function () {
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Stagger each section's parts, then reveal them once on entry.
  document.querySelectorAll('section, .foot').forEach(function (section) {
    var items = Array.prototype.slice.call(section.querySelectorAll('.reveal'));
    items.forEach(function (el, i) {
      el.style.setProperty('--d', Math.min(i, 8) * 65 + 'ms');
    });
  });

  var targets = document.querySelectorAll('.reveal, .stair');

  if (reduceMotion || !('IntersectionObserver' in window)) {
    targets.forEach(function (el) { el.classList.add('is-in'); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-in');
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -6% 0px' });
    targets.forEach(function (el) { io.observe(el); });
  }

  // The signature matcher: score the visitor's five deltas against the measured families.
  var form = document.getElementById('signature-form');
  var panel = document.querySelector('.matcher .verdict');
  if (!form || !panel) return;

  var families = [
    { key: 'anthropic-core', signature: [45, 40, 35, 45, 47] },
    { key: 'anthropic-4.6', signature: [35, 34, 29, 46, 47] },
    { key: 'openai', signature: [24, 24, 22, 30, 29] },
    { key: 'deepseek', signature: [27, 26, 23, 32, 25] }
  ];

  var inputs = [1, 2, 3, 4, 5].map(function (n) {
    return document.getElementById('d' + n);
  });

  var word = document.getElementById('matcher-word');
  var say = document.getElementById('matcher-say');
  var count = document.getElementById('matcher-count');
  var driver = document.getElementById('matcher-driver');
  var matchCells = Array.prototype.slice.call(document.querySelectorAll('.sig-match'));

  function tolerance(reference) {
    return Math.max(2, Math.ceil(0.08 * reference));
  }

  function score(deltas, signature) {
    var matched = 0;
    var deviation = 0;
    for (var i = 0; i < 5; i++) {
      var diff = Math.abs(deltas[i] - signature[i]);
      deviation += diff;
      if (diff <= tolerance(signature[i])) matched += 1;
    }
    return { matched: matched, deviation: deviation };
  }

  function render() {
    var raw = inputs.map(function (input) { return input.value.trim(); });
    var filled = raw.filter(function (value) { return value !== ''; }).length;

    if (filled < 5) {
      word.textContent = filled === 0 ? 'Type five deltas.' : 'Keep typing.';
      say.textContent = filled + ' of 5 deltas in. The matcher scores as soon as all five inputs carry a number.';
      count.textContent = 'Matches per family appear here.';
      driver.textContent = 'Tolerance: max(2, ceil(0.08 x reference)). Weak evidence: same dialect, not the same weights.';
      matchCells.forEach(function (cell) { cell.textContent = '—'; });
      return;
    }

    var deltas = raw.map(function (value) { return Number(value); });
    if (deltas.some(function (value) { return !isFinite(value) || value < 0; })) {
      word.textContent = 'Numbers only.';
      say.textContent = 'Each delta is a non-negative whole token count, tokens(T) - tokens("a").';
      count.textContent = 'Matches per family appear here.';
      driver.textContent = 'Tolerance: max(2, ceil(0.08 x reference)).';
      return;
    }

    var results = families.map(function (family) {
      return { key: family.key, result: score(deltas, family.signature) };
    });

    matchCells.forEach(function (cell) {
      var entry = results.filter(function (item) { return item.key === cell.getAttribute('data-family'); })[0];
      if (entry) cell.textContent = entry.result.matched + ' of 5';
    });

    var best = results.slice().sort(function (a, b) {
      if (b.result.matched !== a.result.matched) return b.result.matched - a.result.matched;
      return a.result.deviation - b.result.deviation;
    })[0];

    var verdict;
    var meaning;
    if (best.result.matched >= 4) {
      verdict = 'SIGNATURE MATCH';
      meaning = 'Four or five of five texts agree. Same counting dialect as ' + best.key + ', not proof of the same weights.';
    } else if (best.result.matched >= 2) {
      verdict = 'PARTIAL MATCH';
      meaning = 'Two or three of five agree with ' + best.key + '. Mixed. Re-run with fresh texts before concluding anything.';
    } else {
      verdict = 'SIGNATURE MISMATCH';
      meaning = 'Zero or one of five agrees with any measured family. The tokenizer does not look like the claimed family’s.';
    }

    word.textContent = verdict;
    say.textContent = meaning;
    count.textContent = results.map(function (item) {
      return item.key + ' ' + item.result.matched + '/5';
    }).join(' · ');
    driver.textContent = 'Best family: ' + best.key + ' (' + best.result.matched + ' of 5, deviation ' + best.result.deviation + '). Tolerance: max(2, ceil(0.08 x reference)).';

    panel.classList.remove('is-new');
    void panel.offsetWidth;
    panel.classList.add('is-new');
  }

  form.addEventListener('submit', function (event) { event.preventDefault(); });
  inputs.forEach(function (input) {
    input.addEventListener('input', render);
    input.addEventListener('change', render);
  });

  Array.prototype.forEach.call(document.querySelectorAll('.load-btn'), function (button) {
    button.addEventListener('click', function () {
      var values = (button.getAttribute('data-load') || '').split(',');
      inputs.forEach(function (input, i) { input.value = values[i] || ''; });
      render();
      inputs[0].focus();
    });
  });

  render();
})();
