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

  // The evidence ledger: identity findings decide identity, service findings never do.
  var form = document.getElementById('evidence-form');
  var panel = document.querySelector('.verdict');
  if (!form || !panel) return;

  var boxes = Array.prototype.slice.call(form.querySelectorAll('input[type="checkbox"]'));
  var word = document.getElementById('verdict-word');
  var say = document.getElementById('verdict-say');
  var count = document.getElementById('verdict-count');
  var driver = document.getElementById('verdict-driver');

  var labels = {
    'self-report': 'self-report',
    'cutoff': 'cutoff drift',
    'battery': 'battery',
    'logprobs': 'logprobs',
    'budget': 'reasoning budget'
  };

  var verdicts = {
    contradictions: ['Contradictions found', 'Hard evidence against the label. Do not trust it.'],
    tensions: ['Identity tensions found', 'Mixed signals. Investigate the listed items.'],
    service: ['Service degradation detected', 'The model may be genuine, but it is capped or mutated.'],
    consistent: ['Consistent', 'No contradiction found. Not proof of authenticity.'],
    inconclusive: ['Inconclusive', 'No evidence in either direction.']
  };

  function render() {
    var picked = boxes.filter(function (box) { return box.checked; }).map(function (box) {
      return {
        kind: box.getAttribute('data-kind'),
        dir: box.getAttribute('data-dir'),
        weight: box.getAttribute('data-weight'),
        short: box.getAttribute('data-short')
      };
    });

    var against = picked.filter(function (item) { return item.kind === 'identity' && item.dir === 'against'; });
    var support = picked.filter(function (item) { return item.kind === 'identity' && item.dir === 'for'; });
    var service = picked.filter(function (item) { return item.kind === 'service'; });

    var key = 'inconclusive';
    var drivers = [];
    if (against.length) {
      key = against.some(function (item) { return item.weight === 'strong'; }) ? 'contradictions' : 'tensions';
      drivers = (key === 'contradictions')
        ? against.filter(function (item) { return item.weight === 'strong'; })
        : against;
    } else if (service.length) {
      key = 'service';
      drivers = service;
    } else if (support.length) {
      key = 'consistent';
      drivers = support;
    }

    word.textContent = verdicts[key][0];
    say.textContent = verdicts[key][1];
    count.textContent = 'Identity: ' + support.length + ' for, ' + against.length +
      ' against. Service: ' + service.length + ' against.';
    driver.textContent = drivers.length
      ? 'Decided by: ' + drivers.map(function (item) { return labels[item.short]; }).join(', ') + '.'
      : 'Nothing selected yet.';

    panel.classList.remove('is-new');
    void panel.offsetWidth;
    panel.classList.add('is-new');
  }

  form.addEventListener('submit', function (event) { event.preventDefault(); });
  boxes.forEach(function (box) { box.addEventListener('change', render); });
  render();
})();
