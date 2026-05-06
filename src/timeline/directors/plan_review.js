// Plan Review UI JavaScript (ES5; WebEngine QWebChannel or QtWebKit window object)

var bridge = null;
var currentPlan = null;

function connectPlanReview() {
    if (typeof qt !== 'undefined' && qt.webChannelTransport && typeof QWebChannel !== 'undefined') {
        new QWebChannel(qt.webChannelTransport, function(channel) {
            wirePlanReview(channel.objects.planReviewBridge);
        });
        return;
    }
    if (typeof planReviewBridge !== 'undefined') {
        wirePlanReview(planReviewBridge);
        return;
    }
    setTimeout(connectPlanReview, 30);
}

function wirePlanReview(b) {
    bridge = b;
    bridge.planLoaded.connect(function(planJson) {
        loadPlan(JSON.parse(planJson));
    });
}

connectPlanReview();

function loadPlan(plan) {
    currentPlan = plan;

    document.getElementById('empty-state').style.display = 'none';
    document.getElementById('plan-container').style.display = 'block';

    document.getElementById('plan-title').textContent = plan.title;

    var directors = plan.created_by.join(', ');
    document.getElementById('plan-directors').textContent = 'Directors: ' + directors;

    var confidence = plan.confidence || 0.5;
    var confidenceBadge = document.getElementById('plan-confidence');
    confidenceBadge.textContent = 'Confidence: ' + (confidence * 100).toFixed(0) + '%';
    confidenceBadge.className = 'confidence-badge';
    if (confidence >= 0.7) {
        confidenceBadge.classList.add('confidence-high');
    } else if (confidence >= 0.5) {
        confidenceBadge.classList.add('confidence-medium');
    } else {
        confidenceBadge.classList.add('confidence-low');
    }

    document.getElementById('plan-summary-text').textContent = plan.summary || 'No summary available.';

    renderSteps(plan.steps || []);

    renderDebate(plan.debate_transcript || []);
}

function renderSteps(steps) {
    var stepsList = document.getElementById('steps-list');
    var stepsCount = document.getElementById('steps-count');

    stepsCount.textContent = String(steps.length);
    stepsList.innerHTML = '';

    if (steps.length === 0) {
        stepsList.innerHTML = '<p style="color: #888; padding: 20px; text-align: center;">No steps in plan</p>';
        return;
    }

    var index;
    for (index = 0; index < steps.length; index++) {
        var step = steps[index];
        var stepCard = document.createElement('div');
        stepCard.className = 'step-card';
        stepCard.dataset.stepId = step.step_id;

        var confidenceColor = '#888';
        if (step.confidence >= 0.7) confidenceColor = '#4ade80';
        else if (step.confidence >= 0.5) confidenceColor = '#facc15';
        else confidenceColor = '#f87171';

        var inner = '<div class="step-header">' +
            '<div class="step-number">' + (index + 1) + '</div>' +
            '<div class="step-content">' +
            '<div class="step-description">' + escapeHtml(step.description) + '</div>' +
            '<div class="step-meta">' +
            '<span class="step-type">' + escapeHtml(step.type) + '</span>' +
            '<span class="step-agent">Agent: ' + escapeHtml(step.agent) + '</span>' +
            '<span class="step-confidence" style="color: ' + confidenceColor + '">' +
            'Confidence: ' + (step.confidence * 100).toFixed(0) + '%' +
            '</span></div></div></div>';
        if (step.rationale) {
            inner += '<div class="step-rationale">&#128161; ' + escapeHtml(step.rationale) + '</div>';
        }
        stepCard.innerHTML = inner;

        stepsList.appendChild(stepCard);
    }
}

function renderDebate(messages) {
    var debateTranscript = document.getElementById('debate-transcript');
    var debateCount = document.getElementById('debate-count');

    debateCount.textContent = String(messages.length);

    if (messages.length === 0) {
        debateTranscript.innerHTML = '<p style="color: #888; padding: 20px; text-align: center;">No debate messages</p>';
        return;
    }

    debateTranscript.innerHTML = '';

    var rounds = {};
    var i;
    for (i = 0; i < messages.length; i++) {
        var msg = messages[i];
        var round = msg.round_number || 0;
        if (!rounds[round]) {
            rounds[round] = [];
        }
        rounds[round].push(msg);
    }

    var roundKeys = Object.keys(rounds).sort(function(a, b) { return Number(a) - Number(b); });
    var r;
    for (r = 0; r < roundKeys.length; r++) {
        var round = roundKeys[r];
        var roundHeader = document.createElement('div');
        roundHeader.style.cssText = 'font-weight: 600; color: #6366f1; margin: 16px 0 8px 0; font-size: 14px;';
        roundHeader.textContent = round === '0' ? 'Initial Analysis' : ('Round ' + round);
        debateTranscript.appendChild(roundHeader);

        var list = rounds[round];
        var j;
        for (j = 0; j < list.length; j++) {
            var m = list[j];
            var messageDiv = document.createElement('div');
            messageDiv.className = 'debate-message';
            messageDiv.innerHTML =
                '<div class="debate-message-header">' +
                '<span class="debate-director">' + escapeHtml(m.director_name) + '</span>' +
                '<span class="debate-round">' + escapeHtml(m.message_type) + '</span>' +
                '</div>' +
                '<div class="debate-content">' + escapeHtml(m.content) + '</div>';
            debateTranscript.appendChild(messageDiv);
        }
    }
}

function toggleDebate() {
    var debateTranscript = document.getElementById('debate-transcript');
    var toggleIcon = document.getElementById('debate-toggle');

    if (debateTranscript.style.display === 'none') {
        debateTranscript.style.display = 'block';
        toggleIcon.textContent = '\u25bc';
        toggleIcon.classList.add('expanded');
    } else {
        debateTranscript.style.display = 'none';
        toggleIcon.textContent = '\u25b6';
        toggleIcon.classList.remove('expanded');
    }
}

function approvePlan() {
    if (!currentPlan || !bridge) {
        return;
    }
    bridge.approvePlan(currentPlan.plan_id);
}

function rejectPlan() {
    if (!currentPlan || !bridge) {
        return;
    }
    bridge.rejectPlan(currentPlan.plan_id);
}

function escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
