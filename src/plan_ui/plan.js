(function () {
    'use strict';

    var currentPlan = null;
    var planBridge = null;

    function escapeHtml(s) {
        if (!s) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function checkboxForStatus(status) {
        var st = (status || 'pending').toLowerCase();
        if (st === 'completed') return '[x]';
        if (st === 'in_progress') return '[~]';
        if (st === 'failed' || st === 'blocked') return '[!]';
        if (st === 'skipped') return '[-]';
        return '[ ]';
    }

    function statusClass(status) {
        return 'status-' + (status || 'pending').toLowerCase().replace(/\s+/g, '_');
    }

    function renderStep(step, index) {
        var st = (step.status || 'pending').toLowerCase();
        var desc = step.description || step.tool_name || ('Step ' + (index + 1));
        var tool = step.tool_name ? '<span class="plan-step-tool">' + escapeHtml(step.tool_name) + '</span>' : '';
        var err = step.last_error
            ? '<span class="plan-step-error">' + escapeHtml(step.last_error) + '</span>'
            : '';
        var details = '';
        if (step.rationale || step.expected_outcome) {
            details = '<details><summary>Details</summary>';
            if (step.rationale) {
                details += '<div>' + escapeHtml(step.rationale) + '</div>';
            }
            if (step.expected_outcome) {
                details += '<div><em>Expected:</em> ' + escapeHtml(step.expected_outcome) + '</div>';
            }
            details += '</details>';
        }
        return (
            '<li class="plan-step ' + statusClass(st) + '" data-step-id="' + escapeHtml(step.step_id || '') + '">' +
            '<div class="plan-step-row">' +
            '<span class="plan-checkbox">' + checkboxForStatus(st) + '</span>' +
            '<div class="plan-step-body">' +
            '<span class="plan-step-desc">' + escapeHtml(desc) + '</span>' +
            tool + err + details +
            '</div></div></li>'
        );
    }

    function renderPlan(plan) {
        var emptyEl = document.getElementById('plan-empty');
        var contentEl = document.getElementById('plan-content');
        if (!plan || !plan.steps) {
            currentPlan = null;
            if (emptyEl) emptyEl.hidden = false;
            if (contentEl) contentEl.hidden = true;
            return;
        }
        currentPlan = plan;
        if (emptyEl) emptyEl.hidden = true;
        if (contentEl) contentEl.hidden = false;

        var titleEl = document.getElementById('plan-title');
        var statusEl = document.getElementById('plan-status');
        var summaryEl = document.getElementById('plan-summary');
        var stepsEl = document.getElementById('plan-steps');
        var actionsEl = document.getElementById('plan-actions');
        var execBtn = document.getElementById('plan-exec-btn');
        var editBtn = document.getElementById('plan-edit-btn');

        if (titleEl) titleEl.textContent = plan.title || 'Edit plan';
        var status = (plan.status || 'draft').toLowerCase();
        if (statusEl) {
            statusEl.textContent = status.toUpperCase();
            statusEl.className = 'plan-status ' + statusClass(status);
        }
        if (summaryEl) {
            if (plan.summary) {
                summaryEl.textContent = plan.summary;
                summaryEl.hidden = false;
            } else {
                summaryEl.textContent = '';
                summaryEl.hidden = true;
            }
        }
        if (stepsEl) {
            var html = '';
            var steps = plan.steps || [];
            for (var i = 0; i < steps.length; i++) {
                html += renderStep(steps[i], i);
            }
            stepsEl.innerHTML = html;
        }
        if (actionsEl && execBtn) {
            var canExec = status === 'ready' || status === 'blocked' || status === 'draft';
            actionsEl.hidden = !canExec;
            execBtn.disabled = status !== 'ready' && status !== 'blocked';
            execBtn.textContent = status === 'blocked' ? 'Retry execution' : 'Execute Plan';
        }
        if (editBtn) {
            editBtn.hidden = status !== 'blocked';
        }
    }

    window.loadPlan = function (planJson) {
        try {
            var plan = typeof planJson === 'string' ? JSON.parse(planJson) : planJson;
            renderPlan(plan);
        } catch (e) {
            console.error('loadPlan failed', e);
        }
    };

    window.updateStepStatus = function (stepId, status, error) {
        if (!stepId) return;
        var el = document.querySelector('.plan-step[data-step-id="' + stepId + '"]');
        if (!el) return;
        var st = (status || 'pending').toLowerCase();
        el.className = 'plan-step ' + statusClass(st);
        var cb = el.querySelector('.plan-checkbox');
        if (cb) cb.textContent = checkboxForStatus(st);
        if (error) {
            var errEl = el.querySelector('.plan-step-error');
            if (!errEl) {
                errEl = document.createElement('span');
                errEl.className = 'plan-step-error';
                el.querySelector('.plan-step-body').appendChild(errEl);
            }
            errEl.textContent = error;
        }
        if (currentPlan && currentPlan.steps) {
            for (var i = 0; i < currentPlan.steps.length; i++) {
                if (currentPlan.steps[i].step_id === stepId) {
                    currentPlan.steps[i].status = st;
                    if (error) currentPlan.steps[i].last_error = error;
                    break;
                }
            }
            if (currentPlan.status !== 'blocked' && (st === 'failed' || st === 'blocked')) {
                currentPlan.status = 'blocked';
                renderPlan(currentPlan);
            }
        }
    };

    function wireBridge() {
        var execBtn = document.getElementById('plan-exec-btn');
        if (execBtn) {
            execBtn.addEventListener('click', function () {
                if (planBridge && planBridge.executePlan) {
                    var pid = (currentPlan && currentPlan.plan_id) ? currentPlan.plan_id : '';
                    planBridge.executePlan(pid);
                }
            });
        }
        var editBtn = document.getElementById('plan-edit-btn');
        if (editBtn) {
            editBtn.addEventListener('click', function () {
                if (planBridge && planBridge.editPlanInPlanningMode) {
                    planBridge.editPlanInPlanningMode();
                }
            });
        }
    }

    function connectBridge() {
        if (typeof QWebChannel !== 'undefined' && window.qt && window.qt.webChannelTransport) {
            new QWebChannel(window.qt.webChannelTransport, function (channel) {
                planBridge = channel.objects.planDockBridge;
                wireBridge();
            });
            return;
        }
        if (typeof planDockBridge !== 'undefined') {
            planBridge = planDockBridge;
            wireBridge();
            return;
        }
        setTimeout(connectBridge, 30);
    }

    document.addEventListener('DOMContentLoaded', connectBridge);
    connectBridge();
})();
