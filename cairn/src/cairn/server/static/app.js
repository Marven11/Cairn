function cairnApp() {
  let dispatcherEditor = null;
  let dispatcherLineSeparator = '\n';
  return {
    view: 'list',
    projects: [],
    project: null,
    selectedProjectId: '',
    polling: true,
    pollTimer: null,
    cy: null,
    sideTab: 'detail',
    selectedNode: null,
    selectedFacts: [],
    selectedTimelineEntryId: null,
    layoutMode: 'dagre_tb',
    sidePanelWidth: 320,
    isResizingPanel: false,

    showNewProject: false,
    showIntentModal: false,
    showConcludeModal: false,
    showCompleteModal: false,
    showHintModal: false,
    showAddGoalModal: false,
    showRenameModal: false,
    showLocalPrefs: false,
    showSettings: false,
    showDispatchers: false,
    showYamlModal: false,
    exportTab: 'yaml',
    exportProjectId: '',
    showDeleteModal: false,

    newProject: { title: '', origin: '', goal: '', bootstrap: true, hints: [{ content: '' }] },
    intentForm: { description: '' },
    concludeForm: { description: '', intentId: '' },
    completeForm: { description: '', goals: [] },
    hintForm: { content: '' },
    addGoalForm: { projectId: '', projectTitle: '', description: '', reopens: false },
    renameForm: { projectId: '', originalTitle: '', title: '' },
    localPrefs: { actor_name: 'Human', layout_mode: 'dagre_tb' },
    settingsForm: { intent_timeout: 5, reason_timeout: 5, dispatcher_heartbeat_timeout: 45 },
    settingsTab: 'server',
    runtimeDispatchers: null,
    dispatchersBusy: false,
    dispatcherEntries: null,
    dispatcherSelectedName: null,
    dispatcherName: '',
    dispatcherYaml: '',
    dispatcherSavedYaml: '',
    dispatcherBusy: false,
    dispatcherError: '',
    deleteConfirm: { id: '', title: '' },
    isDeletingProject: false,
    yamlPreviewTitle: '',
    yamlPreviewText: '',
    yamlPreviewHtml: '',
    toast: { show: false, message: '', type: 'info' },
    replay: {
      active: false,
      playing: false,
      stepMs: '1600',
      frameIndex: -1,
      frames: [],
      visibleEvents: [],
      sourceProject: null,
      timer: null,
    },
    _timelineEventsCacheProject: null,
    _timelineEventsCache: [],
    _centerAnimation: null,
    projectListScrollTop: 0,
    shouldRestoreProjectListScroll: false,
    isStoppingAllProjects: false,

    async init() {
      this._panelResizeMove = (e) => this.onPanelResize(e);
      this._panelResizeStop = () => this.stopPanelResize();
      window.addEventListener('pointermove', this._panelResizeMove);
      window.addEventListener('pointerup', this._panelResizeStop);
      this.loadLocalPrefs();
      await this.loadProjects();
      await this.loadSettings();
      this.startPolling();
      window.addEventListener('hashchange', () => this.handleRoute());
      this.handleRoute();
    },

    handleRoute() {
      const hash = location.hash || '#/';
      const m = hash.match(/^#\/projects\/(.+)$/);
      if (m) {
        const id = m[1];
        if (this.selectedProjectId !== id || this.view !== 'graph') {
          this.openProject(id);
        }
      } else {
        if (this.view !== 'list') this.backToList(true);
      }
    },

    async api(method, path, body) {
      const opts = { method, headers: { 'Content-Type': 'application/json' } };
      if (body) opts.body = JSON.stringify(body);
      const r = await fetch(path, opts);
      if (r.status === 204) return null;
      let data;
      try { data = await r.json(); } catch { throw new Error(`HTTP ${r.status}: invalid server response`); }
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        if (typeof data.detail === 'string') msg = data.detail;
        else if (Array.isArray(data.detail)) msg = data.detail.map(e => `${e.loc.length ? e.loc.join('.') : 'config'}: ${e.msg}`).join('; ');
        throw new Error(msg);
      }
      return data;
    },

    async fetchText(path) {
      const r = await fetch(path);
      const text = await r.text();
      if (!r.ok) {
        let detail = text;
        try {
          const data = JSON.parse(text);
          detail = data.detail || text;
        } catch {}
        throw new Error(detail || `HTTP ${r.status}`);
      }
      return text;
    },

    showToast(msg, type = 'info') {
      this.toast = { show: true, message: msg, type };
      setTimeout(() => this.toast.show = false, 3000);
    },

    cloneData(value) {
      try {
        return JSON.parse(JSON.stringify(value));
      } catch (error) {
        if (typeof structuredClone === 'function') return structuredClone(value);
        throw error;
      }
    },

    isValidLayoutMode(mode) {
      return ['dagre_tb', 'dagre_lr', 'klay_tb', 'klay_lr', 'elk_tb', 'elk_lr'].includes(mode);
    },

    layoutEngine(mode = this.layoutMode) {
      if (mode.startsWith('elk')) return 'elk';
      return mode.startsWith('klay') ? 'klay' : 'dagre';
    },

    layoutDirection(mode = this.layoutMode) {
      return mode.endsWith('_lr') ? 'LR' : 'TB';
    },

    loadLocalPrefs() {
      try {
        const raw = localStorage.getItem('cairn.localPrefs');
        if (!raw) {
          this.localPrefs.actor_name = 'Human';
        } else {
          const parsed = JSON.parse(raw);
          if (typeof parsed.actor_name === 'string') this.localPrefs.actor_name = parsed.actor_name;
          if (this.isValidLayoutMode(parsed.layout_mode)) {
            this.localPrefs.layout_mode = parsed.layout_mode;
          } else if (parsed.layout_dir === 'TB' || parsed.layout_dir === 'LR') {
            this.localPrefs.layout_mode = parsed.layout_dir === 'LR' ? 'dagre_lr' : 'dagre_tb';
          }
        }
        const rawPanelWidth = localStorage.getItem('cairn.sidePanelWidth');
        if (rawPanelWidth !== null) {
          const savedPanelWidth = Number(rawPanelWidth);
          if (Number.isFinite(savedPanelWidth)) this.sidePanelWidth = savedPanelWidth;
        }
      } catch (e) {
        console.error(e);
      }
      if (!this.localPrefs.actor_name.trim()) this.localPrefs.actor_name = 'Human';
      if (!this.isValidLayoutMode(this.localPrefs.layout_mode)) this.localPrefs.layout_mode = 'dagre_tb';
      this.layoutMode = this.localPrefs.layout_mode;
    },

    saveLocalPrefs() {
      try {
        localStorage.setItem('cairn.localPrefs', JSON.stringify(this.localPrefs));
      } catch (e) {
        console.error(e);
      }
    },

    saveSidePanelWidth() {
      try {
        localStorage.setItem('cairn.sidePanelWidth', String(this.sidePanelWidth));
      } catch (e) {
        console.error(e);
      }
    },

    actorName() {
      return this.localPrefs.actor_name.trim() || 'Human';
    },

    projectStatusBadgeClass(status) {
      const classes = {
        active: 'bg-teal-50 text-teal-600',
        stopped: 'bg-amber-50 text-amber-700',
        completed: 'bg-slate-100 text-slate-500',
        halted: 'bg-rose-50 text-rose-600',
      };
      return classes[status] || 'bg-slate-100 text-slate-500';
    },

    isStoppableStatus(status) {
      return status === 'active' || status === 'halted';
    },

    dispatcherOptions(current) {
      const options = this.runtimeDispatchers ?? [];
      if (!options.some(d => d.id === current.id)) return [current, ...options];
      return options;
    },

    dispatcherOptionLabel(d) {
      return `${d.name} · ${d.hostname}${d.online ? '' : ' (offline)'}`;
    },

    reasonBadgeText(reason, compact = false) {
      if (!reason) return '';
      if (compact) return `Reason · ${reason.worker}`;
      const trigger = reason.trigger ? ` (${reason.trigger})` : '';
      return `Reasoning · ${reason.worker}${trigger}`;
    },

    workingIntentBadgeText(count) {
      return `Exploring · ${count}`;
    },

    isBootstrapIntent(intent) {
      return Boolean(
        intent
        && intent.description === 'bootstrap'
        && intent.creator === 'dispatcher.bootstrap'
        && Array.isArray(intent.from)
        && intent.from.length === 1
        && intent.from[0] === 'origin'
        && this.isOpenIntent(intent),
      );
    },

    isOpenIntent(intent) {
      return Boolean(intent) && intent.to.length === 0;
    },

    isGoalId(factId) {
      return /^g\d+$/.test(String(factId || ''));
    },

    goalFacts() {
      if (!this.project) return [];
      return this.project.facts.filter(fact => this.isGoalId(fact.id));
    },

    completedGoalIds() {
      if (!this.project) return new Set();
      const completed = new Set();
      for (const intent of this.project.intents) {
        for (const target of intent.to) {
          if (this.isGoalId(target)) completed.add(target);
        }
      }
      return completed;
    },

    isGoalCompleted(goalId) {
      return this.completedGoalIds().has(goalId);
    },

    openGoalFacts() {
      const completed = this.completedGoalIds();
      return this.goalFacts().filter(goal => !completed.has(goal.id));
    },

    goalProgressLabel() {
      if (!this.project) return '';
      const goals = this.goalFacts();
      if (goals.length === 0) return '0 goals';
      const completed = this.completedGoalIds();
      const done = goals.filter(goal => completed.has(goal.id)).length;
      return `${done}/${goals.length} goals`;
    },

    factBadgeClass(factId) {
      if (factId === 'origin') return 'bg-teal-50 text-teal-700';
      if (this.isGoalId(factId)) {
        return this.isGoalCompleted(factId) ? 'bg-rose-100 text-rose-400' : 'bg-rose-50 text-rose-600';
      }
      return 'bg-brand-50 text-brand-700';
    },

    openIntentNodeType(intent) {
      if (this.isBootstrapIntent(intent)) return intent.worker ? 'bootstrap_running' : 'bootstrap_pending';
      return intent.worker ? 'in_progress' : 'unclaimed';
    },

    openIntentNodeLabel(intent) {
      return this.isBootstrapIntent(intent) ? 'Bootstrap' : '?';
    },

    openIntentNodeSize(intent) {
      if (this.isBootstrapIntent(intent)) return { width: 82, height: 30 };
      return { width: 22, height: 22 };
    },

    projectAcceptsGraphWrites() {
      return !this.replay.active && ['active', 'halted'].includes(this.project?.project?.status);
    },

    countProjectsByStatus(status) {
      return this.projects.filter(project => project.status === status).length;
    },

    hasStoppableProjects() {
      return this.projects.some(project => this.isStoppableStatus(project.status));
    },

    projectCanWriteHints() {
      return !this.replay.active && ['active', 'halted', 'stopped', 'completed'].includes(this.project?.project?.status);
    },

    canActOnSelectedFacts() {
      return this.projectAcceptsGraphWrites() && this.selectedFacts.length > 0;
    },

    async loadProjects() {
      try { this.projects = await this.api('GET', '/projects'); } catch(e) { console.error(e); }
    },

    rememberProjectListScroll() {
      const scroller = this.$refs.projectListScroll;
      if (!scroller) return;
      this.projectListScrollTop = scroller.scrollTop;
    },

    restoreProjectListScroll() {
      if (!this.shouldRestoreProjectListScroll) return;
      this.$nextTick(() => {
        const scroller = this.$refs.projectListScroll;
        if (!scroller) return;
        const maxScrollTop = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
        scroller.scrollTop = Math.min(this.projectListScrollTop, maxScrollTop);
        this.shouldRestoreProjectListScroll = false;
      });
    },

    escapeHtml(text) {
      return String(text ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    },

    highlightYamlScalar(value) {
      const escaped = this.escapeHtml(value);
      if (!value.trim()) return escaped;
      if (/^\s*#.*$/.test(value)) return `<span style="color:#64748b">${escaped}</span>`;
      if (/^\s*['"].*['"]\s*$/.test(value)) return `<span style="color:#15803d">${escaped}</span>`;
      if (/^\s*\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})\s*$/.test(value)) return `<span style="color:#b45309">${escaped}</span>`;
      if (/^\s*(true|false|null|~)\s*$/i.test(value)) return `<span style="color:#b91c1c">${escaped}</span>`;
      if (/^\s*-?\d+(\.\d+)?\s*$/.test(value)) return `<span style="color:#0f766e">${escaped}</span>`;
      if (/^\s*(origin|goal|g\d+|f\d+|i\d+)\s*$/i.test(value)) return `<span style="color:#6d28d9">${escaped}</span>`;
      return `<span style="color:#0f172a">${escaped}</span>`;
    },

    highlightYamlLine(line) {
      if (/^\s*$/.test(line)) return '';
      if (/^\s*#/.test(line)) return `<span style="color:#64748b">${this.escapeHtml(line)}</span>`;

      const listKeyMatch = line.match(/^(\s*-\s+)([^:#\n][^:]*):(.*)$/);
      if (listKeyMatch) {
        const [, prefix, key, rest] = listKeyMatch;
        return `${this.escapeHtml(prefix)}<span style="color:#7dd3fc">${this.escapeHtml(key)}</span>:${this.highlightYamlScalar(rest)}`;
      }

      const keyMatch = line.match(/^(\s*)([^:#\n][^:]*):(.*)$/);
      if (keyMatch) {
        const [, indent, key, rest] = keyMatch;
        return `${this.escapeHtml(indent)}<span style="color:#7dd3fc">${this.escapeHtml(key)}</span>:${this.highlightYamlScalar(rest)}`;
      }

      const listValueMatch = line.match(/^(\s*-\s+)(.*)$/);
      if (listValueMatch) {
        const [, prefix, rest] = listValueMatch;
        return `${this.escapeHtml(prefix)}${this.highlightYamlScalar(rest)}`;
      }

      return this.highlightYamlScalar(line);
    },

    yamlSectionTint(sectionName) {
      const tints = {
        project: { header: '#eff6ff', body: '#fafcff', itemA: '#f3f8ff', itemB: '#edf5ff' },
        hints: { header: '#fffbeb', body: '#fffef8', itemA: '#fffaf0', itemB: '#fff6e8' },
        facts: { header: '#eef2ff', body: '#fafaff', itemA: '#f5f7ff', itemB: '#eef3ff' },
        intents: { header: '#ecfdf5', body: '#f8fdfb', itemA: '#f1fbf5', itemB: '#eaf8ef' },
      };
      return tints[sectionName] || { header: '#f8fafc', body: '#ffffff', itemA: '#f8fafc', itemB: '#f1f5f9' };
    },

    async copyYamlPreview() {
      if (!this.yamlPreviewText) return;
      try {
        await this.copyText(this.yamlPreviewText);
        this.showToast('Copied');
      } catch {
        this.showToast('Copy failed', 'error');
      }
    },

    async copyText(text) {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        return;
      }
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.setAttribute('readonly', '');
      textarea.style.position = 'fixed';
      textarea.style.top = '0';
      textarea.style.left = '-9999px';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      textarea.setSelectionRange(0, textarea.value.length);
      try {
        if (!document.execCommand('copy')) {
          throw new Error('copy command rejected');
        }
      } finally {
        document.body.removeChild(textarea);
      }
    },

    highlightYaml(text) {
      const lines = String(text ?? '').split('\n');
      let currentSection = '';
      let activeItemIndent = null;
      let activeItemStripe = 0;
      let nextItemStripe = 0;

      return lines.map((line) => {
        const topLevelMatch = line.match(/^([A-Za-z_][A-Za-z0-9_-]*):\s*$/);
        if (topLevelMatch) {
          currentSection = topLevelMatch[1];
          activeItemIndent = null;
          activeItemStripe = 0;
          nextItemStripe = 0;
        }

        const tint = this.yamlSectionTint(currentSection);
        const indent = (line.match(/^(\s*)/) || ['',''])[1].length;
        const isBlank = /^\s*$/.test(line);
        const isListItem = /^(\s*)-\s+/.test(line);
        const isTopLevelItem = isListItem && indent === 0;

        if (isTopLevelItem) {
          activeItemIndent = indent;
          activeItemStripe = nextItemStripe;
          nextItemStripe = nextItemStripe === 0 ? 1 : 0;
        } else if (!isBlank && activeItemIndent !== null && indent === 0) {
          activeItemIndent = null;
        }

        const isNestedListItem = isListItem && activeItemIndent !== null && indent > activeItemIndent;
        const isItemContinuation = !isListItem && !isBlank && activeItemIndent !== null && indent > activeItemIndent;
        const isBlankWithinItem = isBlank && activeItemIndent !== null;
        const isItemLine = isTopLevelItem || isNestedListItem || isItemContinuation || isBlankWithinItem;
        const lineHtml = isBlank ? '&nbsp;' : this.highlightYamlLine(line);
        let background = currentSection ? tint.body : 'transparent';
        let fontWeight = '400';

        if (topLevelMatch) {
          background = tint.header;
          fontWeight = '700';
        } else if (isItemLine) {
          background = activeItemStripe === 0 ? tint.itemA : tint.itemB;
        }

        return `<div style="white-space:pre;padding:0 16px;background:${background};font-weight:${fontWeight};color:#0f172a">${lineHtml}</div>`;
      }).join('');
    },

    highlightTimeline(text) {
      const lines = String(text ?? '').split('\n');
      const stripes = ['#fffbf5', '#fef5ee'];
      let blockIndex = -1;
      return lines.map((line) => {
        if (/^\[/.test(line)) blockIndex++;
        const bg = blockIndex < 0 ? stripes[0] : stripes[blockIndex % 2];
        const isBlank = /^\s*$/.test(line);
        return `<div style="white-space:pre;padding:0 16px;background:${bg};color:#0f172a">${isBlank ? '&nbsp;' : this.escapeHtml(line)}</div>`;
      }).join('');
    },

    projectLabel(projectId, projectTitle) {
      return projectTitle ? `${projectId} - ${projectTitle}` : projectId;
    },

    deleteConfirmLabel() {
      return this.projectLabel(this.deleteConfirm.id, this.deleteConfirm.title);
    },

    requestDeleteProject(projectId, projectTitle = '') {
      if (!projectId) return;
      this.deleteConfirm = { id: projectId, title: projectTitle };
      this.showDeleteModal = true;
    },

    closeDeleteModal(force = false) {
      if (this.isDeletingProject && !force) return;
      this.showDeleteModal = false;
      this.deleteConfirm = { id: '', title: '' };
    },

    async viewProjectYaml(projectId, projectTitle = '') {
      if (!projectId) return;
      this.exportProjectId = projectId;
      this.yamlPreviewTitle = this.projectLabel(projectId, projectTitle);
      this.exportTab = 'yaml';
      await this.loadExportTab('yaml');
      this.showYamlModal = true;
    },

    async switchExportTab(tab) {
      if (tab === this.exportTab) return;
      this.exportTab = tab;
      await this.loadExportTab(tab);
    },

    async loadExportTab(tab) {
      if (!this.exportProjectId) return;
      try {
        const text = await this.fetchText(`/projects/${this.exportProjectId}/export?format=${tab}`);
        this.yamlPreviewText = text;
        this.yamlPreviewHtml = tab === 'yaml' ? this.highlightYaml(text) : this.highlightTimeline(text);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    deleteProject() {
      if (!this.selectedProjectId) return;
      this.requestDeleteProject(this.selectedProjectId, this.project?.project?.title || '');
    },

    deleteProjectById(projectId, projectTitle = '') {
      this.requestDeleteProject(projectId, projectTitle);
    },

    async confirmDeleteProject() {
      const projectId = this.deleteConfirm.id;
      if (!projectId || this.isDeletingProject) return;
      try {
        this.isDeletingProject = true;
        await this.api('DELETE', `/projects/${projectId}`);
        this.closeDeleteModal(true);
        if (this.selectedProjectId === projectId && this.view === 'graph') {
          this.backToList();
        } else {
          await this.loadProjects();
        }
        this.showToast(`Deleted ${projectId}`);
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.isDeletingProject = false;
        if (!this.showDeleteModal) this.deleteConfirm = { id: '', title: '' };
      }
    },

    async openSettings() {
      this.showSettings = true;
      this.settingsTab = 'server';
      await this.loadSettings();
    },

    closeSettings() {
      if (this.dispatcherBusy) return;
      if (this.dispatcherDirty() && !confirm('Discard unsaved dispatcher changes?')) return;
      this.setDispatcherYaml(this.dispatcherSavedYaml);
      this.dispatcherName = this.dispatcherSelectedName ?? '';
      this.dispatcherError = '';
      this.showSettings = false;
    },

    dispatcherDirty() {
      return this.dispatcherYaml !== this.dispatcherSavedYaml || this.dispatcherName !== (this.dispatcherSelectedName ?? '');
    },

    setDispatcherYaml(source) {
      dispatcherLineSeparator = source.match(/\r\n|\r|\n/)?.[0] ?? '\n';
      this.dispatcherYaml = source;
      if (dispatcherEditor) dispatcherEditor.setValue(source);
    },

    setDispatcherBusy(busy) {
      this.dispatcherBusy = busy;
      if (dispatcherEditor) dispatcherEditor.setOption('readOnly', busy);
    },

    async openDispatcherSettings() {
      this.settingsTab = 'dispatchers';
      await this.$nextTick();
      if (!dispatcherEditor) {
        dispatcherEditor = CodeMirror.fromTextArea(this.$refs.dispatcherEditor, {
          mode: 'yaml', lineNumbers: true, indentUnit: 2, tabSize: 2,
          indentWithTabs: false, lineWrapping: true,
          extraKeys: { Tab: cm => cm.replaceSelection('  ') },
        });
        dispatcherEditor.getWrapperElement().style.height = 'min(48vh, 480px)';
        dispatcherEditor.on('change', (cm, change) => {
          if (change.origin !== 'setValue') this.dispatcherYaml = cm.getValue(dispatcherLineSeparator);
        });
        dispatcherEditor.setValue(this.dispatcherYaml);
      }
      dispatcherEditor.refresh();
      if (this.dispatcherEntries !== null || this.dispatcherBusy) return;
      this.setDispatcherBusy(true);
      this.dispatcherError = '';
      try {
        const entries = await this.api('GET', '/dispatchers');
        this.dispatcherEntries = entries;
        await this.$nextTick();
        const name = entries.find(entry => entry.error === null)?.name ?? null;
        const data = name !== null ? await this.api('GET', `/dispatchers/${encodeURIComponent(name)}/yaml`) : { yaml: '' };
        this.dispatcherSelectedName = name;
        this.dispatcherName = name ?? '';
        this.setDispatcherYaml(data.yaml);
        this.dispatcherSavedYaml = data.yaml;
      } catch (e) {
        this.dispatcherError = e.message;
      } finally {
        this.setDispatcherBusy(false);
      }
    },

    async selectDispatcher(name) {
      if (this.dispatcherBusy || name === this.dispatcherSelectedName) return;
      if (this.dispatcherDirty() && !confirm('Discard unsaved dispatcher changes?')) return;
      this.setDispatcherBusy(true);
      this.dispatcherError = '';
      try {
        const data = name !== null ? await this.api('GET', `/dispatchers/${encodeURIComponent(name)}/yaml`) : { yaml: '' };
        this.dispatcherSelectedName = name;
        this.dispatcherName = name ?? '';
        this.setDispatcherYaml(data.yaml);
        this.dispatcherSavedYaml = data.yaml;
      } catch (e) {
        this.dispatcherError = e.message;
      } finally {
        this.setDispatcherBusy(false);
      }
    },

    async saveDispatcher() {
      if (this.dispatcherBusy || this.dispatcherEntries === null) return;
      if (!/^[a-zA-Z0-9_-]+$/.test(this.dispatcherName)) {
        this.dispatcherError = 'Use letters, digits, underscores or hyphens in the name.';
        return;
      }
      this.setDispatcherBusy(true);
      this.dispatcherError = '';
      try {
        const name = this.dispatcherName;
        await this.api(this.dispatcherSelectedName !== null ? 'PUT' : 'POST', `/dispatchers/${encodeURIComponent(name)}`, { yaml: this.dispatcherYaml });
        if (!this.dispatcherEntries.some(entry => entry.name === name)) {
          this.dispatcherEntries = [...this.dispatcherEntries, { name, error: null }].sort((a, b) => a.name.localeCompare(b.name));
          await this.$nextTick();
        }
        this.dispatcherSelectedName = name;
        this.dispatcherSavedYaml = this.dispatcherYaml;
        this.showToast('Configuration saved. Restart the dispatcher to apply changes.');
      } catch (e) {
        this.dispatcherError = e.message;
      } finally {
        this.setDispatcherBusy(false);
      }
    },

    async deleteDispatcher() {
      if (this.dispatcherBusy || this.dispatcherSelectedName === null) return;
      if (!confirm(`Delete configuration "${this.dispatcherSelectedName}"?`)) return;
      this.setDispatcherBusy(true);
      this.dispatcherError = '';
      try {
        await this.api('DELETE', `/dispatchers/${encodeURIComponent(this.dispatcherSelectedName)}`);
        this.dispatcherEntries = this.dispatcherEntries.filter(entry => entry.name !== this.dispatcherSelectedName);
        this.dispatcherSelectedName = null;
        this.dispatcherName = '';
        this.setDispatcherYaml('');
        this.dispatcherSavedYaml = '';
        this.showToast('Dispatcher configuration deleted');
      } catch (e) {
        this.dispatcherError = e.message;
      } finally {
        this.setDispatcherBusy(false);
      }
    },

    async loadSettings() {
      try {
        const s = await this.api('GET', '/settings');
        this.settingsForm.intent_timeout = s.intent_timeout;
        this.settingsForm.reason_timeout = s.reason_timeout;
        this.settingsForm.dispatcher_heartbeat_timeout = s.dispatcher_heartbeat_timeout;
      } catch(e) { console.error(e); }
    },

    async openDispatchers() {
      this.showDispatchers = true;
      await this.loadRuntimeDispatchers();
    },

    closeDispatchers() {
      this.showDispatchers = false;
    },

    async loadRuntimeDispatchers() {
      this.dispatchersBusy = true;
      try {
        this.runtimeDispatchers = await this.api('GET', '/runtime/dispatchers');
      } catch (e) {
        this.showToast(e.message, 'error');
        this.runtimeDispatchers = null;
      } finally {
        this.dispatchersBusy = false;
      }
    },

    async changeProjectDispatcher(projectId, dispatcherId) {
      try {
        const result = await this.api('PUT', `/runtime/projects/${encodeURIComponent(projectId)}/dispatcher`, { dispatcher_id: dispatcherId });
        this.showToast(`Switched to dispatcher ${result.dispatcher.name}`);
        await this.loadProjects();
        if (this.selectedProjectId === projectId && this.view === 'graph') {
          await this.loadProject(projectId);
        }
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async loadProject(id) {
      try { this.project = await this.api('GET', `/projects/${id}`); } catch(e) { this.showToast(e.message, 'error'); }
    },

    async toggleProjectStop() {
      if (!this.selectedProjectId || !this.project || this.project.project.status === 'completed') return;
      const nextStatus = this.isStoppableStatus(this.project.project.status) ? 'stopped' : 'active';
      try {
        await this.setProjectStatus(this.selectedProjectId, nextStatus);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async toggleProjectStopById(projectId, currentStatus) {
      if (!projectId || currentStatus === 'completed') return;
      const nextStatus = this.isStoppableStatus(currentStatus) ? 'stopped' : 'active';
      try {
        await this.setProjectStatus(projectId, nextStatus);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async setProjectStatus(projectId, status, options = {}) {
      const { reload = true, toast = true } = options;
      const updated = await this.api('PUT', `/projects/${projectId}/status`, { status });
      const projectSummary = this.projects.find(item => item.id === projectId);
      if (projectSummary) {
        projectSummary.status = updated.status;
        projectSummary.reason = updated.reason;
      }
      if (this.selectedProjectId === projectId && this.project) {
        this.project.project.status = updated.status;
        this.project.project.reason = updated.reason;
        if (!this.projectAcceptsGraphWrites()) {
          this.showIntentModal = false;
          this.showConcludeModal = false;
          this.showCompleteModal = false;
        }
        if (!this.projectCanWriteHints()) {
          this.showHintModal = false;
        }
      }
      if (reload) await this.loadProjects();
      if (toast) this.showToast(status === 'stopped' ? 'Project stopped' : 'Project resumed');
      return updated;
    },

    async stopAllStoppableProjects() {
      const stoppableProjects = this.projects.filter(project => this.isStoppableStatus(project.status));
      if (stoppableProjects.length === 0 || this.isStoppingAllProjects) return;
      this.isStoppingAllProjects = true;
      try {
        const results = await Promise.allSettled(
          stoppableProjects.map(project => this.setProjectStatus(project.id, 'stopped', { reload: false, toast: false }))
        );
        await this.loadProjects();
        const successCount = results.filter(result => result.status === 'fulfilled').length;
        const failureCount = results.length - successCount;
        if (failureCount === 0) {
          this.showToast(`Stopped ${successCount} active project${successCount === 1 ? '' : 's'}`);
        } else {
          this.showToast(`Stopped ${successCount} projects, ${failureCount} failed`, 'error');
        }
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.isStoppingAllProjects = false;
      }
    },

    openAddGoal(projectId, projectTitle = '') {
      if (!projectId) return;
      const status = this.project && this.project.project.id === projectId
        ? this.project.project.status
        : (this.projects.find(p => p.id === projectId)?.status || '');
      this.addGoalForm = { projectId, projectTitle, description: '', reopens: status === 'completed' };
      this.showAddGoalModal = true;
    },

    async addGoal() {
      const projectId = this.addGoalForm.projectId || this.selectedProjectId;
      if (!projectId) return;
      const wasCompleted = this.addGoalForm.reopens;
      try {
        const actor = this.actorName();
        const result = await this.api('POST', `/projects/${projectId}/goals`, {
          description: this.addGoalForm.description,
          creator: actor,
        });
        this.showAddGoalModal = false;
        this.addGoalForm = { projectId: '', projectTitle: '', description: '', reopens: false };
        await this.loadProjects();
        if (this.selectedProjectId === projectId && this.view === 'graph') {
          await this.loadProject(projectId);
          this.updateGraph();
        }
        this.showToast(wasCompleted ? 'Goal added — project reopened' : 'Goal added');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    openRenameProject(projectId, projectTitle = '') {
      if (!projectId) return;
      this.renameForm = { projectId, originalTitle: projectTitle, title: projectTitle };
      this.showRenameModal = true;
      this.$nextTick(() => {
        this.$refs.renameTitleInput?.focus();
        this.$refs.renameTitleInput?.select?.();
      });
    },

    async renameProject() {
      const projectId = this.renameForm.projectId || this.selectedProjectId;
      if (!projectId) return;
      try {
        const updated = await this.api('PUT', `/projects/${projectId}/title`, {
          title: this.renameForm.title,
        });
        const projectSummary = this.projects.find(item => item.id === projectId);
        if (projectSummary) {
          projectSummary.title = updated.title;
        }
        if (this.selectedProjectId === projectId && this.project) {
          this.project.project.title = updated.title;
        }
        this.showRenameModal = false;
        this.renameForm = { projectId: '', originalTitle: '', title: '' };
        await this.loadProjects();
        this.showToast('Project renamed');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    replayProgressLabel() {
      if (!this.replay.active || this.replay.frames.length === 0) return 'Replay';
      return `Replay ${Math.min(this.replay.frameIndex + 1, this.replay.frames.length)} / ${this.replay.frames.length}`;
    },

    stopReplayTimer() {
      if (!this.replay.timer) return;
      clearTimeout(this.replay.timer);
      this.replay.timer = null;
    },

    updateReplaySpeed() {
      if (!this.replay.active || !this.replay.playing) return;
      this.stopReplayTimer();
      this.scheduleReplayTick();
    },

    replayEventWeight(event) {
      const type = event?.type;
      if (type === 'reason_started') return 1.6;
      if (type === 'intent_declared') return 1.05;
      if (type === 'intent_running') return 0.75;
      if (type === 'goal_completed') return 1.4;
      if (type === 'intent_concluded' || type === 'project_completed') return 1.6;
      if (type === 'project_created' || type === 'hint_added' || type === 'goal_declared') return 0.8;
      return 1.0;
    },

    replayEventDurationMs(event) {
      const base = Number(this.replay.stepMs) || 1100;
      return Math.round(base * this.replayEventWeight(event));
    },

    replayTimelineElapsedDurationMs(events, index) {
      let elapsed = 0;
      for (let i = 0; i < index; i += 1) {
        elapsed += this.replayEventDurationMs(events[i]);
      }
      return elapsed;
    },

    scheduleReplayTick() {
      if (!this.replay.active || !this.replay.playing) return;
      if (this.replay.frameIndex >= this.replay.frames.length - 1) {
        this.replay.playing = false;
        return;
      }
      this.stopReplayTimer();
      const currentFrame = this.replay.frames[this.replay.frameIndex];
      const delay = this.replayEventDurationMs(currentFrame?.event);
      this.replay.timer = setTimeout(() => this.advanceProjectReplay(), delay);
    },

    buildInitialReplayProject(sourceProject) {
      const origin = sourceProject.facts.find(fact => fact.id === 'origin');
      return {
        project: {
          ...this.cloneData(sourceProject.project),
          status: 'active',
          reason: null,
        },
        facts: origin ? [this.cloneData(origin)] : [],
        intents: [],
        hints: [],
      };
    },

    buildReplayFrames(sourceProject, baseEvents) {
      const sourceIntents = new Map(sourceProject.intents.map(intent => [intent.id, intent]));
      const replayEvents = [];

      for (const event of baseEvents) {
        if (event.type !== 'intent_declared' || !event.intentId) {
          replayEvents.push(this.cloneData(event));
          continue;
        }

        const sourceIntent = sourceIntents.get(event.intentId);
        replayEvents.push({
          id: `reason-started-${event.intentId}`,
          type: 'reason_started',
          timestamp: sourceIntent?.created_at || event.timestamp,
          actor: sourceIntent?.creator || event.actor || 'reasoner',
          title: sourceIntent?.description || event.title,
          meta: [
            sourceIntent?.id || event.intentId,
            `from ${(sourceIntent?.from || event.sourceFactIds || []).join(', ')}`,
          ],
          targetType: 'reason',
          targetId: sourceIntent?.id || event.intentId,
          order: `${event.order}.reason`,
          intentId: sourceIntent?.id || event.intentId,
          producedFactId: null,
          sourceFactIds: [...(sourceIntent?.from || event.sourceFactIds || [])],
        });
        replayEvents.push(this.cloneData(event));
        if (!sourceIntent?.worker) continue;
        replayEvents.push({
          id: `intent-running-${sourceIntent.id}`,
          type: 'intent_running',
          timestamp: sourceIntent.last_heartbeat_at || sourceIntent.created_at,
          actor: sourceIntent.worker,
          title: sourceIntent.description,
          meta: [sourceIntent.id, `worker ${sourceIntent.worker}`],
          targetType: 'intent',
          targetId: sourceIntent.id,
          order: `${event.order}.run`,
          intentId: sourceIntent.id,
          producedFactId: null,
          sourceFactIds: [...sourceIntent.from],
        });
      }

      const replayProject = this.buildInitialReplayProject(sourceProject);
      const frames = [];
      for (const event of replayEvents) {
        this.applyReplayEvent(replayProject, sourceProject, event);
        frames.push({
          event: this.cloneData(event),
          project: this.cloneData(replayProject),
        });
      }

      if (frames.length > 0) {
        frames[frames.length - 1].project.project.status = sourceProject.project.status;
        frames[frames.length - 1].project.project.reason = this.cloneData(sourceProject.project.reason);
        frames[frames.length - 1].project.project.completed_at = sourceProject.project.completed_at ?? null;
      }
      return frames;
    },

    applyReplayEvent(replayProject, sourceProject, event) {
      if (!replayProject || !sourceProject || !event) return;
      const sourceIntent = event.intentId
        ? sourceProject.intents.find(intent => intent.id === event.intentId) || null
        : null;

      if (event.type === 'project_created') {
        replayProject.project.title = sourceProject.project.title;
        replayProject.project.status = 'active';
        return;
      }

      if (event.type === 'hint_added') {
        const hint = sourceProject.hints.find(item => item.id === event.targetId);
        if (hint && !replayProject.hints.some(item => item.id === hint.id)) {
          replayProject.hints.push(this.cloneData(hint));
        }
        return;
      }

      if (event.type === 'goal_declared') {
        const goal = sourceProject.facts.find(item => item.id === event.targetId);
        if (goal && !replayProject.facts.some(item => item.id === goal.id)) {
          replayProject.facts.push(this.cloneData(goal));
        }
        if (replayProject.project.status === 'completed') {
          replayProject.project.status = 'active';
          replayProject.project.completed_at = null;
        }
        return;
      }

      if (event.type === 'reason_started') {
        replayProject.project.reason = {
          worker: event.actor || 'reasoner',
          trigger: 'new_facts',
          started_at: event.timestamp,
          last_heartbeat_at: event.timestamp,
        };
        return;
      }

      if (event.type === 'intent_declared') {
        if (!sourceIntent) return;
        replayProject.project.reason = null;
        if (!replayProject.intents.some(intent => intent.id === sourceIntent.id)) {
          replayProject.intents.push({
            id: sourceIntent.id,
            from: [...sourceIntent.from],
            to: [],
            description: sourceIntent.description,
            creator: sourceIntent.creator,
            worker: null,
            last_heartbeat_at: null,
            created_at: sourceIntent.created_at,
            concluded_at: null,
          });
        }
        return;
      }

      if (event.type === 'intent_running') {
        if (!sourceIntent) return;
        const replayIntent = replayProject.intents.find(intent => intent.id === sourceIntent.id);
        if (!replayIntent) return;
        replayIntent.worker = sourceIntent.worker || sourceIntent.creator;
        replayIntent.last_heartbeat_at = sourceIntent.last_heartbeat_at || sourceIntent.created_at;
        return;
      }

      if (event.type !== 'intent_concluded' && event.type !== 'project_completed' && event.type !== 'goal_completed') return;
      if (!sourceIntent) return;

      const replayIntent = replayProject.intents.find(intent => intent.id === sourceIntent.id);
      if (replayIntent) {
        replayIntent.to = [...sourceIntent.to];
        replayIntent.worker = sourceIntent.worker || sourceIntent.creator;
        replayIntent.last_heartbeat_at = sourceIntent.last_heartbeat_at;
        replayIntent.concluded_at = sourceIntent.concluded_at;
      }

      if (event.type === 'project_completed') {
        replayProject.project.status = 'completed';
        replayProject.project.completed_at = sourceIntent.concluded_at;
        return;
      }

      if (event.type === 'goal_completed') return;

      const producedFactId = sourceIntent.to.find(target => !this.isGoalId(target));
      const producedFact = sourceProject.facts.find(fact => fact.id === producedFactId);
      if (producedFact && !replayProject.facts.some(fact => fact.id === producedFact.id)) {
        replayProject.facts.push(this.cloneData(producedFact));
      }
    },

    applyReplayFrame(frameIndex, options = {}) {
      if (!this.replay.active) return;
      const { reinitialize = false } = options;
      const frame = this.replay.frames[frameIndex];
      if (!frame) return;

      this.replay.frameIndex = frameIndex;
      this.project = this.cloneData(frame.project);
      this.replay.visibleEvents = this.replay.frames
        .slice(0, frameIndex + 1)
        .map(item => this.cloneData(item.event))
        .filter(entry => entry.type !== 'reason_started')
        .map((entry, index, list) => ({ ...entry, isLast: index === list.length - 1 }));
      this._timelineEventsCacheProject = null;
      this._timelineEventsCache = [];

      if (reinitialize) {
        this.teardownAutoFit();
        if (this.cy) {
          this.cy.destroy();
          this.cy = null;
        }
        this.$nextTick(() => {
          this.initGraph();
          this.followReplayTimelineTail();
        });
        return;
      }

      this.updateGraph();
      this.followReplayTimelineTail();
    },

    async startProjectReplay() {
      if (!this.project || this.replay.active || !this.selectedProjectId) return;
      try {
        const sourceProject = await this.api('GET', `/projects/${this.selectedProjectId}`);
        const baseEvents = this.timelineEvents().map(event => ({
          ...event,
          meta: [...(event.meta || [])],
          sourceFactIds: [...(event.sourceFactIds || [])],
        }));
        const frames = this.buildReplayFrames(sourceProject, baseEvents);
        if (frames.length === 0) {
          this.showToast('No timeline to replay', 'error');
          return;
        }

        const stepMs = String(this.replay.stepMs || '1100');
        this.stopReplayTimer();
        this.replay = {
          active: true,
          playing: true,
          stepMs,
          frameIndex: -1,
          frames,
          visibleEvents: [],
          sourceProject,
          timer: null,
        };
        this.polling = false;
        this.sideTab = 'log';
        this.selectedNode = null;
        this.selectedFacts = [];
        this.selectedTimelineEntryId = null;
        this.applyReplayFrame(0, { reinitialize: true });
        this.scheduleReplayTick();
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    advanceProjectReplay() {
      if (!this.replay.active) return;
      if (this.replay.frameIndex >= this.replay.frames.length - 1) {
        this.replay.playing = false;
        this.stopReplayTimer();
        return;
      }
      this.applyReplayFrame(this.replay.frameIndex + 1);
      this.scheduleReplayTick();
    },

    toggleProjectReplayPlayback() {
      if (!this.replay.active) return;
      if (this.replay.playing) {
        this.replay.playing = false;
        this.stopReplayTimer();
        return;
      }
      if (this.replay.frameIndex >= this.replay.frames.length - 1) {
        this.restartProjectReplay();
        return;
      }
      this.replay.playing = true;
      this.scheduleReplayTick();
    },

    restartProjectReplay() {
      if (!this.replay.active || this.replay.frames.length === 0) return;
      this.stopReplayTimer();
      this.replay.playing = true;
      this.selectedNode = null;
      this.selectedFacts = [];
      this.selectedTimelineEntryId = null;
      this.applyReplayFrame(0, { reinitialize: true });
      this.scheduleReplayTick();
    },

    async exitProjectReplay() {
      if (!this.replay.active) return;
      const projectId = this.selectedProjectId;
      const stepMs = String(this.replay.stepMs || '1100');
      this.stopReplayTimer();
      this.replay = {
        active: false,
        playing: false,
        stepMs,
        frameIndex: -1,
        frames: [],
        visibleEvents: [],
        sourceProject: null,
        timer: null,
      };
      this._timelineEventsCacheProject = null;
      this._timelineEventsCache = [];
      this.polling = true;
      if (!projectId) return;
      await this.loadProject(projectId);
      this.$nextTick(() => {
        this.teardownAutoFit();
        if (this.cy) {
          this.cy.destroy();
          this.cy = null;
        }
        this.initGraph();
      });
    },

    async openProject(id) {
      if (this.replay.active) {
        this.stopReplayTimer();
        this.replay.active = false;
        this.replay.playing = false;
        this.replay.frames = [];
        this.replay.visibleEvents = [];
        this.replay.frameIndex = -1;
        this.replay.sourceProject = null;
        this._timelineEventsCacheProject = null;
        this._timelineEventsCache = [];
        this.polling = true;
      }
      this.rememberProjectListScroll();
      this.selectedProjectId = id;
      this.selectedNode = null;
      this.selectedFacts = [];
      this.selectedTimelineEntryId = null;
      if (this.runtimeDispatchers === null) {
        this.loadRuntimeDispatchers();
      }
      await this.loadProject(id);
      this.view = 'graph';
      if (location.hash !== `#/projects/${id}`) location.hash = `/projects/${id}`;
      this.$nextTick(() => {
        const clampedWidth = this.clampPanelWidth(this.sidePanelWidth);
        if (clampedWidth !== this.sidePanelWidth) {
          this.sidePanelWidth = clampedWidth;
          this.saveSidePanelWidth();
        }
        this.teardownAutoFit();
        if (this.cy) { this.cy.destroy(); this.cy = null; }
        this.initGraph();
      });
    },

    backToList(fromRoute) {
      if (this.replay.active) {
        this.stopReplayTimer();
        this.replay.active = false;
        this.replay.playing = false;
        this.replay.frames = [];
        this.replay.visibleEvents = [];
        this.replay.frameIndex = -1;
        this.replay.sourceProject = null;
        this._timelineEventsCacheProject = null;
        this._timelineEventsCache = [];
        this.polling = true;
      }
      this.view = 'list';
      this.shouldRestoreProjectListScroll = true;
      this.project = null;
      this.selectedProjectId = '';
      this.selectedNode = null;
      this.selectedFacts = [];
      this.selectedTimelineEntryId = null;
      this.teardownAutoFit();
      if (this.cy) { this.cy.destroy(); this.cy = null; }
      if (!fromRoute) location.hash = '/';
      this.loadProjects().then(() => this.restoreProjectListScroll());
    },

    startPolling() {
      this.pollTimer = setInterval(async () => {
        if (!this.polling) return;
        if (this.selectedProjectId && this.view === 'graph') {
          await this.loadProject(this.selectedProjectId);
          this.updateGraph();
        } else {
          await this.loadProjects();
        }
        if (!this.dispatchersBusy) await this.loadRuntimeDispatchers();
      }, 5000);
    },

    buildElements() {
      const nodes = [];
      const edges = [];
      for (const f of this.project.facts) {
        let nodeType = 'fact';
        if (f.id === 'origin') nodeType = 'origin';
        else if (this.isGoalId(f.id)) nodeType = this.isGoalCompleted(f.id) ? 'goal_done' : 'goal';
        const label = this.summarizeFactLabel(f);
        const size = this.factNodeSize(label, nodeType);
        nodes.push({ data: {
          id: f.id,
          label,
          description: f.description,
          nodeType,
          width: size.width,
          height: size.height,
        }});
      }
      for (const intent of this.project.intents) {
        const lbl = intent.description;
        if (intent.to.length > 0) {
          for (const src of intent.from) {
            for (const target of intent.to) {
              edges.push({ data: { id: `${intent.id}_${src}_${target}`, source: src, target, intentId: intent.id, label: lbl, status: 'concluded' }});
            }
          }
        } else {
          const phId = `_ph_${intent.id}`;
          const nodeSize = this.openIntentNodeSize(intent);
          const nodeType = this.openIntentNodeType(intent);
          nodes.push({ data: {
            id: phId,
            label: this.openIntentNodeLabel(intent),
            description: intent.description,
            nodeType,
            intentId: intent.id,
            width: nodeSize.width,
            height: nodeSize.height,
          }});
          for (const src of intent.from) {
            edges.push({ data: { id: `${intent.id}_${src}`, source: src, target: phId, intentId: intent.id, label: lbl, status: nodeType }});
          }
          if (this.isBootstrapIntent(intent)) {
            for (const goal of this.openGoalFacts()) {
              edges.push({ data: { id: `${intent.id}_${goal.id}_scope`, source: phId, target: goal.id, intentId: intent.id, label: '', status: nodeType, edgeType: 'bootstrap_scope' }});
            }
          }
        }
      }
      return { nodes, edges };
    },

    initGraph() {
      const container = document.getElementById('cy');
      if (!container) return;
      const { nodes, edges } = this.buildElements();
      const rawCy = cytoscape({
        container,
        elements: [...nodes, ...edges],
        style: this.graphStyles(),
        layout: this.layoutOpts(),
        minZoom: 0.15, maxZoom: 3.5,
      });
      this.cy = rawCy;
      const self = this;
      rawCy.on('tap', 'node', e => self.onNodeTap(e));
      rawCy.on('tap', 'edge', e => self.onEdgeTap(e));
      rawCy.on('tap', e => { if (e.target === rawCy) self.clearSelection(); });
      this.pulseActive();
      this.setupAutoFit();
    },

    graphStyles() {
      const common = { 'text-valign':'center','text-halign':'center','font-family':'-apple-system,BlinkMacSystemFont,Inter,sans-serif' };
      return [
        { selector: 'node[nodeType="origin"]', style: { ...common, shape:'round-rectangle','background-color':'#14b8a6',label:'data(label)',color:'#fff','font-size':'11px','font-weight':'bold','text-wrap':'wrap','text-max-width':'92px','text-overflow-wrap':'anywhere',width:'data(width)',height:'data(height)','border-width':0 }},
        { selector: 'node[nodeType="goal"]', style: { ...common, shape:'round-rectangle','background-color':'#f43f5e',label:'data(label)',color:'#fff','font-size':'11px','font-weight':'bold','text-wrap':'wrap','text-max-width':'92px','text-overflow-wrap':'anywhere',width:'data(width)',height:'data(height)','border-width':0 }},
        { selector: 'node[nodeType="goal_done"]', style: { ...common, shape:'round-rectangle','background-color':'#fda4af',label:'data(label)',color:'#fff','font-size':'11px','font-weight':'bold','text-wrap':'wrap','text-max-width':'92px','text-overflow-wrap':'anywhere',width:'data(width)',height:'data(height)','border-width':0 }},
        { selector: 'node[nodeType="fact"]', style: { ...common, shape:'round-rectangle','background-color':'#6366f1',label:'data(label)',color:'#fff','font-size':'10px','font-weight':'bold','text-wrap':'wrap','text-max-width':'116px','text-overflow-wrap':'anywhere',width:'data(width)',height:'data(height)','border-width':0 }},
        { selector: 'node[nodeType="in_progress"]', style: { ...common, shape:'ellipse','background-color':'#f59e0b','background-opacity':0.8,label:'?',color:'#fff','font-size':'11px','font-weight':'bold',width:22,height:22,'border-width':2,'border-color':'#d97706' }},
        { selector: 'node[nodeType="unclaimed"]', style: { ...common, shape:'ellipse','background-color':'#cbd5e1','background-opacity':0.5,label:'?',color:'#94a3b8','font-size':'11px','font-weight':'bold',width:20,height:20,'border-width':1.5,'border-color':'#94a3b8','border-style':'dashed' }},
        { selector: 'node[nodeType="bootstrap_pending"]', style: { ...common, shape:'round-rectangle','background-color':'#fff7ed','background-opacity':0.96,label:'data(label)',color:'#c2410c','font-size':'10px','font-weight':'bold',width:'data(width)',height:'data(height)','border-width':1.5,'border-color':'#fdba74','border-style':'dashed','text-wrap':'wrap','text-max-width':'70px' }},
        { selector: 'node[nodeType="bootstrap_running"]', style: { ...common, shape:'round-rectangle','background-color':'#fb923c','background-opacity':0.96,label:'data(label)',color:'#fff7ed','font-size':'10px','font-weight':'bold',width:'data(width)',height:'data(height)','border-width':2,'border-color':'#ea580c','text-wrap':'wrap','text-max-width':'70px' }},

        { selector: 'edge[status="concluded"]', style: { width:2,'line-color':'#6ee7b7','target-arrow-color':'#6ee7b7','target-arrow-shape':'triangle','curve-style':'bezier',label:'data(label)','font-size':'7px',color:'#94a3b8','text-rotation':'autorotate','text-margin-y':-9,'text-max-width':'80px','text-wrap':'ellipsis','text-background-color':'#f8fafc','text-background-opacity':0.85,'text-background-padding':'2px','text-events':'yes','arrow-scale':0.9 }},
        { selector: 'edge[status="in_progress"]', style: { width:2,'line-color':'#fbbf24','line-style':'dashed','line-dash-pattern':[8,4],'line-dash-offset':0,'target-arrow-color':'#fbbf24','target-arrow-shape':'triangle','curve-style':'bezier',label:'data(label)','font-size':'7px',color:'#b45309','text-rotation':'autorotate','text-margin-y':-9,'text-max-width':'80px','text-wrap':'ellipsis','text-background-color':'#fffbeb','text-background-opacity':0.85,'text-background-padding':'2px','text-events':'yes','arrow-scale':0.9 }},
        { selector: 'edge[status="unclaimed"]', style: { width:1.5,'line-color':'#cbd5e1','line-style':'dashed','line-dash-pattern':[5,5],'target-arrow-color':'#cbd5e1','target-arrow-shape':'triangle','curve-style':'bezier',label:'data(label)','font-size':'7px',color:'#94a3b8','text-rotation':'autorotate','text-margin-y':-9,'text-max-width':'80px','text-wrap':'ellipsis','text-background-color':'#f8fafc','text-background-opacity':0.85,'text-background-padding':'2px','text-events':'yes','arrow-scale':0.7 }},
        { selector: 'edge[status="bootstrap_pending"]', style: { width:2,'line-color':'#fdba74','line-style':'dashed','line-dash-pattern':[8,4],'line-dash-offset':0,'target-arrow-color':'#fdba74','target-arrow-shape':'triangle','curve-style':'bezier',label:'data(label)','font-size':'7px',color:'#c2410c','text-rotation':'autorotate','text-margin-y':-9,'text-max-width':'88px','text-wrap':'ellipsis','text-background-color':'#fff7ed','text-background-opacity':0.92,'text-background-padding':'2px','text-events':'yes','arrow-scale':0.85 }},
        { selector: 'edge[status="bootstrap_running"]', style: { width:2.5,'line-color':'#fb923c','line-style':'dashed','line-dash-pattern':[10,4],'line-dash-offset':0,'target-arrow-color':'#fb923c','target-arrow-shape':'triangle','curve-style':'bezier',label:'data(label)','font-size':'7px',color:'#c2410c','text-rotation':'autorotate','text-margin-y':-9,'text-max-width':'88px','text-wrap':'ellipsis','text-background-color':'#fff7ed','text-background-opacity':0.92,'text-background-padding':'2px','text-events':'yes','arrow-scale':0.95 }},
        { selector: 'edge[edgeType="bootstrap_scope"]', style: { label:'',width:1.8,'curve-style':'bezier','line-style':'dotted','line-dash-pattern':[2,5],'target-arrow-shape':'triangle-backcurve','arrow-scale':0.75,'target-distance-from-node':2 }},

        { selector: '.highlight', style: { 'z-index':999 }},
        { selector: 'edge.highlight', style: { 'z-index':999 }},
        { selector: 'node.focus', style: { 'border-width':3,'border-color':'#312e81','border-opacity':0.95,'z-index':1000 }},
        { selector: 'edge.focus', style: { 'z-index':1000,'overlay-color':'#93c5fd','overlay-opacity':0.22,'overlay-padding':5 }},
        { selector: 'node.selected-fact', style: { 'border-width':0,'underlay-color':'#93c5fd','underlay-padding':8,'underlay-opacity':0.28,'z-index':1001 }},
        { selector: '.faded', style: { opacity:0.5 }},
        { selector: '.fresh', style: { opacity:0 }},
      ];
    },

    layoutOpts(animate = true) {
      const direction = this.layoutDirection();
      if (this.layoutEngine() === 'elk') {
        const elkDirection = direction === 'TB' ? 'DOWN' : 'RIGHT';
        return {
          name: 'elk',
          fit: true,
          padding: 50,
          animate,
          animationDuration: 350,
          animationEasing: 'ease-in-out-cubic',
          elk: {
            algorithm: 'layered',
            'elk.direction': elkDirection,
            'elk.aspectRatio': '1.5',
            'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
            'elk.spacing.nodeNode': '50',
            'elk.layered.spacing.nodeNodeBetweenLayers': '80',
            'elk.spacing.edgeNode': '25',
            'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
            'elk.layered.nodePlacement.bk.fixedAlignment': 'BALANCED',
          },
        };
      }
      if (this.layoutEngine() === 'klay') {
        const isHorizontal = direction === 'LR';
        return {
          name: 'klay',
          fit: true,
          padding: 50,
          animate,
          animationDuration: 400,
          animationEasing: 'ease-in-out-cubic',
          klay: {
            direction: direction === 'TB' ? 'DOWN' : 'RIGHT',
            edgeRouting: 'POLYLINE',
            crossingMinimization: 'LAYER_SWEEP',
            nodeLayering: 'NETWORK_SIMPLEX',
            nodePlacement: 'BRANDES_KOEPF',
            separateConnectedComponents: true,
            spacing: isHorizontal ? 52 : 40,
            inLayerSpacingFactor: isHorizontal ? 1.15 : 1.0,
            thoroughness: 8,
          },
        };
      }
      return {
        name: 'dagre',
        rankDir: direction,
        nodeSep: 60,
        rankSep: 80,
        padding: 50,
        fit: true,
        animate,
        animationDuration: 400,
        animationEasing: 'ease-in-out-cubic',
      };
    },

    snapshotNodePositions() {
      const positions = new Map();
      if (!this.cy) return positions;
      this.cy.nodes().forEach(node => {
        positions.set(node.id(), { x: node.position('x'), y: node.position('y') });
      });
      return positions;
    },

    anchorPositionFromIds(nodeIds, previousPositions, offset = 36) {
      const anchors = [];
      for (const nodeId of nodeIds) {
        const existing = this.cy?.getElementById(nodeId);
        if (existing?.length) {
          anchors.push({ x: existing.position('x'), y: existing.position('y') });
          continue;
        }
        const previous = previousPositions.get(nodeId);
        if (previous) anchors.push(previous);
      }
      if (anchors.length === 0) return null;
      const center = anchors.reduce((acc, pos) => ({ x: acc.x + pos.x, y: acc.y + pos.y }), { x: 0, y: 0 });
      const avg = { x: center.x / anchors.length, y: center.y / anchors.length };
      return this.layoutDirection() === 'TB'
        ? { x: avg.x, y: avg.y + offset }
        : { x: avg.x + offset, y: avg.y };
    },

    initialPositionForNode(nodeData, previousPositions) {
      if (!this.project) return null;
      if (nodeData.intentId) {
        const previousPlaceholder = previousPositions.get(nodeData.id);
        if (previousPlaceholder) return previousPlaceholder;
        const intent = this.project.intents.find(item => item.id === nodeData.intentId);
        return intent ? this.anchorPositionFromIds(intent.from, previousPositions, 32) : null;
      }

      const producingIntent = this.project.intents.find(item => item.to.includes(nodeData.id));
      if (!producingIntent) return null;
      const placeholderPosition = previousPositions.get(`_ph_${producingIntent.id}`);
      if (placeholderPosition) return placeholderPosition;
      return this.anchorPositionFromIds(producingIntent.from, previousPositions, 44);
    },

    fadeInFreshElement(element) {
      if (!element || element.length === 0) return;
      element.addClass('fresh');
      setTimeout(() => {
        if (!element.inside()) return;
        element.animate(
          { style: { opacity: 1 } },
          {
            duration: 500,
            complete: () => {
              if (!element.inside()) return;
              element.removeStyle('opacity');
            },
          },
        );
        element.removeClass('fresh');
      }, 30);
    },

    updateGraph() {
      if (!this.cy || !this.project) return;
      const { nodes, edges } = this.buildElements();
      const wantNodes = new Set(nodes.map(n => n.data.id));
      const wantEdges = new Set(edges.map(e => e.data.id));
      const previousPositions = this.snapshotNodePositions();
      let changed = false;

      this.cy.nodes().forEach(n => { if (!wantNodes.has(n.id())) { n.remove(); changed = true; } });
      this.cy.edges().forEach(e => { if (!wantEdges.has(e.id())) { e.remove(); changed = true; } });

      for (const n of nodes) {
        const ex = this.cy.getElementById(n.data.id);
        if (ex.length === 0) {
          const initialPosition = this.initialPositionForNode(n.data, previousPositions);
          const a = this.cy.add(initialPosition ? { ...n, position: initialPosition } : n);
          this.fadeInFreshElement(a);
          changed = true;
        } else if (
          ex.data('nodeType') !== n.data.nodeType ||
          ex.data('label') !== n.data.label ||
          ex.data('description') !== n.data.description ||
          ex.data('width') !== n.data.width ||
          ex.data('height') !== n.data.height
        ) {
          ex.data(n.data); changed = true;
        }
      }
      for (const e of edges) {
        const ex = this.cy.getElementById(e.data.id);
        if (ex.length === 0) {
          const a = this.cy.add(e);
          this.fadeInFreshElement(a);
          changed = true;
        } else if (ex.data('status') !== e.data.status) {
          ex.data(e.data);
        }
      }
      if (changed) this.cy.layout(this.layoutOpts()).run();
      this.pulseActive();
      this.refreshGraphDecorations();
    },

    pulseActive() {
      if (!this.cy) return;
      this.cy.nodes('[nodeType="in_progress"], [nodeType="bootstrap_running"]').forEach(node => {
        if (node.scratch('_pulseActive')) return;
        node.scratch('_pulseActive', true);
        const isBootstrap = node.data('nodeType') === 'bootstrap_running';
        const minOpacity = isBootstrap ? 0.55 : 0.35;
        const maxOpacity = isBootstrap ? 0.98 : 0.8;
        const pulseOut = () => {
          if (!node.inside()) {
            node.removeScratch('_pulseActive');
            return;
          }
          node.animate({ style:{'background-opacity':minOpacity} }, { duration:900, complete: pulseIn });
        };
        const pulseIn = () => {
          if (!node.inside()) {
            node.removeScratch('_pulseActive');
            return;
          }
          node.animate({ style:{'background-opacity':maxOpacity} }, { duration:900, complete: pulseOut });
        };
        pulseOut();
      });
      this.cy.edges('[status="in_progress"], [status="bootstrap_running"]').forEach(edge => {
        if (edge.scratch('_flowActive')) return;
        edge.scratch('_flowActive', true);
        const isBootstrap = edge.data('status') === 'bootstrap_running';
        const dashOffset = isBootstrap ? -16 : -12;
        const flow = () => {
          if (!edge.inside() || !['in_progress', 'bootstrap_running'].includes(edge.data('status'))) {
            edge.removeScratch('_flowActive');
            return;
          }
          edge.animate({ style:{'line-dash-offset':dashOffset} }, {
            duration:isBootstrap ? 620 : 700,
            complete:() => {
              if (!edge.inside() || !['in_progress', 'bootstrap_running'].includes(edge.data('status'))) {
                edge.removeScratch('_flowActive');
                return;
              }
              edge.style('line-dash-offset', 0);
              flow();
            }
          });
        };
        flow();
      });
    },

    fitGraph() { if (this.cy) this.cy.fit(undefined, 50); },

    centerGraphOnElements(eles) {
      if (!this.cy || !eles || eles.length === 0) return;
      if (this._centerAnimation) this._centerAnimation.stop();
      this._centerAnimation = this.cy.animation({ center: { eles }, duration: 220, easing: 'ease-in-out-cubic' });
      this._centerAnimation.play();
    },

    centerGraphOnFact(factId) {
      if (!this.cy || !factId) return;
      const node = this.cy.getElementById(factId);
      if (node.length > 0) this.centerGraphOnElements(node);
    },

    centerGraphOnIntent(intentId) {
      if (!this.cy || !intentId) return;
      const edges = this.cy.edges(`[intentId="${intentId}"]`);
      if (edges.length === 0) return;
      this.centerGraphOnElements(edges.add(edges.sources()).add(edges.targets()));
    },

    summarizeFactLabel(fact) {
      if (fact.id === 'origin') return 'Origin';
      const normalized = fact.description.replace(/\s+/g, ' ').trim();
      const chars = Array.from(normalized);
      if (chars.length <= 24) return normalized || fact.id;
      return `${chars.slice(0, 24).join('')}…`;
    },

    factNodeSize(label, nodeType) {
      const preset = nodeType === 'fact'
        ? { fontSize: 10, maxTextWidth: 116, minWidth: 52, minHeight: 34, paddingX: 10, paddingY: 10 }
        : { fontSize: 11, maxTextWidth: 92, minWidth: 58, minHeight: 38, paddingX: 10, paddingY: 10 };

      const measured = this.measureWrappedText(label, preset.maxTextWidth, preset.fontSize);
      return {
        width: Math.max(preset.minWidth, Math.ceil(measured.width + preset.paddingX * 2)),
        height: Math.max(preset.minHeight, Math.ceil(measured.height + preset.paddingY * 2)),
      };
    },

    measureWrappedText(text, maxWidth, fontSize) {
      const content = (text || '').trim() || ' ';
      const lines = [];
      let currentWidth = 0;
      let currentChars = 0;
      let maxLineWidth = 0;

      const pushLine = () => {
        if (currentChars === 0 && lines.length > 0) lines.push(0);
        else if (currentChars > 0) lines.push(currentWidth);
        maxLineWidth = Math.max(maxLineWidth, currentWidth);
        currentWidth = 0;
        currentChars = 0;
      };

      for (const char of Array.from(content)) {
        if (char === '\n') {
          pushLine();
          continue;
        }
        const charWidth = this.estimateLabelCharWidth(char, fontSize);
        if (currentChars > 0 && currentWidth + charWidth > maxWidth) pushLine();
        currentWidth += charWidth;
        currentChars += 1;
      }

      pushLine();

      const lineCount = Math.max(1, lines.length);
      const lineHeight = fontSize * 1.35;
      return {
        width: Math.min(maxWidth, Math.max(fontSize * 1.6, maxLineWidth)),
        height: lineCount * lineHeight,
      };
    },

    estimateLabelCharWidth(char, fontSize) {
      if (/\s/.test(char)) return fontSize * 0.35;
      if (/[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE10-\uFE6F\uFF00-\uFF60\uFFE0-\uFFE6]/.test(char)) {
        return fontSize * 1.0;
      }
      return fontSize * 0.58;
    },

    setupAutoFit() {
      this.teardownAutoFit();
      const container = document.getElementById('cy');
      if (!container || !this.cy) return;
      let fitTimer = null;
      this._resizeObserver = new ResizeObserver(() => {
        clearTimeout(fitTimer);
        fitTimer = setTimeout(() => {
          if (this.cy) {
            const clampedWidth = this.clampPanelWidth(this.sidePanelWidth);
            if (clampedWidth !== this.sidePanelWidth) {
              this.sidePanelWidth = clampedWidth;
              this.saveSidePanelWidth();
            }
            this.cy.resize();
            this.cy.fit(undefined, 50);
          }
        }, 200);
      });
      this._resizeObserver.observe(container);
    },

    teardownAutoFit() {
      if (this._resizeObserver) {
        this._resizeObserver.disconnect();
        this._resizeObserver = null;
      }
    },
    applySelectedLayout() {
      this.layoutMode = this.isValidLayoutMode(this.layoutMode) ? this.layoutMode : 'dagre_tb';
      this.localPrefs.layout_mode = this.layoutMode;
      this.saveLocalPrefs();
      if (this.cy) this.cy.layout(this.layoutOpts()).run();
    },

    clampPanelWidth(width) {
      const containerWidth = document.getElementById('graphLayout')?.getBoundingClientRect().width || window.innerWidth;
      const min = 260;
      const max = Math.max(min, containerWidth - 260);
      return Math.min(max, Math.max(min, width));
    },

    startPanelResize(e) {
      e.currentTarget?.setPointerCapture?.(e.pointerId);
      this.isResizingPanel = true;
      this.onPanelResize(e);
    },

    onPanelResize(e) {
      if (!this.isResizingPanel) return;
      const rect = document.getElementById('graphLayout')?.getBoundingClientRect();
      if (!rect) return;
      this.sidePanelWidth = this.clampPanelWidth(rect.right - e.clientX);
    },

    stopPanelResize() {
      if (!this.isResizingPanel) return;
      this.isResizingPanel = false;
      this.saveSidePanelWidth();
    },

    clearGraphSelection(preserveTimeline = false) {
      this.selectedNode = null;
      this.selectedFacts = [];
      if (!preserveTimeline) this.selectedTimelineEntryId = null;
      if (this.cy) this.cy.elements().removeClass('highlight focus faded selected-fact');
    },

    clearSelection() {
      this.clearGraphSelection(false);
    },

    toggleFactSelection(fid) {
      this.selectedTimelineEntryId = null;
      const idx = this.selectedFacts.indexOf(fid);
      if (idx >= 0) {
        this.selectedFacts.splice(idx, 1);
        if (this.selectedFacts.length === 0) {
          this.clearSelection();
          return;
        }
        if (this.selectedNode?.type === 'fact' && this.selectedNode.id === fid) {
          this.selectedNode = { type:'fact', id: this.selectedFacts[this.selectedFacts.length - 1] };
        }
      } else {
        this.selectedFacts.push(fid);
        this.selectedNode = { type:'fact', id: fid };
      }
      this.refreshGraphDecorations();
    },

    removeFactSelection(fid) {
      this.selectedTimelineEntryId = null;
      const idx = this.selectedFacts.indexOf(fid);
      if (idx < 0) return;
      this.selectedFacts.splice(idx, 1);
      if (this.selectedFacts.length === 0) {
        this.clearSelection();
        return;
      }
      if (this.selectedNode?.type === 'fact' && this.selectedNode.id === fid) {
        this.selectedNode = { type:'fact', id: this.selectedFacts[this.selectedFacts.length - 1] };
      }
      this.refreshGraphDecorations();
    },

    onNodeTap(e) {
      const node = e.target;
      const id = node.data('id');
      const nt = node.data('nodeType');
      const keepLogFocus = this.sideTab === 'log';

      if (['in_progress', 'unclaimed', 'bootstrap_pending', 'bootstrap_running'].includes(nt)) {
        this.selectIntent(node.data('intentId'));
        if (keepLogFocus) this.scrollTimelineToSelection();
        else this.sideTab = 'detail';
        return;
      }
      if (e.originalEvent.shiftKey) {
        this.toggleFactSelection(id);
        if (keepLogFocus) this.scrollTimelineToSelection();
        else this.sideTab = 'detail';
        return;
      } else {
        this.selectedFacts = [id];
        this.selectFact(id);
      }
      if (keepLogFocus) this.scrollTimelineToSelection();
      else this.sideTab = 'detail';
    },

    onEdgeTap(e) {
      this.selectIntent(e.target.data('intentId'));
      if (this.sideTab === 'log') this.scrollTimelineToSelection();
      else this.sideTab = 'detail';
    },

    selectIntent(intent) {
      const intentId = typeof intent === 'string' ? intent : intent?.id;
      if (!intentId) return;
      this.selectedFacts = [];
      this.selectedTimelineEntryId = null;
      this.selectedNode = { type:'intent', id: intentId };
      this.applyLineageHighlightForIntent(intentId);
    },

    selectFact(fact) {
      const factId = typeof fact === 'string' ? fact : fact?.id;
      if (!factId) return;
      if (!this.selectedFacts.includes(factId)) this.selectedFacts = [factId];
      this.selectedTimelineEntryId = null;
      this.selectedNode = { type:'fact', id: factId };
      if (this.selectedFacts.length > 1) {
        this.applyMultiFactSelectionHighlight();
        return;
      }
      this.applyLineageHighlightForFact(factId);
    },

    refreshGraphDecorations() {
      if (!this.selectedNode) {
        this.syncFactSelections();
        return;
      }
      if (this.selectedNode.type === 'intent') this.applyLineageHighlightForIntent(this.selectedNode.id);
      if (this.selectedNode.type === 'fact') {
        if (this.selectedFacts.length > 1 && this.selectedFacts.includes(this.selectedNode.id)) {
          this.applyMultiFactSelectionHighlight();
          return;
        }
        this.applyLineageHighlightForFact(this.selectedNode.id);
      }
    },

    syncFactSelections() {
      if (!this.cy) return;
      this.cy.nodes().removeClass('selected-fact');
      for (const fid of this.selectedFacts) {
        const node = this.cy.getElementById(fid);
        if (node.length > 0) node.addClass('selected-fact');
      }
    },

    collectFactLineage(fid) {
      const upstreamFacts = new Set();
      const upstreamIntents = new Set();

      const walkFactUpstream = (factId) => {
        if (upstreamFacts.has(factId) || !this.project) return;
        upstreamFacts.add(factId);
        for (const intent of this.project.intents) {
          if (intent.to.includes(factId)) walkIntentUpstream(intent.id);
        }
      };

      const walkIntentUpstream = (iid) => {
        if (upstreamIntents.has(iid) || !this.project) return;
        upstreamIntents.add(iid);
        const intent = this.project.intents.find(i => i.id === iid);
        if (!intent) return;
        for (const sourceId of intent.from) walkFactUpstream(sourceId);
      };

      walkFactUpstream(fid);
      return { upstreamFacts, upstreamIntents };
    },

    collectIntentElements(intent, nodeIds, edgeIds) {
      if (intent.to.length > 0) {
        for (const target of intent.to) nodeIds.add(target);
        for (const sourceId of intent.from) {
          for (const target of intent.to) {
            edgeIds.add(`${intent.id}_${sourceId}_${target}`);
          }
        }
      } else {
        nodeIds.add(`_ph_${intent.id}`);
        for (const sourceId of intent.from) {
          edgeIds.add(`${intent.id}_${sourceId}`);
        }
        if (this.isBootstrapIntent(intent)) {
          for (const goal of this.openGoalFacts()) {
            nodeIds.add(goal.id);
            edgeIds.add(`${intent.id}_${goal.id}_scope`);
          }
        }
      }
    },

    applyLineageHighlightForIntent(intentId) {
      if (!this.cy || !this.project) return;
      const intent = this.project.intents.find(i => i.id === intentId);
      if (!intent) return;
      this.cy.elements().removeClass('highlight focus faded');

      const nodeIds = new Set();
      const edgeIds = new Set();
      this.collectIntentElements(intent, nodeIds, edgeIds);
      const highlightNodes = this.cy.nodes().filter(n => nodeIds.has(n.id()));
      const focusEdges = this.cy.edges().filter(e => edgeIds.has(e.id()));

      highlightNodes.addClass('highlight');
      focusEdges.addClass('focus');

      const visible = highlightNodes.add(focusEdges);
      this.cy.elements().not(visible).addClass('faded');
      this.syncFactSelections();
    },

    applyLineageHighlightForFact(factId) {
      if (!this.cy || !this.project) return;
      const { upstreamFacts, upstreamIntents } = this.collectFactLineage(factId);
      const nodeIds = new Set(upstreamFacts);
      const edgeIds = new Set();

      for (const iid of upstreamIntents) {
        const intent = this.project.intents.find(i => i.id === iid);
        if (intent) this.collectIntentElements(intent, nodeIds, edgeIds);
      }

      this.cy.elements().removeClass('highlight focus faded');
      const highlightNodes = this.cy.nodes().filter(n => nodeIds.has(n.id()));
      const highlightEdges = this.cy.edges().filter(e => edgeIds.has(e.id()));
      const focusNode = this.cy.getElementById(factId);

      highlightNodes.addClass('highlight');
      highlightEdges.addClass('highlight');
      focusNode.addClass('focus');

      const visible = highlightNodes.add(highlightEdges).add(focusNode);
      this.cy.elements().not(visible).addClass('faded');
      this.syncFactSelections();
    },

    applyMultiFactSelectionHighlight() {
      if (!this.cy) return;
      this.cy.elements().removeClass('highlight focus faded selected-fact');
      this.syncFactSelections();
    },

    getProducingIntent(fid) {
      if (!this.project) return null;
      return this.project.intents.find(i => i.to.includes(fid)) || null;
    },

    selectedFactId() {
      return this.selectedNode?.type === 'fact' ? this.selectedNode.id : null;
    },

    selectedFactRecord() {
      const factId = this.selectedFactId();
      if (!this.project || !factId) return null;
      return this.project.facts.find(f => f.id === factId) || null;
    },

    selectedFactProducingIntent() {
      const factId = this.selectedFactId();
      return factId ? this.getProducingIntent(factId) : null;
    },

    selectedFactRecords() {
      if (!this.project || this.selectedFacts.length === 0) return [];
      const factsById = new Map(this.project.facts.map(f => [f.id, f]));
      return this.selectedFacts.map(fid => factsById.get(fid)).filter(Boolean);
    },

    selectedIntentId() {
      return this.selectedNode?.type === 'intent' ? this.selectedNode.id : null;
    },

    selectedIntentRecord() {
      const intentId = this.selectedIntentId();
      if (!this.project || !intentId) return null;
      return this.project.intents.find(i => i.id === intentId) || null;
    },

    selectedOpenIntentRecord() {
      const intent = this.selectedIntentRecord();
      if (!intent || !this.isOpenIntent(intent) || !this.projectAcceptsGraphWrites()) return null;
      return intent;
    },

    selectedActionableOpenIntentRecord() {
      const intent = this.selectedOpenIntentRecord();
      if (!intent) return null;
      const actor = this.actorName();
      return !intent.worker || intent.worker === actor ? intent : null;
    },

    selectedReleasableOpenIntentRecord() {
      const intent = this.selectedOpenIntentRecord();
      if (!intent?.worker) return null;
      return intent.worker === this.actorName() ? intent : null;
    },

    selectedIntentPrimaryActionLabel() {
      const intent = this.selectedOpenIntentRecord();
      if (!intent) return 'Claim';
      if (!intent.worker) return 'Claim';
      return intent.worker === this.actorName() ? 'Heartbeat' : 'Claimed';
    },

    getFactRecord(fid) {
      return this.project ? this.project.facts.find(f => f.id === fid) || null : null;
    },

    intentDotClass(i) {
      if (!this.isOpenIntent(i)) return 'bg-teal-400';
      if (this.isBootstrapIntent(i)) return i.worker ? 'bg-orange-400' : 'bg-orange-200';
      return i.worker ? 'bg-amber-400' : 'bg-slate-300';
    },
    intentStatusClass(i) {
      if (!this.isOpenIntent(i)) return 'text-teal-600';
      if (this.isBootstrapIntent(i)) return i.worker ? 'text-orange-600' : 'text-orange-400';
      return i.worker ? 'text-amber-600' : 'text-slate-400';
    },
    intentStatusLabel(i) {
      if (!this.isOpenIntent(i)) return 'Concluded';
      if (this.isBootstrapIntent(i)) return i.worker ? 'Bootstrap Running' : 'Bootstrap Pending';
      return i.worker ? 'In Progress' : 'Unclaimed';
    },

    timelineEvents() {
      if (this.replay.active) return this.replay.visibleEvents;
      if (!this.project) return [];
      if (this._timelineEventsCacheProject === this.project) return this._timelineEventsCache;

      const events = [];
      let order = 0;
      const origin = this.getFactRecord('origin');

      events.push({
        id: `project-created-${this.project.project.id}`,
        type: 'project_created',
        timestamp: this.project.project.created_at,
        actor: 'system',
        title: this.project.project.title,
        meta: [origin ? origin.description : null].filter(Boolean),
        targetType: 'fact',
        targetId: 'origin',
        order: order++,
        intentId: null,
        producedFactId: null,
        sourceFactIds: [],
      });

      for (const goal of this.goalFacts()) {
        events.push({
          id: `goal-declared-${goal.id}`,
          type: 'goal_declared',
          timestamp: goal.created_at,
          actor: goal.creator || 'system',
          title: goal.description,
          meta: [goal.id],
          targetType: 'fact',
          targetId: goal.id,
          order: order++,
          intentId: null,
          producedFactId: goal.id,
          sourceFactIds: [],
        });
      }

      for (const hint of this.project.hints) {
        events.push({
          id: `hint-${hint.id}`,
          type: 'hint_added',
          timestamp: hint.created_at,
          actor: hint.creator,
          title: hint.content,
          meta: [],
          targetType: 'hints',
          targetId: hint.id,
          order: order++,
          intentId: null,
          producedFactId: null,
          sourceFactIds: [],
        });
      }

      const completionIntentId = this.project.project.completed_by_intent;

      for (const intent of this.project.intents) {
        events.push({
          id: `intent-declared-${intent.id}`,
          type: 'intent_declared',
          timestamp: intent.created_at,
          actor: intent.creator,
          title: intent.description,
          meta: [intent.id, `from ${intent.from.join(', ')}`],
          targetType: 'intent',
          targetId: intent.id,
          order: order++,
          intentId: intent.id,
          producedFactId: null,
          sourceFactIds: [...intent.from],
        });

        if (!intent.concluded_at || intent.to.length === 0) continue;

        const factTargets = intent.to.filter(target => !this.isGoalId(target));
        const goalTargets = intent.to.filter(target => this.isGoalId(target));

        if (factTargets.length > 0) {
          const fact = this.getFactRecord(factTargets[0]);
          events.push({
            id: `intent-concluded-${intent.id}`,
            type: 'intent_concluded',
            timestamp: intent.concluded_at,
            actor: intent.worker || intent.creator,
            title: fact ? fact.description : intent.description,
            meta: [intent.id, `produced ${factTargets.join(', ')}`, `from ${intent.from.join(', ')}`],
            targetType: 'fact',
            targetId: factTargets[0],
            order: order++,
            intentId: intent.id,
            producedFactId: factTargets[0],
            sourceFactIds: [...intent.from],
          });
        }

        for (const gid of goalTargets) {
          events.push({
            id: `goal-completed-${gid}-${intent.id}`,
            type: 'goal_completed',
            timestamp: intent.concluded_at,
            actor: intent.worker || intent.creator,
            title: this.getFactRecord(gid)?.description || gid,
            meta: [gid, `via ${intent.id}`, `from ${intent.from.join(', ')}`],
            targetType: 'fact',
            targetId: gid,
            order: order++,
            intentId: intent.id,
            producedFactId: null,
            sourceFactIds: [...intent.from],
          });
        }

        if (intent.id === completionIntentId) {
          events.push({
            id: `project-completed-${intent.id}`,
            type: 'project_completed',
            timestamp: intent.concluded_at,
            actor: intent.worker || intent.creator,
            title: intent.description,
            meta: [intent.id, `from ${intent.from.join(', ')}`],
            targetType: 'reason',
            targetId: intent.id,
            order: order++,
            intentId: intent.id,
            producedFactId: null,
            sourceFactIds: [...intent.from],
          });
        }
      }

      const chronological = [...events].sort((a, b) =>
        a.timestamp.localeCompare(b.timestamp) || a.order - b.order
      );

      const resolved = [];
      for (let i = 0; i < chronological.length;) {
        const bucket = [chronological[i]];
        let j = i + 1;
        while (j < chronological.length && chronological[j].timestamp === chronological[i].timestamp) {
          bucket.push(chronological[j]);
          j += 1;
        }
        resolved.push(...this.resolveTimelineBucket(bucket));
        i = j;
      }

      const cached = resolved.map((entry, index) => ({ ...entry, isLast: index === resolved.length - 1 }));
      this._timelineEventsCacheProject = this.project;
      this._timelineEventsCache = cached;
      return cached;
    },

    resolveTimelineBucket(bucket) {
      if (bucket.length <= 1) return bucket;

      const eventById = new Map(bucket.map(event => [event.id, event]));
      const declareEventIdByIntent = new Map();
      const produceEventIdByFact = new Map();
      const outgoing = new Map(bucket.map(event => [event.id, new Set()]));
      const incomingCount = new Map(bucket.map(event => [event.id, 0]));

      const addDependency = (beforeId, afterId) => {
        if (!beforeId || !afterId || beforeId === afterId) return;
        const deps = outgoing.get(beforeId);
        if (!deps || deps.has(afterId)) return;
        deps.add(afterId);
        incomingCount.set(afterId, (incomingCount.get(afterId) || 0) + 1);
      };

      for (const event of bucket) {
        if (event.type === 'intent_declared') declareEventIdByIntent.set(event.intentId, event.id);
        if (event.producedFactId) produceEventIdByFact.set(event.producedFactId, event.id);
      }

      for (const event of bucket) {
        if ((event.type === 'intent_concluded' || event.type === 'goal_completed' || event.type === 'project_completed') && event.intentId) {
          addDependency(declareEventIdByIntent.get(event.intentId), event.id);
        }

        if (!event.sourceFactIds?.length) continue;
        for (const factId of event.sourceFactIds) {
          addDependency(produceEventIdByFact.get(factId), event.id);
        }
      }

      const ready = bucket
        .filter(event => incomingCount.get(event.id) === 0)
        .sort((a, b) => a.order - b.order);
      const ordered = [];

      while (ready.length > 0) {
        const event = ready.shift();
        ordered.push(event);
        for (const nextId of outgoing.get(event.id) || []) {
          incomingCount.set(nextId, incomingCount.get(nextId) - 1);
          if (incomingCount.get(nextId) === 0) {
            ready.push(eventById.get(nextId));
            ready.sort((a, b) => a.order - b.order);
          }
        }
      }

      if (ordered.length === bucket.length) return ordered;

      const remainingIds = bucket
        .filter(event => !ordered.some(placed => placed.id === event.id))
        .sort((a, b) => a.order - b.order);
      return [...ordered, ...remainingIds];
    },

    timelineEventBadge(entry) {
      const labels = {
        project_created: 'Project',
        goal_declared: 'Goal',
        goal_completed: 'Goal ✓',
        hint_added: 'Hint',
        reason_started: 'Reason',
        intent_declared: 'Intent',
        intent_running: 'Execute',
        intent_concluded: 'Conclude',
        project_completed: 'Complete',
      };
      return labels[entry.type] || 'Event';
    },

    timelineEventBadgeClass(entry) {
      const classes = {
        project_created: 'bg-slate-100 text-slate-600',
        goal_declared: 'bg-sky-50 text-sky-700',
        goal_completed: 'bg-rose-50 text-rose-700',
        hint_added: 'bg-amber-50 text-amber-700',
        reason_started: 'bg-sky-50 text-sky-700',
        intent_declared: 'bg-violet-50 text-violet-700',
        intent_running: 'bg-amber-50 text-amber-700',
        intent_concluded: 'bg-teal-50 text-teal-700',
        project_completed: 'bg-rose-50 text-rose-700',
      };
      return classes[entry.type] || 'bg-slate-100 text-slate-600';
    },

    timelineEventIsInteractive(entry) {
      return entry?.targetType === 'fact' || entry?.targetType === 'intent' || entry?.targetType === 'hints' || entry?.targetType === 'reason';
    },

    timelineEventTriggersGraphFocus(entry) {
      return entry?.targetType === 'fact' || entry?.targetType === 'intent';
    },

    timelineEventDotClass(entry) {
      const classes = {
        project_created: 'bg-slate-400',
        goal_declared: 'bg-sky-400',
        goal_completed: 'bg-rose-400',
        hint_added: 'bg-amber-400',
        reason_started: 'bg-sky-400',
        intent_declared: 'bg-violet-400',
        intent_running: 'bg-amber-400',
        intent_concluded: 'bg-teal-400',
        project_completed: 'bg-rose-400',
      };
      return classes[entry.type] || 'bg-slate-300';
    },

    timelineEntryButtonClass(entry) {
      const base = this.timelineEventIsInteractive(entry) ? 'cursor-pointer hover:bg-slate-50/70' : 'cursor-default';
      return this.timelineEntryIsSelected(entry)
        ? `${base} bg-brand-50/80 ring-1 ring-brand-200`
        : base;
    },

    timelineEntryDomId(entryId) {
      return `timeline-entry-${entryId}`;
    },

    timelineEntryElement(entryId) {
      return document.getElementById(this.timelineEntryDomId(entryId));
    },

    timelineTargetEntryIdForGraphSelection(allowMultiFact = false) {
      const events = this.timelineEvents();
      if (this.selectedNode?.type === 'intent') {
        const selectedIntentId = this.selectedNode.id;
        return events.find(entry => entry.type === 'intent_declared' && entry.intentId === selectedIntentId)?.id
          || events.find(entry => entry.intentId === selectedIntentId)?.id
          || null;
      }

      if (this.selectedNode?.type === 'fact') {
        if (!allowMultiFact && this.selectedFacts.length > 1) return null;
        const selectedFactId = this.selectedNode.id;
        return events.find(entry => entry.targetType === 'fact' && entry.targetId === selectedFactId)?.id || null;
      }

      return null;
    },

    activeTimelineEntryId(allowMultiFact = false) {
      return this.selectedTimelineEntryId || this.timelineTargetEntryIdForGraphSelection(allowMultiFact);
    },

    latestTimelineEntryId() {
      const events = this.timelineEvents();
      return events.length > 0 ? events[events.length - 1].id : null;
    },

    timelineEntryIsSelected(entry) {
      return !!entry && this.activeTimelineEntryId(false) === entry.id;
    },

    selectedTimelineSummary() {
      const targetEntryId = this.activeTimelineEntryId(false);
      if (!targetEntryId) return null;

      const events = this.timelineEvents();
      const index = events.findIndex(entry => entry.id === targetEntryId);
      if (index < 0) return null;

      const entry = events[index];
      const total = events.length;
      const sequencePercent = total <= 1 ? 100 : Math.round((index / (total - 1)) * 100);
      const replayMode = this.replay.active;
      const totalDuration = replayMode
        ? this.replayTimelineElapsedDurationMs(events, Math.max(0, total - 1))
        : Math.max(0, Date.parse(events[total - 1].timestamp) - Date.parse(events[0].timestamp));
      const elapsedDuration = replayMode
        ? this.replayTimelineElapsedDurationMs(events, index)
        : Math.max(0, Date.parse(entry.timestamp) - Date.parse(events[0].timestamp));
      const timePercent = totalDuration === 0 ? 100 : Math.round((elapsedDuration / totalDuration) * 100);

      return {
        sequencePercent,
        sequenceLabel: `${index + 1} / ${total} · ${sequencePercent}%`,
        timePercent,
        timeLabel: `${this.formatDurationMs(elapsedDuration)} / ${this.formatDurationMs(totalDuration)} · ${timePercent}%`,
      };
    },

    scrollTimelineToEntry(entryId, options = {}) {
      const { flash = true } = options;
      if (!entryId) return;
      this.$nextTick(() => {
        requestAnimationFrame(() => {
          const entry = this.timelineEntryElement(entryId);
          if (!entry) return;
          const panel = entry.closest('.overflow-y-auto');
          if (!panel) return;
          const top = entry.offsetTop - panel.clientHeight / 2 + entry.clientHeight / 2;
          const targetTop = Math.max(0, top);
          panel.scrollTo({ top: targetTop, behavior: 'smooth' });
          if (!flash) return;
          const travel = Math.abs(panel.scrollTop - targetTop);
          const flashDelay = Math.min(450, Math.max(140, travel * 0.18));
          setTimeout(() => {
            entry.classList.remove('timeline-flash');
            void entry.offsetWidth;
            entry.classList.add('timeline-flash');
            setTimeout(() => entry.classList.remove('timeline-flash'), 1000);
          }, flashDelay);
        });
      });
    },

    followReplayTimelineTail() {
      if (!this.replay.active || this.sideTab !== 'log') return;
      this.scrollTimelineToEntry(this.latestTimelineEntryId(), { flash: false });
    },

    scrollTimelineToSelection() {
      if (this.sideTab !== 'log') return;
      this.scrollTimelineToEntry(this.activeTimelineEntryId(true));
    },

    openHintTimelineEntry(hintId) {
      if (!hintId) return;
      const entry = this.timelineEvents().find(event => event.type === 'hint_added' && event.targetId === hintId);
      if (!entry) return;
      this.sideTab = 'log';
      this.openTimelineEntry(entry);
      this.scrollTimelineToEntry(entry.id);
    },

    openTimelineEntry(entry, options = {}) {
      const { centerGraph = true } = options;
      if (!entry || !this.timelineEventIsInteractive(entry)) return;
      if (!this.timelineEventTriggersGraphFocus(entry)) {
        this.selectedTimelineEntryId = entry.id;
        this.clearGraphSelection(true);
        return;
      }
      if (entry.targetType === 'intent' && entry.targetId) {
        this.selectIntent(entry.targetId);
        this.selectedTimelineEntryId = entry.id;
        if (centerGraph) this.centerGraphOnIntent(entry.targetId);
        return;
      }
      if (entry.targetType === 'fact' && entry.targetId) {
        this.selectedFacts = [entry.targetId];
        this.selectFact(entry.targetId);
        this.selectedTimelineEntryId = entry.id;
        if (centerGraph) this.centerGraphOnFact(entry.targetId);
      }
    },

    formatTime(ts) { if (!ts) return ''; return new Date(ts).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'}); },
    formatDate(ts) { if (!ts) return ''; const d = new Date(ts); return d.toLocaleDateString([],{year:'numeric',month:'short',day:'numeric'}) + ' ' + d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}); },
    formatTimelineDate(ts) { if (!ts) return ''; return new Date(ts).toLocaleDateString([],{year:'numeric',month:'short',day:'numeric'}); },
    formatDurationMs(ms) {
      const totalSeconds = Math.max(0, Math.round(ms / 1000));
      const hours = Math.floor(totalSeconds / 3600);
      const minutes = Math.floor((totalSeconds % 3600) / 60);
      const seconds = totalSeconds % 60;
      if (hours > 0) return `${hours}h ${minutes}m`;
      if (minutes > 0) return `${minutes}m ${seconds}s`;
      return `${seconds}s`;
    },

    async createProject() {
      try {
        const actor = this.actorName();
        const hintContents = this.newProject.hints.filter(h => h.content?.trim());
        const body = {
          title: this.newProject.title,
          origin: this.newProject.origin,
          goal: this.newProject.goal,
          bootstrap_enabled: this.newProject.bootstrap
        };
        const hints = hintContents.map(h => ({ content: h.content.trim(), creator: actor }));
        if (hints.length > 0) body.hints = hints;
        const data = await this.api('POST', '/projects', body);
        this.showNewProject = false;
        this.newProject = { title:'', origin:'', goal:'', bootstrap: true, hints: [{ content:'' }] };
        await this.loadProjects();
        await this.openProject(data.project.id);
        this.showToast('Project created');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    openCreateIntent() {
      if (!this.canActOnSelectedFacts()) return;
      this.intentForm = { description:'' };
      this.showIntentModal = true;
    },

    async createIntent(claim = false) {
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/intents`, {
          from: this.selectedFacts,
          description: this.intentForm.description,
          creator: actor,
          worker: claim ? actor : null,
        });
        this.showIntentModal = false;
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast(claim ? 'Intent declared and claimed' : 'Intent declared');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async sendHeartbeatForSelectedIntent() {
      const intent = this.selectedActionableOpenIntentRecord();
      if (!intent) return;
      try {
        const actor = this.actorName();
        const claiming = !intent.worker;
        await this.api('POST', `/projects/${this.selectedProjectId}/intents/${intent.id}/heartbeat`, { worker: actor });
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast(claiming ? 'Intent claimed' : 'Heartbeat sent');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async releaseSelectedIntent() {
      const intent = this.selectedReleasableOpenIntentRecord();
      if (!intent) return;
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/intents/${intent.id}/release`, { worker: actor });
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast('Intent released');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    openConclude(intent) { this.concludeForm = { description:'', intentId: intent.id }; this.showConcludeModal = true; },

    async concludeIntent() {
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/intents/${this.concludeForm.intentId}/conclude`, { worker: actor, description: this.concludeForm.description });
        this.showConcludeModal = false;
        this.selectedNode = null;
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast('Intent concluded');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    openCompleteProject() {
      if (!this.canActOnSelectedFacts()) return;
      this.completeForm = { description:'', goals: [] };
      this.showCompleteModal = true;
    },

    openHintModal() {
      if (!this.projectCanWriteHints()) return;
      this.hintForm = { content:'' };
      this.showHintModal = true;
    },

    async completeProject() {
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/complete`, {
          from: this.selectedFacts,
          description: this.completeForm.description,
          worker: actor,
          goals: this.completeForm.goals,
        });
        this.showCompleteModal = false;
        await this.loadProjects();
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast('Goals completed');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async addHint() {
      if (!this.projectCanWriteHints()) return;
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/hints`, { content: this.hintForm.content, creator: actor });
        this.showHintModal = false;
        this.hintForm = { content:'' };
        await this.loadProjects();
        await this.loadProject(this.selectedProjectId);
        this.sideTab = 'hints';
        this.showToast('Hint added');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    saveLocalSettings() {
      try {
        this.localPrefs.actor_name = this.actorName();
        this.localPrefs.layout_mode = this.isValidLayoutMode(this.localPrefs.layout_mode) ? this.localPrefs.layout_mode : 'dagre_tb';
        this.layoutMode = this.localPrefs.layout_mode;
        this.saveLocalPrefs();
        if (this.cy) this.cy.layout(this.layoutOpts()).run();
        this.showLocalPrefs = false;
        this.showToast('Local preferences saved');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async saveServerSettings() {
      try {
        await this.api('PUT', '/settings', this.settingsForm);
        this.showToast('Server settings saved');
      } catch(e) { this.showToast(e.message, 'error'); }
    },
  };
}
