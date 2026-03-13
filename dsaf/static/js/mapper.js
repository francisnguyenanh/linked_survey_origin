/**
 * mapper.js — Alpine.js app logic for the Mapper UI (mapper.html)
 */

function mapperApp() {
  return {
    // State
    surveyUrl: '',
    headless: false,
    sessionId: null,
    surveyId: '',
    loading: false,

    pagesRecorded: [],
    currentPageQuestions: [],
    currentPageId: null,
    currentFingerprint: null,
    currentPageStatus: null,
    lastBranchTarget: null,
    currentAnswers: {},
    newBranchCount: 0,
    branchLabel: '',

    coverageStats: {},
    branchTreeSummary: '',
    showTreeModal: false,

    async init() {
      await this.loadCoverage();
    },

    // ── Mapping session ────────────────────────────────────────────────

    async startMapping() {
      this.loading = true;
      try {
        const res = await fetch('/api/mapper/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ survey_url: this.surveyUrl, headless: this.headless }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Unknown error');
        this.sessionId = data.session_id;
        showToast('Browser launched. Navigate the survey and click Scan Page.');
      } catch (e) {
        showToast('Failed to start mapping: ' + e.message, 'error');
      } finally {
        this.loading = false;
      }
    },

    async scanPage() {
      if (!this.sessionId) return;
      try {
        const res = await fetch('/api/mapper/scan-page', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: this.sessionId }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Scan failed');
        this.currentPageQuestions = data.page_data.questions || [];
        this.currentFingerprint = data.fingerprint;
        this.currentAnswers = {};
        showToast(`Scanned: ${this.currentPageQuestions.length} questions found`);
      } catch (e) {
        showToast('Scan error: ' + e.message, 'error');
      }
    },

    recordAnswer(qId, value) {
      this.currentAnswers[qId] = value;
    },

    async confirmAnswersAndProceed() {
      if (!this.sessionId || !this.currentFingerprint) return;

      // Submit current page data with recorded answers to branch-aware endpoint
      const pageData = {
        page_fingerprint: this.currentFingerprint,
        questions: this.currentPageQuestions,
      };

      const body = {
        session_id: this.sessionId,
        previous_page_id: this.currentPageId,
        answers_on_previous_page: Object.keys(this.currentAnswers).length > 0
          ? this.currentAnswers : null,
        current_page_data: pageData,
      };

      try {
        const res = await fetch('/api/mapper/page/record-with-answers', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Record failed');

        this.currentPageId = data.page_id;
        this.currentPageStatus = data.status;
        if (data.is_new_branch) this.newBranchCount++;

        this.pagesRecorded.push({ page_id: data.page_id, status: data.status });
        this.currentAnswers = {};

        const msg = data.status === 'new'
          ? `✓ New page recorded: ${data.page_id}`
          : `↻ Revisited: ${data.page_id}`;
        showToast(msg, data.is_new_branch ? 'success' : 'warning');

        await this.loadCoverage();
      } catch (e) {
        showToast('Record error: ' + e.message, 'error');
      }
    },

    async saveBranchLabel() {
      showToast('Branch label saved: ' + this.branchLabel);
      this.branchLabel = '';
    },

    async stopSession() {
      if (!this.sessionId) return;
      if (!confirm('Stop recording session and save?')) return;
      try {
        await fetch('/api/mapper/session/end', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: this.sessionId, result: 'aborted' }),
        });
        this.sessionId = null;
        this.pagesRecorded = [];
        this.currentPageQuestions = [];
        showToast('Session stopped and saved.');
      } catch (e) {
        showToast('Error stopping session', 'error');
      }
    },

    async finalizeMap() {
      if (!this.sessionId || !this.surveyId.trim()) {
        showToast('Survey ID is required before finalizing.', 'warning');
        return;
      }
      try {
        const res = await fetch('/api/mapper/finalize', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: this.sessionId, survey_id: this.surveyId }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Finalize failed');
        showToast(`Map saved: ${data.total_pages} pages, ${data.total_questions} questions`);
        this.sessionId = null;
        this.pagesRecorded = [];
      } catch (e) {
        showToast('Finalize error: ' + e.message, 'error');
      }
    },

    // ── Coverage ───────────────────────────────────────────────────────

    async loadCoverage() {
      if (!this.surveyId) return;
      try {
        const res = await fetch(`/api/mapper/coverage/${this.surveyId}`);
        if (!res.ok) return;
        const data = await res.json();
        this.coverageStats = data.coverage_stats || {};
        this.branchTreeSummary = data.branch_tree_summary || '';
      } catch (e) {
        // silently ignore
      }
    },
  };
}

