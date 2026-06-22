/* Edge AI Person Detection - ES5 JavaScript */
/* Based on Web App Template patterns */

(function() {
    'use strict';

    /* --- Dark Mode --- */
    function initDarkMode() {
        var toggle = document.getElementById('dark-mode-toggle');
        if (!toggle) return;

        var saved = localStorage.getItem('edge-ai-theme');
        if (saved === 'dark') {
            document.documentElement.setAttribute('data-theme', 'dark');
            toggle.innerHTML = '<i class="fas fa-sun"></i>';
        }

        toggle.addEventListener('click', function() {
            var current = document.documentElement.getAttribute('data-theme');
            if (current === 'dark') {
                document.documentElement.removeAttribute('data-theme');
                toggle.innerHTML = '<i class="fas fa-moon"></i>';
                localStorage.setItem('edge-ai-theme', 'light');
            } else {
                document.documentElement.setAttribute('data-theme', 'dark');
                toggle.innerHTML = '<i class="fas fa-sun"></i>';
                localStorage.setItem('edge-ai-theme', 'dark');
            }
        });
    }

    /* --- Error Display --- */
    function clearErrors() {
        var els = document.querySelectorAll('.error-msg');
        for (var i = 0; i < els.length; i++) {
            els[i].textContent = '';
            els[i].className = 'error-msg';
        }
    }

    function showError(elementId, message) {
        var el = document.getElementById(elementId);
        if (el) {
            el.textContent = message;
            el.className = 'error-msg visible';
        }
    }

    function showToast(type, message) {
        var container = document.getElementById('toast-container');
        if (!container) return;

        var icons = {
            success: 'fas fa-check-circle',
            error: 'fas fa-exclamation-circle',
            warning: 'fas fa-exclamation-triangle',
            info: 'fas fa-info-circle'
        };
        var icon = icons[type] || icons.info;

        var toast = document.createElement('div');
        toast.className = 'toast ' + type;
        toast.innerHTML = '<i class="' + icon + '"></i><span class="toast-message">' + message + '</span>';
        container.appendChild(toast);

        setTimeout(function() { toast.classList.add('show'); }, 50);

        setTimeout(function() {
            toast.classList.remove('show');
            setTimeout(function() {
                if (toast.parentNode) toast.parentNode.removeChild(toast);
            }, 300);
        }, 4000);
    }

    /* --- Validation --- */
    function validateConfidence(value) {
        var num = parseFloat(value);
        if (isNaN(num)) {
            return 'Confidence threshold must be a numeric value';
        }
        if (num < 0.1 || num > 1.0) {
            return 'Confidence threshold must be between 0.1 and 1.0';
        }
        return '';
    }

    function validateFps(value) {
        var num = parseInt(value, 10);
        if (isNaN(num) || String(num) !== String(value).trim()) {
            return 'Target FPS must be an integer value';
        }
        if (num < 1 || num > 30) {
            return 'Target FPS must be between 1 and 30';
        }
        return '';
    }

    function validateResolution(value) {
        var valid = ['320x240', '640x480', '1280x720'];
        for (var i = 0; i < valid.length; i++) {
            if (valid[i] === value) return '';
        }
        return 'Input resolution must be one of: 320x240, 640x480, 1280x720';
    }

    function validateRtspUrl(value) {
        if (value === '') return '';
        if (value.length > 2048) {
            return 'RTSP URL must not exceed 2048 characters';
        }
        if (value.indexOf('rtsp://') !== 0) {
            return 'RTSP URL must start with rtsp://';
        }
        return '';
    }

    /* --- Session Control --- */
    var sessionId = '';
    var isPrimary = false;

    /* --- Resource Chart History --- */
    var cpuHistory = [];
    var memHistory = [];
    var fpsHistory = [];
    var CHART_MAX_POINTS = 15; /* 30 seconds / 2 second interval */

    function updateResourceChart() {
        var canvas = document.getElementById('resource-canvas');
        if (!canvas) return;
        var ctx = canvas.getContext('2d');
        var w = canvas.width;
        var h = canvas.height;

        ctx.clearRect(0, 0, w, h);

        /* Draw grid lines */
        ctx.strokeStyle = 'rgba(200, 211, 235, 0.3)';
        ctx.lineWidth = 1;
        for (var y = 0; y <= 100; y += 25) {
            var yPos = h - (y / 100) * h;
            ctx.beginPath();
            ctx.moveTo(0, yPos);
            ctx.lineTo(w, yPos);
            ctx.stroke();
        }

        /* Draw CPU line */
        if (cpuHistory.length > 1) {
            ctx.strokeStyle = '#4f46e5';
            ctx.lineWidth = 2;
            ctx.beginPath();
            for (var i = 0; i < cpuHistory.length; i++) {
                var x = (i / (CHART_MAX_POINTS - 1)) * w;
                var y2 = h - (cpuHistory[i] / 100) * h;
                if (i === 0) { ctx.moveTo(x, y2); } else { ctx.lineTo(x, y2); }
            }
            ctx.stroke();
        }

        /* Draw Memory line */
        if (memHistory.length > 1) {
            ctx.strokeStyle = '#059669';
            ctx.lineWidth = 2;
            ctx.beginPath();
            for (var j = 0; j < memHistory.length; j++) {
                var x2 = (j / (CHART_MAX_POINTS - 1)) * w;
                var y3 = h - (memHistory[j] / 100) * h;
                if (j === 0) { ctx.moveTo(x2, y3); } else { ctx.lineTo(x2, y3); }
            }
            ctx.stroke();
        }

        /* Draw FPS % line */
        if (fpsHistory.length > 1) {
            ctx.strokeStyle = '#d97706';
            ctx.lineWidth = 2;
            ctx.beginPath();
            for (var k = 0; k < fpsHistory.length; k++) {
                var x3 = (k / (CHART_MAX_POINTS - 1)) * w;
                var fpsVal = Math.min(fpsHistory[k], 100);
                var y4 = h - (fpsVal / 100) * h;
                if (k === 0) { ctx.moveTo(x3, y4); } else { ctx.lineTo(x3, y4); }
            }
            ctx.stroke();
        }
    }

    function registerSession() {
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '/session', true);
        xhr.onreadystatechange = function() {
            if (xhr.readyState === 4) {
                if (xhr.status === 200) {
                    try {
                        var resp = JSON.parse(xhr.responseText);
                        sessionId = resp.session_id;
                        isPrimary = resp.is_primary;
                        updateControlState();
                    } catch (e) {
                        /* Parse error */
                    }
                } else {
                    /* Session registration failed - retry in 2 seconds */
                    setTimeout(registerSession, 2000);
                }
            }
        };
        xhr.send();
    }

    function sendHeartbeat() {
        if (!sessionId) return;
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/heartbeat', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function() {
            if (xhr.readyState === 4 && xhr.status === 200) {
                try {
                    var resp = JSON.parse(xhr.responseText);
                    var wasPrimary = isPrimary;
                    isPrimary = resp.is_primary;
                    if (isPrimary !== wasPrimary) {
                        updateControlState();
                    }
                } catch (e) {
                    /* Silently ignore parse errors */
                }
            }
        };
        xhr.send(JSON.stringify({session_id: sessionId}));
    }

    function updateControlState() {
        var banner = document.getElementById('control-lock-banner');
        var controlsGrid = document.querySelector('.controls-grid');
        var videoControls = document.querySelector('.video-controls');
        var sessionIndicator = document.getElementById('session-indicator');
        var sessionRole = document.getElementById('session-role');
        if (!banner || !controlsGrid) return;

        if (isPrimary) {
            banner.className = 'control-lock-banner';
            controlsGrid.className = 'controls-grid';
            if (videoControls) videoControls.className = 'video-controls';
            if (sessionIndicator) sessionIndicator.className = 'session-indicator primary';
            if (sessionRole) sessionRole.textContent = 'Primary User';
        } else {
            banner.className = 'control-lock-banner visible';
            controlsGrid.className = 'controls-grid controls-disabled';
            if (videoControls) videoControls.className = 'video-controls controls-disabled';
            if (sessionIndicator) sessionIndicator.className = 'session-indicator viewer';
            if (sessionRole) sessionRole.textContent = 'Viewer Only';
        }

        /* Collapse config and model panels for viewers */
        var panels = document.querySelectorAll('.panel');
        for (var p = 0; p < panels.length; p++) {
            var header = panels[p].querySelector('.panel-header h2');
            if (!header) continue;
            var text = header.textContent || '';
            if (text.indexOf('Configuration') >= 0 || text.indexOf('Model Info') >= 0) {
                if (!isPrimary) {
                    panels[p].classList.add('collapsed');
                } else {
                    panels[p].classList.remove('collapsed');
                }
            }
        }
    }

    /* --- Apply Configuration --- */
    function applyConfig() {
        clearErrors();

        var confidenceVal = document.getElementById('confidence-threshold').value;
        var fpsVal = document.getElementById('target-fps').value;
        var rtspVal = document.getElementById('rtsp-url').value;

        var hasError = false;

        var confErr = validateConfidence(confidenceVal);
        if (confErr) { showError('error-confidence', confErr); hasError = true; }

        var fpsErr = validateFps(fpsVal);
        if (fpsErr) { showError('error-fps', fpsErr); hasError = true; }

        var rtspErr = validateRtspUrl(rtspVal);
        if (rtspErr) { showError('error-rtsp', rtspErr); hasError = true; }

        if (hasError) return;

        var payload = {
            confidence_threshold: parseFloat(confidenceVal),
            target_fps: parseInt(fpsVal, 10)
        };

        if (rtspVal !== '') {
            payload.rtsp_url = rtspVal;
        }

        payload.show_bboxes = document.getElementById('toggle-bboxes').checked;
        payload.show_labels = document.getElementById('toggle-labels').checked;
        payload.show_fps = document.getElementById('toggle-fps').checked;
        payload.show_detcount = document.getElementById('toggle-detcount').checked;

        var skipVal = document.getElementById('skip-frames').value;
        var skipNum = parseInt(skipVal, 10);
        if (!isNaN(skipNum) && skipNum >= 0 && skipNum <= 10) {
            payload.skip_inference_frames = skipNum;
        }

        var jpegVal = document.getElementById('jpeg-quality').value;
        var jpegNum = parseInt(jpegVal, 10);
        if (!isNaN(jpegNum) && jpegNum >= 1 && jpegNum <= 100) {
            payload.jpeg_quality = jpegNum;
        }

        payload.model_name = document.getElementById('model-select').value;

        if (sessionId) {
            payload.session_id = sessionId;
        }

        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/config', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function() {
            if (xhr.readyState === 4) {
                if (xhr.status === 200) {
                    showToast('success', 'Configuration applied successfully');
                } else {
                    try {
                        var resp = JSON.parse(xhr.responseText);
                        if (resp.errors) {
                            var fields = Object.keys(resp.errors);
                            for (var i = 0; i < fields.length; i++) {
                                var field = fields[i];
                                var errorId = '';
                                if (field === 'confidence_threshold') errorId = 'error-confidence';
                                else if (field === 'target_fps') errorId = 'error-fps';
                                else if (field === 'input_resolution') errorId = 'error-resolution';
                                else if (field === 'rtsp_url') errorId = 'error-rtsp';
                                else if (field === 'model_name') errorId = 'error-model';
                                if (errorId) showError(errorId, resp.errors[field]);
                            }
                            showToast('error', 'Some configuration values were rejected');
                        } else if (resp.error) {
                            showToast('error', resp.error);
                        } else {
                            showToast('error', 'Failed to apply configuration');
                        }
                    } catch (e) {
                        showToast('error', 'Failed to apply configuration');
                    }
                }
            }
        };
        xhr.send(JSON.stringify(payload));
    }

    /* --- Detection Start/Stop Control --- */
    var isViewing = false;  /* Local viewing state (independent of server detection state) */

    function startDetection() {
        var img = document.getElementById('video-stream');
        if (isPrimary) {
            // Primary user: start server-side detection then reconnect stream
            var xhr = new XMLHttpRequest();
            xhr.open('POST', '/control', true);
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.onreadystatechange = function() {
                if (xhr.readyState === 4 && xhr.status === 200) {
                    // Force stream reconnect so browser picks up live frames
                    if (img) {
                        img.src = '';
                        setTimeout(function() { img.src = '/stream'; }, 100);
                    }
                    isViewing = true;
                    updateDetectionUI('running');
                }
            };
            xhr.send(JSON.stringify({action: 'start', session_id: sessionId}));
        } else {
            // Non-primary: reconnect to stream (view whatever server is serving)
            if (img) {
                img.src = '';
                setTimeout(function() { img.src = '/stream'; }, 100);
            }
            isViewing = true;
            updateDetectionUI('running');
        }
    }

    function stopDetection() {
        var img = document.getElementById('video-stream');
        if (isPrimary) {
            // Primary user: stop server-side detection
            var xhr = new XMLHttpRequest();
            xhr.open('POST', '/control', true);
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.onreadystatechange = function() {
                if (xhr.readyState === 4 && xhr.status === 200) {
                    isViewing = false;
                    updateDetectionUI('stopped');
                    // Stream now serves placeholder — force reconnect to show it
                    if (img) {
                        img.src = '';
                        setTimeout(function() { img.src = '/stream'; }, 100);
                    }
                }
            };
            xhr.send(JSON.stringify({action: 'stop', session_id: sessionId}));
        } else {
            // Non-primary: disconnect from stream (blank)
            if (img) img.src = '';
            isViewing = false;
            updateDetectionUI('stopped');
        }
    }

    function updateDetectionUI(state) {
        var startBtn = document.getElementById('btn-start');
        var stopBtn = document.getElementById('btn-stop');
        var stateEl = document.getElementById('detection-state');
        if (state === 'running') {
            if (startBtn) startBtn.style.display = 'none';
            if (stopBtn) stopBtn.style.display = '';
            if (stateEl) stateEl.textContent = 'Running';
        } else {
            if (startBtn) startBtn.style.display = '';
            if (stopBtn) stopBtn.style.display = 'none';
            if (stateEl) stateEl.textContent = 'Stopped';
        }
    }

    /* --- Model Switch (immediate on dropdown change) --- */
    function onModelChange() {
        var select = document.getElementById('model-select');
        if (!select) return;
        var val = select.value;

        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/config', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function() {
            if (xhr.readyState === 4) {
                if (xhr.status === 200) {
                    showToast('success', 'Model switched to ' + val);
                } else {
                    showToast('error', 'Failed to switch model');
                }
            }
        };
        var payload = {model_name: val};
        if (sessionId) { payload.session_id = sessionId; }
        xhr.send(JSON.stringify(payload));
    }

    /* --- Stats Refresh --- */
    function refreshStats() {
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '/stats', true);
        xhr.onreadystatechange = function() {
            if (xhr.readyState === 4 && xhr.status === 200) {
                try {
                    var stats = JSON.parse(xhr.responseText);
                    var fpsEl = document.getElementById('stat-fps');
                    var detectEl = document.getElementById('stat-detections');
                    var inferEl = document.getElementById('stat-inference');
                    var statusEl = document.getElementById('stat-status');

                    if (fpsEl) fpsEl.textContent = stats.current_fps.toFixed(1);
                    if (detectEl) detectEl.textContent = String(stats.total_detections);
                    if (inferEl) inferEl.textContent = stats.avg_inference_ms.toFixed(1) + ' ms';
                    if (statusEl) {
                        statusEl.textContent = stats.connection_status;
                        statusEl.className = 'stat-value status-' + stats.connection_status;
                    }

                    /* Update model/stream info */
                    var streamResEl = document.getElementById('info-stream-res');
                    var streamEl = document.getElementById('info-stream');
                    var modelResEl = document.getElementById('info-model-res');
                    var modelNameEl = document.getElementById('info-model-name');
                    var modelEl = document.getElementById('info-model');
                    var inputEl = document.getElementById('info-input');
                    var outputEl = document.getElementById('info-output');

                    if (streamResEl && stats.stream_resolution) {
                        streamResEl.textContent = stats.stream_resolution;
                    }
                    if (streamEl && stats.stream_resolution) {
                        streamEl.textContent = stats.stream_resolution + ' (full color BGR24)';
                    }
                    if (modelResEl && stats.model_input) {
                        modelResEl.textContent = stats.model_input;
                    }
                    if (modelNameEl && stats.model_name) {
                        modelNameEl.textContent = stats.model_name;
                    }
                    if (modelEl && stats.model_name) {
                        modelEl.textContent = stats.model_name + ' (INT8)';
                    }

                    /* Sync model dropdown to current server state */
                    var modelSelect = document.getElementById('model-select');
                    if (modelSelect && stats.model_key && modelSelect.value !== stats.model_key) {
                        modelSelect.value = stats.model_key;
                    }
                    if (inputEl && stats.model_input) {
                        inputEl.textContent = stats.model_input + ' RGB uint8';
                    }
                    if (outputEl && stats.stream_resolution) {
                        outputEl.textContent = stats.stream_resolution + ' full color (BGR24)';
                    }

                    /* Update detection state from stats — controls UI for all users */
                    if (stats.detection_state) {
                        updateDetectionUI(stats.detection_state);
                        isViewing = (stats.detection_state === 'running');
                    }

                    /* Update resource usage */
                    var cpuPct = document.getElementById('cpu-percent');
                    var memPct = document.getElementById('mem-percent');
                    var memDetail = document.getElementById('mem-detail');
                    var fpsPct = document.getElementById('fps-percent');
                    var sourceFpsEl = document.getElementById('info-source-fps');

                    if (typeof stats.cpu_percent === 'number') {
                        if (cpuPct) cpuPct.textContent = stats.cpu_percent.toFixed(1) + '%';
                        cpuHistory.push(stats.cpu_percent);
                        if (cpuHistory.length > CHART_MAX_POINTS) cpuHistory.shift();
                    }
                    if (typeof stats.mem_percent === 'number') {
                        if (memPct) memPct.textContent = stats.mem_percent.toFixed(1) + '%';
                        memHistory.push(stats.mem_percent);
                        if (memHistory.length > CHART_MAX_POINTS) memHistory.shift();
                    }
                    if (typeof stats.mem_used_mb === 'number' && memDetail) {
                        memDetail.textContent = stats.mem_used_mb.toFixed(1) + ' MB';
                    }
                    if (typeof stats.fps_percent === 'number') {
                        if (fpsPct) fpsPct.textContent = stats.fps_percent.toFixed(0) + '%';
                        fpsHistory.push(stats.fps_percent);
                        if (fpsHistory.length > CHART_MAX_POINTS) fpsHistory.shift();
                    }
                    if (typeof stats.source_fps === 'number' && sourceFpsEl) {
                        sourceFpsEl.textContent = stats.source_fps > 0 ? stats.source_fps.toFixed(0) : '--';
                    }
                    updateResourceChart();
                } catch (e) {
                    /* Silently ignore parse errors */
                }
            }
        };
        xhr.send();
    }

    /* --- Init --- */
    function init() {
        initDarkMode();

        var applyBtn = document.getElementById('apply-btn');
        if (applyBtn) {
            applyBtn.addEventListener('click', applyConfig);
        }

        /* Wire up start/stop detection buttons */
        var startBtn = document.getElementById('btn-start');
        if (startBtn) startBtn.addEventListener('click', startDetection);
        var stopBtn = document.getElementById('btn-stop');
        if (stopBtn) stopBtn.addEventListener('click', stopDetection);

        /* Wire up model selector for immediate switch */
        var modelSelect = document.getElementById('model-select');
        if (modelSelect) {
            modelSelect.addEventListener('change', onModelChange);
        }

        /* Panel collapse toggle */
        var panelHeaders = document.querySelectorAll('.panel-header');
        for (var i = 0; i < panelHeaders.length; i++) {
            (function(header) {
                header.addEventListener('click', function(e) {
                    e.stopPropagation();
                    var panel = header.parentNode;
                    while (panel && !panel.classList.contains('panel')) {
                        panel = panel.parentNode;
                    }
                    if (panel) {
                        panel.classList.toggle('collapsed');
                    }
                });
            })(panelHeaders[i]);
        }

        /* Help modal */
        var helpBtn = document.getElementById('help-toggle');
        var helpClose = document.getElementById('help-close');
        var helpModal = document.getElementById('help-modal');
        if (helpBtn && helpModal) {
            helpBtn.addEventListener('click', function() {
                helpModal.classList.add('visible');
                loadHelp();
            });
        }
        if (helpClose && helpModal) {
            helpClose.addEventListener('click', function() {
                helpModal.classList.remove('visible');
            });
        }
        if (helpModal) {
            helpModal.addEventListener('click', function(e) {
                if (e.target === helpModal) {
                    helpModal.classList.remove('visible');
                }
            });
        }

        /* Load current config values into form fields */
        loadCurrentConfig();

        /* Register session and start heartbeat */
        registerSession();
        setInterval(sendHeartbeat, 3000);

        /* Refresh stats every 2 seconds */
        setInterval(refreshStats, 2000);
        refreshStats();
    }

    function loadHelp() {
        var body = document.getElementById('help-modal-body');
        if (!body) return;
        if (body.dataset.loaded === 'true') return;
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '/help', true);
        xhr.onreadystatechange = function() {
            if (xhr.readyState === 4 && xhr.status === 200) {
                var text = xhr.responseText;
                var html = renderMarkdown(text);
                body.innerHTML = html;
                body.dataset.loaded = 'true';
            }
        };
        xhr.send();
    }

    function renderMarkdown(text) {
        /* Process code blocks first (preserve content) */
        var codeBlocks = [];
        text = text.replace(/```(\w*)\n([\s\S]*?)```/g, function(match, lang, code) {
            var idx = codeBlocks.length;
            codeBlocks.push('<pre><code>' + escapeHtml(code.trim()) + '</code></pre>');
            return '%%CODEBLOCK' + idx + '%%';
        });

        /* Split into lines for processing */
        var lines = text.split('\n');
        var html = '';
        var inList = false;

        for (var i = 0; i < lines.length; i++) {
            var line = lines[i];

            /* Headers */
            if (line.match(/^### /)) {
                if (inList) { html += '</ul>'; inList = false; }
                html += '<h4>' + processInline(line.substring(4)) + '</h4>';
            } else if (line.match(/^## /)) {
                if (inList) { html += '</ul>'; inList = false; }
                html += '<h3>' + processInline(line.substring(3)) + '</h3>';
            } else if (line.match(/^# /)) {
                if (inList) { html += '</ul>'; inList = false; }
                html += '<h2>' + processInline(line.substring(2)) + '</h2>';
            }
            /* List items */
            else if (line.match(/^- /)) {
                if (!inList) { html += '<ul>'; inList = true; }
                html += '<li>' + processInline(line.substring(2)) + '</li>';
            }
            /* Code block placeholder */
            else if (line.match(/^%%CODEBLOCK\d+%%$/)) {
                if (inList) { html += '</ul>'; inList = false; }
                var idx = parseInt(line.replace(/%%CODEBLOCK(\d+)%%/, '$1'), 10);
                html += codeBlocks[idx];
            }
            /* Empty line = paragraph break */
            else if (line.trim() === '') {
                if (inList) { html += '</ul>'; inList = false; }
                html += '<br>';
            }
            /* Regular text */
            else {
                if (inList) { html += '</ul>'; inList = false; }
                html += '<p>' + processInline(line) + '</p>';
            }
        }
        if (inList) { html += '</ul>'; }
        return html;
    }

    function processInline(text) {
        /* Bold */
        text = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        /* Inline code */
        text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
        return text;
    }

    function escapeHtml(text) {
        return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function loadCurrentConfig() {
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '/config', true);
        xhr.onreadystatechange = function() {
            if (xhr.readyState === 4 && xhr.status === 200) {
                try {
                    var cfg = JSON.parse(xhr.responseText);
                    var el;
                    if (cfg.confidence_threshold !== undefined) {
                        el = document.getElementById('confidence-threshold');
                        if (el) el.value = cfg.confidence_threshold;
                    }
                    if (cfg.target_fps !== undefined) {
                        el = document.getElementById('target-fps');
                        if (el) el.value = cfg.target_fps;
                    }
                    if (cfg.skip_inference_frames !== undefined) {
                        el = document.getElementById('skip-frames');
                        if (el) el.value = cfg.skip_inference_frames;
                    }
                    if (cfg.jpeg_quality !== undefined) {
                        el = document.getElementById('jpeg-quality');
                        if (el) el.value = cfg.jpeg_quality;
                    }
                    if (cfg.rtsp_url) {
                        el = document.getElementById('rtsp-url');
                        if (el) el.value = cfg.rtsp_url;
                    }
                    if (cfg.show_bboxes !== undefined) {
                        el = document.getElementById('toggle-bboxes');
                        if (el) el.checked = cfg.show_bboxes;
                    }
                    if (cfg.show_labels !== undefined) {
                        el = document.getElementById('toggle-labels');
                        if (el) el.checked = cfg.show_labels;
                    }
                    if (cfg.show_fps !== undefined) {
                        el = document.getElementById('toggle-fps');
                        if (el) el.checked = cfg.show_fps;
                    }
                    if (cfg.show_detcount !== undefined) {
                        el = document.getElementById('toggle-detcount');
                        if (el) el.checked = cfg.show_detcount;
                    }
                } catch (e) {
                    /* Silently ignore */
                }
            }
        };
        xhr.send();
    }

    /* Run on DOM ready */
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
