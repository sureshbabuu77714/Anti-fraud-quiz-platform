(function () {
  const cfg = window.QUIZ_CONTEXT;
  const warningCountEl = document.getElementById('warningCount');
  const timerEl = document.getElementById('timer');
  const quizArea = document.getElementById('quizArea');
  const submitForm = document.getElementById('submitTestForm');
  let currentWarnings = Number(warningCountEl?.textContent || 0);
  let violationCooldown = false;
  let monitoringArmed = false;
  let submitting = false;

  function postJSON(url, data) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data)
    }).then(async r => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.message || 'Request failed');
      return data;
    });
  }

  function exitFullscreenSafe() {
    if (document.fullscreenElement && document.exitFullscreen) {
      document.exitFullscreen().catch(() => {});
    }
  }

  function postSubmit(url) {
    submitting = true;
    exitFullscreenSafe();
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = url;
    document.body.appendChild(form);
    form.submit();
  }

  function warn(type, details) {
    if (!monitoringArmed || submitting || violationCooldown) return;
    violationCooldown = true;
    setTimeout(() => violationCooldown = false, 1500);

    postJSON(cfg.violationUrl, { violation_type: type, details })
      .then(data => {
        if (data.ignored) return;
        currentWarnings = data.warnings;
        if (warningCountEl) warningCountEl.textContent = currentWarnings;
        alert(`Security warning: ${type.replace(/_/g, ' ')}. Warning ${currentWarnings}/${window.MAX_VIOLATIONS}`);
        if (data.terminated) {
          alert('Maximum violations reached. Your test is disqualified.');
          window.location.href = cfg.dashboardUrl;
        }
      })
      .catch(() => {});
  }

  function autoFullscreen() {
    const el = document.documentElement;
    if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
  }

  setTimeout(() => { monitoringArmed = true; }, 4000);
  setTimeout(autoFullscreen, 800);

  document.addEventListener('contextmenu', function (e) {
    if (quizArea && quizArea.contains(e.target)) {
      e.preventDefault();
      warn('right_click', 'Context menu blocked');
    }
  });

  ['copy', 'cut', 'paste'].forEach(evt => {
    document.addEventListener(evt, function (e) {
      if (quizArea && quizArea.contains(e.target)) {
        e.preventDefault();
        warn(evt + '_detected', `${evt} blocked`);
      }
    });
  });

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) warn('tab_switch', 'Document hidden');
  });

  document.querySelectorAll('.question-block input[type="radio"]').forEach(input => {
    input.addEventListener('change', function () {
      const block = input.closest('.question-block');
      const questionId = block?.dataset.questionId;
      if (!questionId) return;
      postJSON(cfg.autosaveUrl, { question_id: questionId, selected_answer: input.value }).catch(() => {});
    });
  });

  if (submitForm) {
    submitForm.addEventListener('submit', function () {
      submitting = true;
      exitFullscreenSafe();
    });
  }

  window.addEventListener('beforeunload', function () {
    exitFullscreenSafe();
  });

  function updateTimer() {
    const durationEnd = new Date(new Date(cfg.startedAt.replace(' ', 'T')).getTime() + cfg.durationMinutes * 60000);
    const hardEnd = new Date(cfg.endAt.replace(' ', 'T'));
    const end = durationEnd < hardEnd ? durationEnd : hardEnd;
    const diff = end - new Date();
    if (diff <= 0) {
      timerEl.textContent = '00:00';
      alert('Time is over. The test will be submitted now.');
      postSubmit(cfg.submitUrl);
      return;
    }
    const mins = Math.floor(diff / 60000);
    const secs = Math.floor((diff % 60000) / 1000);
    timerEl.textContent = String(mins).padStart(2, '0') + ':' + String(secs).padStart(2, '0');
  }

  updateTimer();
  setInterval(updateTimer, 1000);
})();