// ==========================================================================
// Auto-Mapping Alpine component (Section 3.2c)
// ==========================================================================

function autoMappingApp() {
  return {
    // Config
    autoSurveyUrl: '',
    autoSafeUid: '',
    autoSurveyId: '',
    autoMaxBranches: 200,
    autoMaxDepth: 20,
    autoUidPoolRaw: '',

    // State
    panelOpen: false,
    autoJobId: null,
    autoStatus: null,   // queued | running | stopping | complete | error
    autoBranchesExplored: 0,
    autoPagesFound: 0,
    autoPatternsGenerated: 0,
    autoElapsed: 0,
    autoStartTime: null,
    autoLog: [],           // [{time, event, message}]
    autoGeneratedPatterns: [],
    estimate: {},

    // Graph preview modal
    showGraphModal: false,
    graphTree: '',
    graphStats: {},

    // Polling interval handle
    _pollHandle: null,
    _socket: null,

    async autoInit() {
      // Connect to SocketIO and listen for auto-mapping events
      this._socket = io();

      this._socket.on('mapping_progress', (data) => {
        if (data.job_id !== this.autoJobId) return;
        this._addLog(data.status || 'progress', data.message || JSON.stringify(data));
        if (typeof data.branches_explored === 'number') this.autoBranchesExplored = data.branches_explored;
        if (typeof data.pages_found === 'number') this.autoPagesFound = data.pages_found;
        this._updateElapsed();
      });

      this._socket.on('mapping_new_branch', (data) => {
        if (data.job_id !== this.autoJobId) return;
        const triggers = Object.entries(data.trigger_answers || {})
          .map(([k, v]) => `${k}=${v}`).join(', ');
        this._addLog('branch_start', `${data.from_page} → depth ${data.depth}  [${triggers}]`);
        this._scrollLog();
      });

      this._socket.on('mapping_complete', (data) => {
        if (data.job_id !== this.autoJobId) return;
        this.autoStatus = 'complete';
        this.autoBranchesExplored = data.branches_explored || this.autoBranchesExplored;
        this.autoPagesFound = data.total_pages || 0;
        this.autoPatternsGenerated = data.patterns_generated || 0;
        this._addLog('complete', `Done — ${data.total_pages} pages, ${data.patterns_generated} patterns in ${data.duration_seconds}s`);
        this._stopPolling();
        this._loadPatterns();
        showToast(`Auto-mapping complete: ${data.patterns_generated} patterns generated`, 'success');
      });

      this._socket.on('mapping_error', (data) => {
        if (data.job_id !== this.autoJobId) return;
        this.autoStatus = 'error';
        this._addLog('error', data.message || 'Unknown error');
        this._stopPolling();
        showToast('Auto-mapping error: ' + (data.message || ''), 'error');
      });
    },

    // ── Config ─────────────────────────────────────────────────────────

    async estimateTime() {
      // We don't know the trigger matrix yet without running — provide a
      // rough estimate based on max_branches as upper bound.
      this.estimate = {
        estimated_branches: this.autoMaxBranches,
        estimated_minutes: (this.autoMaxBranches * 1.5).toFixed(0),
        warning: this.autoMaxBranches > 100
          ? `Up to ${this.autoMaxBranches} branches (~${(this.autoMaxBranches * 1.5).toFixed(0)} min)`
          : null,
      };
    },

    // ── Control ────────────────────────────────────────────────────────

    async startAutoMapping() {
      if (!this.autoSurveyUrl || !this.autoSafeUid) {
        showToast('Survey URL and Safe UID are required.', 'warning');
        return;
      }

      this.autoLog = [];
      this.autoGeneratedPatterns = [];
      this.autoBranchesExplored = 0;
      this.autoPagesFound = 0;
      this.autoPatternsGenerated = 0;
      this.autoStartTime = Date.now();
      this.autoElapsed = 0;
      this.autoStatus = 'queued';

      const uidPool = this.autoUidPoolRaw
        ? this.autoUidPoolRaw.split(',').map(s => s.trim()).filter(Boolean)
        : [];

      const res = await fetch('/api/mapper/auto/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          survey_url: this.autoSurveyUrl,
          safe_uid: this.autoSafeUid,
          survey_id: this.autoSurveyId || undefined,
          max_depth: this.autoMaxDepth,
          max_branches: this.autoMaxBranches,
          uid_pool: uidPool,
        }),
      });

      const data = await res.json();
      if (!res.ok) {
        showToast('Failed to start: ' + (data.error || ''), 'error');
        this.autoStatus = 'error';
        return;
      }

      this.autoJobId = data.job_id;
      this.autoSurveyId = data.survey_id || this.autoSurveyId;
      this.autoStatus = 'running';
      this._addLog('progress', `Job ${this.autoJobId} started — survey: ${this.autoSurveyId}`);
      showToast(`Auto-mapping started (job: ${this.autoJobId})`);
      this._startPolling();
    },

    async stopAutoMapping() {
      if (!this.autoJobId) return;
      const res = await fetch(`/api/mapper/auto/stop/${this.autoJobId}`, { method: 'POST' });
      const data = await res.json();
      if (data.stopped) {
        this.autoStatus = 'stopping';
        showToast('Stop signal sent — finishing current branch.', 'warning');
      }
    },

    // ── Results ────────────────────────────────────────────────────────

    async _loadPatterns() {
      // Reload the full patterns list to pick up auto-generated ones
      try {
        const res = await fetch('/api/config/patterns');
        if (!res.ok) return;
        const allPatterns = await res.json();
        this.autoGeneratedPatterns = allPatterns.filter(p => p.auto_generated);
        this.autoPatternsGenerated = this.autoGeneratedPatterns.length;
      } catch (e) { /* ignore */ }
    },

    exportPatterns() {
      if (!this.autoGeneratedPatterns.length) return;
      const blob = new Blob(
        [JSON.stringify(this.autoGeneratedPatterns, null, 2)],
        { type: 'application/json' }
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `auto_patterns_${this.autoSurveyId || this.autoJobId}.json`;
      a.click();
      URL.revokeObjectURL(url);
    },

    async showGraphPreview() {
      if (!this.autoJobId) return;
      try {
        const res = await fetch(`/api/mapper/auto/preview/${this.autoJobId}`);
        const data = await res.json();
        this.graphTree = data.tree_summary || '(empty)';
        this.graphStats = data.graph_stats || {};
      } catch (e) {
        this.graphTree = '(error loading graph)';
      }
      this.showGraphModal = true;
    },

    // ── Polling fallback ───────────────────────────────────────────────

    _startPolling() {
      if (this._pollHandle) clearInterval(this._pollHandle);
      this._pollHandle = setInterval(async () => {
        if (!this.autoJobId || this.autoStatus === 'complete' || this.autoStatus === 'error') {
          this._stopPolling();
          return;
        }
        try {
          const res = await fetch(`/api/mapper/auto/status/${this.autoJobId}`);
          if (!res.ok) return;
          const d = await res.json();
          this.autoBranchesExplored = d.branches_explored || this.autoBranchesExplored;
          this.autoPagesFound = d.pages_found || this.autoPagesFound;
          this._updateElapsed();

          if (d.status === 'complete' && this.autoStatus !== 'complete') {
            this.autoStatus = 'complete';
            if (d.result) this.autoPatternsGenerated = d.result.patterns_generated || 0;
            this._stopPolling();
            await this._loadPatterns();
          } else if (d.status === 'error') {
            this.autoStatus = 'error';
            this._stopPolling();
          }
        } catch (e) { /* ignore */ }
      }, 3000);
    },

    _stopPolling() {
      if (this._pollHandle) {
        clearInterval(this._pollHandle);
        this._pollHandle = null;
      }
    },

    // ── Utilities ──────────────────────────────────────────────────────

    _addLog(event, message) {
      const time = new Date().toTimeString().slice(0, 8);
      this.autoLog.push({ time, event, message });
      if (this.autoLog.length > 200) this.autoLog.shift();
      this.$nextTick && this.$nextTick(() => this._scrollLog());
    },

    _scrollLog() {
      const el = document.getElementById('auto-log-container');
      if (el) el.scrollTop = el.scrollHeight;
    },

    _updateElapsed() {
      if (this.autoStartTime) {
        this.autoElapsed = Math.round((Date.now() - this.autoStartTime) / 1000);
      }
    },
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Hybrid Mapper Alpine component
// ─────────────────────────────────────────────────────────────────────────────

function hybridMappingApp() {
  return {
    // Config
    hybridSurveyUrl: '',
    hybridUidPoolRaw: '',
    hybridSurveyId: '',
    hybridHeadless: true,

    // State
    panelOpen: false,
    hybridJobId: null,
    hybridStatus: null,       // queued | running | stopping | complete | error
    hybridPagesFound: 0,
    hybridCurrentStrategy: null,
    hybridUidsUsed: [],
    hybridUidsRemaining: [],
    hybridLog: [],            // [{time, event, message}]

    // UID report (loaded after completion)
    uidReport: {},

    // Internals
    _pollHandle: null,
    _socket: null,

    hybridInit() {
      this._socket = io();

      this._socket.on('mapping_new_page', (data) => {
        this._addLog('new_page', `Page ${data.page_id}  depth:${data.depth}`);
        this.hybridPagesFound += 1;
      });

      this._socket.on('mapping_trying', (data) => {
        const combo = Object.entries(data.combo || {}).map(([k, v]) => `${k}=${v}`).join(', ');
        const label = `[${data.index + 1}/${data.total}] ${data.strategy}  ${combo}${data.uid ? '  uid:' + data.uid : ''}`;
        this._addLog('trying', label);
        this.hybridCurrentStrategy = data.strategy;
        if (data.uid && data.strategy === 'new_uid_restart') {
          this._addLog('new_uid', `Opened new context for UID: ${data.uid}`);
        }
      });

      this._socket.on('mapping_back_failed', (data) => {
        this._addLog('back_failed', `Back button failed at ${data.page_id} combo ${data.combo_index} — switching to restart`);
      });

      this._socket.on('mapping_warning', (data) => {
        this._addLog('warning', data.msg + (data.page_id ? ` (${data.page_id})` : ''));
      });

      this._socket.on('mapping_terminal', (data) => {
        this._addLog('terminal', `Terminal page reached at depth ${data.depth}`);
      });

      this._socket.on('mapping_complete', (data) => {
        this.hybridStatus = 'complete';
        this.hybridPagesFound = data.pages || this.hybridPagesFound;
        this._addLog('complete', `Done — ${data.pages} pages, ${data.branches} branches, ${data.patterns} patterns`);
        if (Array.isArray(data.uids_used)) this.hybridUidsUsed = data.uids_used;
        this._stopPolling();
        this.loadUidReport();
        showToast(`Hybrid mapping complete: ${data.patterns} patterns`, 'success');
      });

      this._socket.on('mapping_error', (data) => {
        this.hybridStatus = 'error';
        this._addLog('error', data.message || 'Unknown error');
        this._stopPolling();
        showToast('Hybrid mapping error: ' + (data.message || ''), 'error');
      });
    },

    // ── Control ────────────────────────────────────────────────────────

    async startHybridMapping() {
      if (!this.hybridSurveyUrl) {
        showToast('Survey URL is required.', 'warning');
        return;
      }
      const uidPool = this.hybridUidPoolRaw
        .split(',').map(s => s.trim()).filter(Boolean);
      if (!uidPool.length) {
        showToast('Provide at least one UID in the pool.', 'warning');
        return;
      }

      this.hybridLog = [];
      this.hybridPagesFound = 0;
      this.hybridUidsUsed = [];
      this.hybridUidsRemaining = [];
      this.hybridCurrentStrategy = null;
      this.uidReport = {};
      this.hybridStatus = 'queued';

      const res = await fetch('/api/mapper/hybrid/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          survey_url: this.hybridSurveyUrl,
          uid_pool: uidPool,
          survey_id: this.hybridSurveyId || undefined,
          headless: this.hybridHeadless,
        }),
      });

      const data = await res.json();
      if (!res.ok) {
        showToast('Failed to start: ' + (data.error || ''), 'error');
        this.hybridStatus = 'error';
        return;
      }

      this.hybridJobId = data.job_id;
      this.hybridSurveyId = data.survey_id || this.hybridSurveyId;
      this.hybridStatus = 'running';
      this._addLog('start', `Job ${this.hybridJobId} started — survey: ${this.hybridSurveyId}  UIDs: ${uidPool.join(', ')}`);
      showToast(`Hybrid mapping started (job: ${this.hybridJobId})`);
      this._startPolling();
    },

    async stopHybridMapping() {
      if (!this.hybridJobId) return;
      // No dedicated stop endpoint for hybrid; mark locally
      this.hybridStatus = 'stopping';
      showToast('Stop requested — waiting for current branch to finish.', 'warning');
    },

    // ── UID Report ─────────────────────────────────────────────────────

    async loadUidReport() {
      if (!this.hybridJobId) return;
      try {
        const res = await fetch(`/api/mapper/hybrid/uid-report/${this.hybridJobId}`);
        if (res.ok) {
          this.uidReport = await res.json();
        }
      } catch (e) { /* ignore */ }
    },

    // ── Polling fallback ───────────────────────────────────────────────

    _startPolling() {
      if (this._pollHandle) clearInterval(this._pollHandle);
      this._pollHandle = setInterval(async () => {
        if (!this.hybridJobId ||
            this.hybridStatus === 'complete' ||
            this.hybridStatus === 'error') {
          this._stopPolling();
          return;
        }
        try {
          const res = await fetch(`/api/mapper/hybrid/status/${this.hybridJobId}`);
          if (!res.ok) return;
          const d = await res.json();
          this.hybridPagesFound = d.pages_found || this.hybridPagesFound;
          if (Array.isArray(d.uids_used)) this.hybridUidsUsed = d.uids_used;
          if (Array.isArray(d.uids_remaining)) this.hybridUidsRemaining = d.uids_remaining;
          if (d.current_strategy) this.hybridCurrentStrategy = d.current_strategy;

          if (d.status === 'complete' && this.hybridStatus !== 'complete') {
            this.hybridStatus = 'complete';
            this._stopPolling();
            await this.loadUidReport();
          } else if (d.status === 'error') {
            this.hybridStatus = 'error';
            this._stopPolling();
          }
        } catch (e) { /* ignore */ }
      }, 3000);
    },

    _stopPolling() {
      if (this._pollHandle) {
        clearInterval(this._pollHandle);
        this._pollHandle = null;
      }
    },

    // ── Utilities ──────────────────────────────────────────────────────

    _addLog(event, message) {
      const time = new Date().toTimeString().slice(0, 8);
      this.hybridLog.push({ time, event, message });
      if (this.hybridLog.length > 300) this.hybridLog.shift();
      this.$nextTick && this.$nextTick(() => {
        const el = document.getElementById('hybrid-log-container');
        if (el) el.scrollTop = el.scrollHeight;
      });
    },
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Shadow Mode Alpine component  (Mode 1 — user pilots, bot observes)
// ─────────────────────────────────────────────────────────────────────────────

