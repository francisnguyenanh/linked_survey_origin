/**
 * configurator.js — Alpine.js app logic for the Configurator UI (configurator.html)
 */

function configuratorApp() {
  return {
    maps: [],
    patterns: [],
    selectedSurveyId: '',
    allQuestions: [],
    groupedQuestions: [],
    activeQuestion: null,
    activeQId: null,
    activeStrategy: { strategy: 'fixed', value: '', values: [], weights: [], exclude_indices: [] },
    validationResult: null,
    showJsonModal: false,

    pattern: {
      schema_version: '1.1',
      pattern_id: '',
      pattern_name: '',
      description: '',
      linked_survey_id: '',
      created_at: '',
      uid_pool: [],
      uid_strategy: 'sequential',
      answers: {},
      timing: {
        min_total_seconds: 90,
        max_total_seconds: 240,
        page_delay_min: 3.0,
        page_delay_max: 8.0,
        typing_delay_per_char_ms: [50, 150],
      },
      branch_path: [],
      branch_ids_used: [],
      auto_generated_from_mapping: false,
      requires_branch_match: false,
    },

    async init() {
      await this.loadMeta();
    },

    async loadMeta() {
      const [mapsRes, patternsRes] = await Promise.all([
        fetch('/api/mapper/maps'),
        fetch('/api/config/patterns'),
      ]);
      this.maps = await mapsRes.json();
      this.patterns = await patternsRes.json();
    },

    async loadQuestions() {
      if (!this.selectedSurveyId) return;
      const res = await fetch(`/api/config/survey/${this.selectedSurveyId}/questions`);
      if (!res.ok) { showToast('Could not load questions', 'error'); return; }
      this.allQuestions = await res.json();

      // Group by page_id
      const groups = {};
      for (const q of this.allQuestions) {
        if (!groups[q.page_id]) groups[q.page_id] = { page_id: q.page_id, questions: [] };
        groups[q.page_id].questions.push(q);
      }
      this.groupedQuestions = Object.values(groups).sort((a, b) =>
        a.questions[0].page_index - b.questions[0].page_index
      );

      this.pattern.linked_survey_id = this.selectedSurveyId;
      this.validationResult = null;
    },

    selectQuestion(q) {
      this.activeQuestion = q;
      this.activeQId = q.q_id;
      // Load existing strategy from pattern if present
      const existing = this.pattern.answers[q.page_id]?.[q.q_id];
      if (existing) {
        this.activeStrategy = { ...existing };
      } else {
        this.activeStrategy = { strategy: 'fixed', value: '', values: [], weights: [], exclude_indices: [] };
      }
    },

    applyStrategy() {
      if (!this.activeQuestion) return;
      const { page_id, q_id } = this.activeQuestion;
      if (!this.pattern.answers[page_id]) this.pattern.answers[page_id] = {};
      this.pattern.answers[page_id][q_id] = { ...this.activeStrategy };
      showToast(`Strategy applied to ${q_id}`);
    },

    newPattern() {
      this.pattern = {
        schema_version: '1.1',
        pattern_id: '',
        pattern_name: '',
        description: '',
        linked_survey_id: this.selectedSurveyId || '',
        created_at: '',
        uid_pool: [],
        uid_strategy: 'sequential',
        answers: {},
        timing: {
          min_total_seconds: 90,
          max_total_seconds: 240,
          page_delay_min: 3.0,
          page_delay_max: 8.0,
          typing_delay_per_char_ms: [50, 150],
        },
        branch_path: [],
        branch_ids_used: [],
        auto_generated_from_mapping: false,
        requires_branch_match: false,
      };
      this.validationResult = null;
      this.activeQuestion = null;
    },

    async savePattern() {
      if (!this.pattern.pattern_name.trim()) {
        showToast('Pattern name is required', 'warning');
        return;
      }
      const method = this.pattern.pattern_id ? 'PUT' : 'POST';
      const url = this.pattern.pattern_id
        ? `/api/config/patterns/${this.pattern.pattern_id}`
        : '/api/config/patterns';

      const res = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(this.pattern),
      });
      const data = await res.json();
      if (!res.ok) { showToast('Save failed: ' + (data.error || ''), 'error'); return; }
      if (data.pattern_id) this.pattern.pattern_id = data.pattern_id;
      showToast('Pattern saved: ' + this.pattern.pattern_id);
      await this.loadMeta();
    },

    async validatePattern() {
      if (!this.pattern.pattern_id || !this.selectedSurveyId) {
        showToast('Save pattern and select a survey first', 'warning');
        return;
      }
      const res = await fetch(`/api/config/patterns/${this.pattern.pattern_id}/validate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ survey_id: this.selectedSurveyId }),
      });
      this.validationResult = await res.json();
    },
  };
}
