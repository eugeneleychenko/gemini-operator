// Operator — Frontend Application
const API_BASE = window.location.origin;

const app = {
  taskInput: null,
  submitBtn: null,
  taskPanel: null,
  screenshotImg: null,
  reasoningLog: null,
  confirmOverlay: null,
  confirmText: null,
  currentTaskId: null,
  polling: false,

  init() {
    this.taskInput = document.getElementById('task-input');
    this.submitBtn = document.getElementById('submit-btn');
    this.taskPanel = document.getElementById('task-panel');
    this.screenshotImg = document.getElementById('screenshot');
    this.reasoningLog = document.getElementById('reasoning-log');
    this.confirmOverlay = document.getElementById('confirm-overlay');
    this.confirmText = document.getElementById('confirm-text');

    this.submitBtn.addEventListener('click', () => this.startTask());
    this.taskInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') this.startTask();
    });
    document.getElementById('confirm-yes')?.addEventListener('click', () => this.respond(true));
    document.getElementById('confirm-no')?.addEventListener('click', () => this.respond(false));
  },

  async startTask() {
    const description = this.taskInput.value.trim();
    if (!description) return;

    this.submitBtn.disabled = true;
    this.taskInput.disabled = true;
    this.reasoningLog.innerHTML = '';

    try {
      const res = await fetch(`${API_BASE}/api/task`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description })
      });
      const data = await res.json();
      this.currentTaskId = data.task_id;
      this.taskPanel.classList.remove('hidden');
      this.addLog('system', `Task started: ${description}`);
      this.pollStatus();
    } catch (err) {
      this.addLog('error', `Failed to start task: ${err.message}`);
      this.submitBtn.disabled = false;
      this.taskInput.disabled = false;
    }
  },

  async pollStatus() {
    if (this.polling) return;
    this.polling = true;

    while (this.currentTaskId) {
      try {
        const res = await fetch(`${API_BASE}/api/task/${this.currentTaskId}/status`);
        const data = await res.json();

        // Update screenshot
        if (data.screenshot_url) {
          this.screenshotImg.src = data.screenshot_url + '?t=' + Date.now();
          this.screenshotImg.classList.remove('hidden');
        }

        // Add new reasoning steps
        if (data.steps) {
          for (const step of data.steps) {
            if (!document.getElementById(`step-${step.index}`)) {
              this.addStep(step);
            }
          }
        }

        // Check for confirmation needed
        if (data.status === 'waiting_confirmation') {
          this.showConfirm(data.pending_action);
        }

        // Check for completion
        if (data.status === 'completed' || data.status === 'failed' || data.status === 'cancelled') {
          this.addLog('system', `Task ${data.status}${data.summary ? ': ' + data.summary : ''}`);
          this.currentTaskId = null;
          this.submitBtn.disabled = false;
          this.taskInput.disabled = false;
          break;
        }
      } catch (err) {
        this.addLog('error', `Poll error: ${err.message}`);
      }

      await new Promise(r => setTimeout(r, 2000));
    }

    this.polling = false;
  },

  showConfirm(action) {
    this.confirmText.textContent = action?.description || 'The agent wants to perform a sensitive action. Allow?';
    this.confirmOverlay.classList.remove('hidden');
  },

  async respond(approved) {
    this.confirmOverlay.classList.add('hidden');
    try {
      await fetch(`${API_BASE}/api/task/${this.currentTaskId}/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approved })
      });
      this.addLog('user', approved ? 'Action approved' : 'Action rejected');
    } catch (err) {
      this.addLog('error', `Confirm failed: ${err.message}`);
    }
  },

  addStep(step) {
    const div = document.createElement('div');
    div.id = `step-${step.index}`;
    div.className = 'step';
    div.innerHTML = `
      <div class="step-header">
        <span class="step-num">Step ${step.index + 1}</span>
        <span class="step-action">${step.action_type || 'analyze'}</span>
      </div>
      <div class="step-reasoning">${step.reasoning || ''}</div>
      ${step.action_detail ? `<div class="step-detail">${step.action_detail}</div>` : ''}
    `;
    this.reasoningLog.appendChild(div);
    this.reasoningLog.scrollTop = this.reasoningLog.scrollHeight;
  },

  addLog(type, message) {
    const div = document.createElement('div');
    div.className = `log-entry log-${type}`;
    div.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
    this.reasoningLog.appendChild(div);
    this.reasoningLog.scrollTop = this.reasoningLog.scrollHeight;
  }
};

document.addEventListener('DOMContentLoaded', () => app.init());