function shadowMappingApp() {
  return {
    // Config
    shadowSurveyUrl: '',
    shadowUid: '',
    shadowSurveyId: '',
    shadowAssisted: true,

    // State
    panelOpen: false,
    shadowSessionId: null,
    shadowStatus: null,         // starting | running | complete | error
    shadowPagesFound: 0,
    shadowPathLength: 0,
    shadowCoverage: 0,
    shadowSuggestions: [],      // unexplored options on current page
    shadowLog: [],              // [{time, event, message}]
    shadowResult: null,         // result from /shadow/stop
    shadowPatternName: '',

    // Internals
    _pollHandle: null,
    _socket: null,

    shadowInit() {
      this._socket = io();

      this._socket.on('shadow_session_started', (data) => {
        this.shadowStatus = 'running';
        this._addLog('start', data.msg || `Session started — UID: ${data.uid}`);
      });

      this._socket.on('shadow_new_page', (data) => {
        this.shadowPagesFound += 1;
        this._addLog('new_page', data.msg || `Page found: ${data.page_id} (${data.question_count} questions)`);
        this._scrollLog();
      });

      this._socket.on('shadow_new_branch', (data) => {
        const ans = Object.entries(data.trigger_answers || {}).map(([k,v]) => `${k}=${v}`).join(', ');
        this._addLog('new_branch', `${data.msg || '🔀 New branch'}  ${data.from} → ${data.to}  [${ans}]`);
      });

      this._socket.on('shadow_known_page', (data) => {
        this._addLog('known', data.msg || `Known page: ${data.page_id}`);
      });

      this._socket.on('shadow_post_captured', (data) => {
        const preview = Object.entries(data.answers_preview || {}).map(([k,v]) => `${k}=${v}`).join(' ');
        this._addLog('post', `[${data.action}] ${data.field_count} fields captured  ${preview}`);
      });

      this._socket.on('shadow_suggestion', (data) => {
        this.shadowSuggestions = data.unexplored || [];
        this._addLog('suggestion', data.msg || `${this.shadowSuggestions.length} unexplored branches`);
      });

      this._socket.on('shadow_coverage_update', (data) => {
        this.shadowCoverage = data.coverage_pct || 0;
        this.shadowPagesFound = data.total_pages || this.shadowPagesFound;
      });

      this._socket.on('shadow_terminal_warning', (data) => {
        this._addLog('terminal', `${data.msg}  ${data.advice || ''}`);
        showToast('⚠ Sắp Submit! Xem cảnh báo trong log.', 'warning');
      });

      this._socket.on('shadow_warning', (data) => {
        this._addLog('warning', data.msg || 'Warning');
      });

      this._socket.on('shadow_error', (data) => {
        this.shadowStatus = 'error';
        this._addLog('error', data.message || 'Unknown error');
        this._stopPolling();
        showToast('Shadow error: ' + (data.message || ''), 'error');
      });
    },

    // ── Control ────────────────────────────────────────────────────────

    async startShadowSession() {
      if (!this.shadowSurveyUrl) {
        showToast('Survey URL is required.', 'warning');
        return;
      }
      if (!this.shadowUid) {
        showToast('UID is required.', 'warning');
        return;
      }

      this.shadowLog = [];
      this.shadowPagesFound = 0;
      this.shadowPathLength = 0;
      this.shadowCoverage = 0;
      this.shadowSuggestions = [];
      this.shadowResult = null;
      this.shadowPatternName = '';
      this.shadowStatus = 'starting';

      const res = await fetch('/api/mapper/shadow/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          survey_url: this.shadowSurveyUrl,
          uid: this.shadowUid,
          survey_id: this.shadowSurveyId || undefined,
          assisted: this.shadowAssisted,
        }),
      });

      const data = await res.json();
      if (!res.ok) {
        showToast('Failed to start: ' + (data.error || ''), 'error');
        this.shadowStatus = 'error';
        return;
      }

      this.shadowSessionId = data.session_id;
      this.shadowSurveyId = data.survey_id || this.shadowSurveyId;
      this._addLog('start', `Session ${this.shadowSessionId} started — làm khảo sát trong browser vừa mở`);
      showToast(`Shadow session đã mở. Hãy làm khảo sát trong browser mới.`);
      this._startPolling();
    },

    async stopShadowSession() {
      if (!this.shadowSessionId) return;

      const res = await fetch(`/api/mapper/shadow/stop/${this.shadowSessionId}`, {
        method: 'POST',
      });
      const data = await res.json();

      this.shadowStatus = 'complete';
      this.shadowResult = data;
      this._stopPolling();

      if (data.pattern_saved) {
        this._addLog('complete', `Pattern saved — ID: ${data.pattern_id}, pages: ${data.pages_mapped}, coverage: ${data.coverage_pct}%`);
        showToast(`Pattern đã lưu: ${data.pattern_id}`, 'success');
      } else {
        this._addLog('complete', `Session ended — ${data.pages_mapped} pages, ${data.coverage_pct}% coverage`);
      }
    },

    async saveSessionPattern() {
      if (!this.shadowSessionId) return;
      const res = await fetch(`/api/mapper/shadow/save-pattern/${this.shadowSessionId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pattern_name: this.shadowPatternName }),
      });
      const data = await res.json();
      if (res.ok) {
        showToast(`Pattern saved: ${data.pattern_id}`, 'success');
        this._addLog('saved', `Pattern saved as "${this.shadowPatternName || data.pattern_id}"`);
        this.shadowPatternName = '';
      } else {
        showToast('Save failed: ' + (data.error || ''), 'error');
      }
    },

    // ── Polling ────────────────────────────────────────────────────────

    _startPolling() {
      if (this._pollHandle) clearInterval(this._pollHandle);
      this._pollHandle = setInterval(async () => {
        if (!this.shadowSessionId ||
            this.shadowStatus === 'complete' ||
            this.shadowStatus === 'error') {
          this._stopPolling();
          return;
        }
        try {
          const res = await fetch(`/api/mapper/shadow/live/${this.shadowSessionId}`);
          if (!res.ok) return;
          const d = await res.json();
          this.shadowPagesFound = d.pages_found || this.shadowPagesFound;
          this.shadowPathLength = d.session_path_length || this.shadowPathLength;
          this.shadowCoverage = d.coverage_pct || this.shadowCoverage;
          if (Array.isArray(d.unexplored_suggestions)) {
            this.shadowSuggestions = d.unexplored_suggestions;
          }
          if (d.status === 'complete' && this.shadowStatus !== 'complete') {
            this.shadowStatus = 'complete';
            this._stopPolling();
          }
        } catch (e) { /* ignore */ }
      }, 2000);
    },

    _stopPolling() {
      if (this._pollHandle) {
        clearInterval(this._pollHandle);
        this._pollHandle = null;
      }
    },

    // ── Utilities ──────────────────────────────────────────────────────

    _addLog(event, message) {
      const time = new Date().toTimeString().slice(0, 8);
      this.shadowLog.push({ time, event, message });
      if (this.shadowLog.length > 300) this.shadowLog.shift();
      this.$nextTick && this.$nextTick(() => this._scrollLog());
    },

    _scrollLog() {
      const el = document.getElementById('shadow-log-container');
      if (el) el.scrollTop = el.scrollHeight;
    },
  };
}
