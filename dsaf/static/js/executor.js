/**
 * executor.js — Alpine.js app logic and Socket.IO integration for Executor UI
 */

function executorApp() {
  return {
    maps: [],
    patterns: [],
    selectedSurveyId: '',
    selectedPatternId: '',
    runCount: 1,
    concurrency: 1,
    proxyUrl: '',

    batchId: null,
    batchStatus: null,
    total: 0,
    completed: 0,
    succeeded: 0,
    failed: 0,
    currentUid: '',

    logEntries: [],
    runCards: [],
    confirmRunModal: false,

    _socket: null,
    _pollInterval: null,

    async init() {
      await this.loadMeta();
      this._initSocket();
    },

    async loadMeta() {
      const [mapsRes, patternsRes] = await Promise.all([
        fetch('/api/mapper/maps'),
        fetch('/api/config/patterns'),
      ]);
      this.maps = await mapsRes.json();
      this.patterns = await patternsRes.json();
    },

    _initSocket() {
      this._socket = io();

      this._socket.on('run_progress', (data) => {
        if (data.batch_id !== this.batchId) return;

        const now = new Date().toTimeString().slice(0, 8);
        this.logEntries.push({
          time: now,
          uid: data.uid || '',
          status: data.status,
          message: data.message || JSON.stringify(data),
        });

        // Cap log at 200 entries
        if (this.logEntries.length > 200) this.logEntries.shift();

        // Auto-scroll log
        this.$nextTick && this.$nextTick(() => {
          const el = document.getElementById('log-container');
          if (el) el.scrollTop = el.scrollHeight;
        });

        if (typeof data.completed === 'number') this.completed = data.completed;
        if (typeof data.succeeded === 'number') this.succeeded = data.succeeded;
        if (typeof data.failed === 'number') this.failed = data.failed;
        if (data.uid) this.currentUid = data.uid;
        if (data.total) this.total = data.total;
      });

      this._socket.on('batch_complete', (data) => {
        if (data.batch_id !== this.batchId) return;
        this.batchStatus = 'completed';
        const s = data.summary || {};
        showToast(
          `Batch complete: ${s.succeeded}✓ ${s.failed}✗ in ${s.duration_seconds}s`
        );
        this._stopPolling();
        this._loadResults();
      });
    },

    async startRun() {
      this.confirmRunModal = false;
      this.logEntries = [];
      this.runCards = [];
      this.completed = 0;
      this.succeeded = 0;
      this.failed = 0;
      this.batchStatus = 'running';

      const res = await fetch('/api/executor/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          survey_id: this.selectedSurveyId,
          pattern_id: this.selectedPatternId,
          run_count: this.runCount,
          concurrency: this.concurrency,
          proxy_url: this.proxyUrl || null,
        }),
      });

      const data = await res.json();
      if (!res.ok) {
        showToast('Failed to start: ' + (data.error || ''), 'error');
        this.batchStatus = 'error';
        return;
      }

      this.batchId = data.batch_id;
      this.total = this.runCount;
      showToast(`Batch ${this.batchId} started`, 'success');
      this._startPolling();
    },

    _startPolling() {
      if (this._pollInterval) clearInterval(this._pollInterval);
      this._pollInterval = setInterval(async () => {
        if (!this.batchId || this.batchStatus !== 'running') {
          this._stopPolling();
          return;
        }
        try {
          const res = await fetch(`/api/executor/status/${this.batchId}`);
          if (!res.ok) return;
          const d = await res.json();
          this.completed = d.completed || 0;
          this.succeeded = d.succeeded || 0;
          this.failed = d.failed || 0;
          this.currentUid = d.current_uid || '';
          if (d.status !== 'running') {
            this.batchStatus = d.status;
            this._stopPolling();
            await this._loadResults();
          }
        } catch (e) { /* ignore */ }
      }, 2000);
    },

    _stopPolling() {
      if (this._pollInterval) {
        clearInterval(this._pollInterval);
        this._pollInterval = null;
      }
    },

    async _loadResults() {
      if (!this.batchId) return;
      try {
        const res = await fetch(`/api/executor/results/${this.batchId}`);
        if (!res.ok) return;
        this.runCards = await res.json();
      } catch (e) { /* ignore */ }
    },

    async stopBatch() {
      if (!this.batchId) return;
      const res = await fetch(`/api/executor/stop/${this.batchId}`, { method: 'POST' });
      const data = await res.json();
      if (data.stopped) {
        this.batchStatus = 'stopping';
        showToast('Stop signal sent — waiting for current run to finish.', 'warning');
      }
    },

    clearLog() { this.logEntries = []; },

    exportResults() {
      if (!this.runCards.length) return;
      const blob = new Blob([JSON.stringify(this.runCards, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `results_${this.batchId || 'export'}.json`;
      a.click();
      URL.revokeObjectURL(url);
    },
  };
}
